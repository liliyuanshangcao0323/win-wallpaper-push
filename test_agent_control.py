#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""被控端「启停控制」自测（真的起进程，但不换壁纸）

针对的是这类现场抱怨：
    * 静默后台跑起来之后根本关不掉，只能去任务管理器结束进程
    * 自启项 + 开始菜单快捷方式 + 用户双击，一台机器上跑出好几个被控端
    * 装了没反应时，不知道它到底有没有在跑、日志在哪

覆盖：
    1. winipc 控制通道（命名事件）本身
    2. `--status`：没跑时返回 1 并说清楚；在跑时返回 0 并给出端口 / 日志路径
    3. `--silent` 启动：进程活着、窗口是隐藏的（不是最小化，是真看不见）
    4. 单实例：再启动一次不会重复运行，原实例还活着
    5. `--show`：把隐藏的窗口叫出来（用 EnumWindows 数可见窗口验证）
    6. `--stop`：干净退出、状态文件被清掉、日志里有启动和退出记录

运行： python test_agent_control.py
退出码 0 = 全部通过。测试全程只用一个临时端口和临时配置，不碰你现有的配置。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

import winipc

RESULTS: list[tuple[bool, str]] = []
TEST_PORT = 39371
# 面板窗口标题里必有这一段；用它区分"面板打开了"和"只是弹了个密码框"
APP_TITLE_HINT = "壁纸推送"


def check(ok: bool, desc: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), desc))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {desc}"
    if detail:
        line += f"   ({detail})"
    print(line, flush=True)
    return bool(ok)


def skip(desc: str, why: str) -> None:
    print(f"  [SKIP] {desc}   ({why})", flush=True)


# ---------------------------------------------------------------- 小工具

def state_file() -> str:
    import agent

    return agent.state_file_path()


def run_agent(*args: str, timeout: float = 90.0) -> tuple[int, str]:
    """跑一次 WallpaperAgent（源码方式），返回 (退出码, 输出)。

    强制子进程用 UTF-8 输出：管道下 Python 默认按系统代码页写，中文会乱码，
    断言就全失效了（打包后的 exe 会自动挂到 cmd 控制台，用控制台代码页）。

    WPP_PROMPT_TIMEOUT：被控端面板要密码，自动化场景下没人去点那个弹窗 ——
    设个 2 秒超时，超时按「取消」处理（= 拒绝），这样测试不会挂住，
    也不会在桌面上留一个谁都点不掉的模态框。
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8", WPP_PROMPT_TIMEOUT="2")
    proc = subprocess.run(
        [sys.executable, "agent.py", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW, env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def start_agent(config: str, extra: list[str] | None = None) -> subprocess.Popen:
    env = dict(os.environ, WPP_PROMPT_TIMEOUT="2")
    return subprocess.Popen(
        [sys.executable, "agent.py", "--config", config, "--port", str(TEST_PORT),
         *(extra or [])],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW, env=env,
    )


def visible_windows(pid: int) -> int:
    """数一下某个进程有多少个「看得见」的顶层窗口。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found = {"n": 0}

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found["n"] += 1
        return True

    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(proto(callback), 0)
    return found["n"]


def window_titles(pid: int) -> list[str]:
    """某个进程所有「看得见」的顶层窗口标题。

    为什么要看标题：叫窗口时会先弹**密码对话框**，它自己也是一个可见窗口，
    只数数量会误判成"面板已经打开了"（测试真的这么骗过自己一次）。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    titles: list[str] = []

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            titles.append(buf.value)
        return True

    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(proto(callback), 0)
    return titles


def wait_for_log(path: str, needle: str, timeout: float = 15.0) -> str:
    """等日志里出现某个字符串，返回当时的全部内容（等不到就返回最后一次读到的）。"""
    text = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
        if needle in text:
            return text
        time.sleep(0.3)
    return text


def core_pid(proc: subprocess.Popen) -> int:
    """单实例里真正干活的进程号（源码方式 = 自己；状态文件里的那个）。"""
    st = {}
    try:
        with open(state_file(), "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        pass
    return int(st.get("pid") or proc.pid)


# ---------------------------------------------------------------- 1. 控制通道

# ---------------------------------------------------------------- 0. 批处理自检

def test_bat_files() -> None:
    """所有 .bat 必须是纯 ASCII + CRLF 换行。

    * cmd.exe 是按**字节偏移**在批处理文件里找下一行的，文件里只要有一个
      多字节字符（哪怕只是 rem 里的一句中文），它就可能落到字符中间，把后面
      几行开头的字符吃掉 —— 表现出来就是「莫名其妙的语法错误」。
    * 纯 LF 换行的批处理在 Windows 上也能跑，但遇到 goto / 括号块时 cmd 会
      出各种怪问题，所以统一 CRLF。
    这两条都是踩过的坑，做成自动检查。
    """
    print("\n[0] 批处理文件自检")
    import glob

    files = sorted(glob.glob("*.bat")) + sorted(glob.glob("installer/*.bat"))
    if not files:
        skip("批处理自检", "当前目录下没有找到 .bat")
        return
    not_ascii, not_crlf = [], []
    for path in files:
        with open(path, "rb") as f:
            data = f.read()
        if any(b > 127 for b in data):
            not_ascii.append(path)
        if b"\r\n" not in data or data.replace(b"\r\n", b"").count(b"\n"):
            not_crlf.append(path)
    check(not not_ascii, f"全部 {len(files)} 个 .bat 都是纯 ASCII（cmd 按字节偏移解析）",
          "、".join(not_ascii) if not_ascii else "多字节字符会吃掉后面几行")
    check(not not_crlf, "全部 .bat 都是 CRLF 换行", "、".join(not_crlf) if not_crlf else "避免 goto/括号块解析怪问题")


def test_autostart_report() -> None:
    """开机自启诊断输出（纯函数，喂假数据）。"""
    print("\n[0b] 开机自启诊断")
    from agent import describe_autostart

    none_txt = "\n".join(describe_autostart([]))
    check("未设置" in none_txt, "没配自启时说清楚「未设置」", none_txt.splitlines()[0])

    disabled = [{
        "hive": "HKCU", "name": "WinWallpaperAgent", "enabled": False,
        "command": '"D:\\gone\\WallpaperAgent.exe" --silent',
    }]
    txt = "\n".join(describe_autostart(disabled))
    check("已被禁用" in txt, "被任务管理器禁用时明确报出来")
    check("目标文件不存在" in txt, "自启指向的程序被挪走时报出来")
    check("任务管理器" in txt, "并给出怎么修（任务管理器 → 启动）")

    ok = [{
        "hive": "HKLM", "name": "WinWallpaperPushAgent", "enabled": True,
        "command": '"C:\\Windows\\System32\\cmd.exe" --silent',
    }]
    txt_ok = "\n".join(describe_autostart(ok))
    check("已启用" in txt_ok and "目标存在" in txt_ok, "正常时报告「已启用 / 目标存在」")
    check("登录" in txt_ok, "顺带说明「自启是登录时执行，不是开机时」")

    real = describe_autostart()
    check(bool(real), "本机自启状态可读取", real[0])


def test_install_autostart() -> None:
    """--install / --uninstall 里的自启部分（真读真写 HKCU，跑完恢复原样）。"""
    print("\n[0c] 一键安装（--install / --uninstall）")
    import winreg

    import agent

    # 先备份用户原有的设置，测试绝不能把人家环境改坏
    key_path = agent._RUN_KEY
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            backup = winreg.QueryValueEx(k, agent.RUN_VALUE_NAME)[0]
    except OSError:
        backup = None

    try:
        cmd = agent.autostart_command()
        check(cmd.endswith("--silent"), "自启命令行带 --silent", cmd)
        check(os.path.isfile(agent._command_target(cmd)),
              "自启命令行指向真实存在的程序", agent._command_target(cmd))

        ok, msg = agent.enable_autostart()
        check(ok, "enable_autostart() 写入成功", msg[:70])
        entries = agent.autostart_entries()
        names = [e["name"] for e in entries]
        check(agent.RUN_VALUE_NAME in names, "注册表里能看到这条自启项", str(names))
        entry = next(e for e in entries if e["name"] == agent.RUN_VALUE_NAME)
        check(entry["command"] == cmd, "写进去的命令行和预期一致")
        check(entry["enabled"] is True, "显示为「已启用」")

        ok2, msg2 = agent.disable_autostart()
        check(ok2, "disable_autostart() 删除成功", msg2[:70])
        left = [e["name"] for e in agent.autostart_entries()]
        check(agent.RUN_VALUE_NAME not in left, "删完注册表里就没有了", str(left))

        fw = agent.firewall_rule_ok()
        check(fw is not None, "防火墙状态可查询",
              {True: "已放行 UDP 38571", False: "未放行", None: "查不出来"}[fw])

        # 「再启动一份自己」的环境必须干净：带着 PyInstaller 的 _MEIPASS 起自己，
        # 新进程会去复用父进程的临时目录，结果起不来 + 父进程卡住不返回
        env = agent._child_env()
        check(not any(k in env for k in ("_MEIPASS", "_MEIPASS2", "_PYI_ARCHIVE_FILE")),
              "启动自己前会清掉 PyInstaller 的 _MEIPASS 标记")
        check(env.get("PYINSTALLER_RESET_ENVIRONMENT") == "1",
              "并设置 PYINSTALLER_RESET_ENVIRONMENT=1（当全新启动处理）")
        check(agent._child_env() is not env, "返回的是副本，不会改到当前进程的环境")
    finally:
        # 恢复测试前的状态
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                    winreg.KEY_SET_VALUE) as k:
                if backup is None:
                    try:
                        winreg.DeleteValue(k, agent.RUN_VALUE_NAME)
                    except OSError:
                        pass
                else:
                    winreg.SetValueEx(k, agent.RUN_VALUE_NAME, 0, winreg.REG_SZ, backup)
        except OSError:
            pass


def test_ipc() -> None:
    print("\n[1] 控制通道（winipc 命名事件）")
    check(not winipc.instance_running() or True, "instance_running() 可调用")
    check(winipc.event_name("stop").startswith("Local\\"),
          "事件名用 Local\\ 前缀（同会话可见，不需要管理员）",
          winipc.event_name("stop"))
    check(winipc.signal("bogus") is False, "非法信号名被拒绝")
    if not winipc.instance_running():
        check(winipc.signal("stop") is False, "没有实例时信号发不出去（返回 False）")
        check(winipc.wait_gone(1.0) is True, "没有实例时 wait_gone 立刻返回 True")


# ---------------------------------------------------------------- 2~6 生命周期

def test_lifecycle(tmp: str) -> None:
    print("\n[2] --status（没有实例时）")
    rc, out = run_agent("--status", "--config", os.path.join(tmp, "agent_config.json"))
    check(rc == 1, "--status 在没跑时返回 1（方便脚本判断）", f"rc={rc}")
    check("未运行" in out, "输出里明确写了「未运行」",
          out.strip().splitlines()[1] if len(out.strip().splitlines()) > 1 else out[:60])

    print("\n[3] 静默启动（--silent）")
    cfg_path = os.path.join(tmp, "agent_config.json")
    log_path = os.path.join(tmp, "agent.log")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"udp_port": TEST_PORT, "style": "填充", "keep": 1, "retry": 1,
                   "hello_interval": 0, "silent": True,
                   "wallpaper_dir": os.path.join(tmp, "wp"),
                   "log_file": log_path}, f, ensure_ascii=False)

    proc = start_agent(cfg_path)
    try:
        deadline = time.time() + 25
        while time.time() < deadline and not winipc.instance_running():
            time.sleep(0.25)
        check(winipc.instance_running(), "静默模式下进程确实起来了（控制通道可见）")

        pid = core_pid(proc)
        time.sleep(2.0)
        check(proc.poll() is None, "启动进程仍然活着", f"pid={pid}")
        check(visible_windows(pid) == 0,
              "静默模式窗口是「藏起来」而不是最小化（可见窗口数 = 0）",
              f"visible={visible_windows(pid)}")

        print("\n[4] --status（正在运行时）")
        rc, out = run_agent("--status", "--config", cfg_path)
        check(rc == 0, "--status 在跑时返回 0", f"rc={rc}")
        check("正在运行" in out, "输出里写了「正在运行」")
        check(str(TEST_PORT) in out, "输出里带着监听端口", f"UDP {TEST_PORT}")
        check(log_path in out, "输出里带着日志文件路径")
        check("静默后台" in out, "输出里说明了是静默后台运行")

        print("\n[5] 单实例（自启项 + 双击 + 快捷方式不会跑出好几个）")
        rc, out = run_agent("--silent", "--config", cfg_path)
        check(rc == 0, "重复的 --silent 启动会安静退出", f"rc={rc}")
        check("已经在" in out, "并且明确提示已经有实例在跑", out.strip().splitlines()[0] if out.strip() else "")
        check(proc.poll() is None, "原实例没有受影响，还在跑")
        check(core_pid(proc) == pid, "干活的还是原来那个进程（没有起第二个）", f"pid={pid}")

        print("\n[6] --show：设过密码时，没输密码就打不开面板")
        # 只往注册表镜像写哈希（load_blob 每次都实时读），进程不用重启 ——
        # 重启会打断后面"正常退出"那几条生命周期检查。
        import agentauth as A

        A.save_blob(A.make_hash("PanelTest2026!", iterations=2000), {}, None, True)
        try:
            check(A.is_set({}), "测试口令已生效（注册表镜像）")
            rc, out = run_agent("--show", "--config", cfg_path)
            check(rc == 0, "--show 命令本身返回成功（只是把请求转给运行中的实例）", f"rc={rc}")
            log2 = wait_for_log(log_path, "已取消", timeout=15)
            titles = window_titles(pid)
            check(not any(APP_TITLE_HINT in t for t in titles),
                  "没人输密码 → 面板保持隐藏（闸门拦住了）",
                  " / ".join(titles) or "（没有可见窗口）")
            check("已取消" in log2 or "需要密码" in log2,
                  "日志里能看出闸门生效了（超时按取消处理）",
                  [l for l in log2.splitlines() if "密码" in l][-1:] or "（没有相关日志）")
            check(proc.poll() is None, "被拒绝打开面板不会影响被控端本身，它还在跑")
        finally:
            A.save_blob(None, {}, None, True)
        check(not A.is_set({}), "测试口令已清掉（不影响后续用例）")

        print("\n[6b] --show：没设过密码时，面板照样打得开（但会警告）")
        # 老策略是拒绝打开 —— 用户实际踩过：没密码 ⇒ 面板打不开 ⇒ 没入口设密码，
        # 反复提示"请再运行一次本程序"，重跑还是打不开，彻底锁死。
        rc, out = run_agent("--show", "--config", cfg_path)
        gate_log = wait_for_log(log_path, "未设置面板密码", timeout=15)
        check("未设置面板密码" in gate_log,
              "日志里明确警告「未设置面板密码」并给出补设办法",
              [l for l in gate_log.splitlines() if "密码" in l][-1:] or "（没有相关日志）")
        check("--set-password" in gate_log, "警告里带着可照做的命令")
        # 等密码对话框超时（WPP_PROMPT_TIMEOUT=2）之后面板才真的显示；
        # 注意只看"有没有可见窗口"会被那个对话框骗到，所以看标题。
        titles2: list[str] = []
        deadline = time.time() + 10
        while time.time() < deadline:
            titles2 = window_titles(pid)
            if any(APP_TITLE_HINT in t for t in titles2):
                break
            time.sleep(0.3)
        check(any(APP_TITLE_HINT in t for t in titles2),
              "没设密码时面板能打开（不会把用户锁在外面）",
              " / ".join(titles2) or "（没有可见窗口）")
        check(proc.poll() is None, "被控端本身不受影响，还在跑")

        print("\n[7] 日志文件（静默后台唯一的排查线索）")
        check(os.path.isfile(log_path), "日志文件已经落盘", log_path)
        text = ""
        if os.path.isfile(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        check("启动" in text, "日志里有启动记录")
        check(str(TEST_PORT) in text, "日志里有监听端口", f"UDP {TEST_PORT}")

        print("\n[8] --stop（关不掉的问题）")
        rc, out = run_agent("--stop", "--config", cfg_path, timeout=40)
        check(rc == 0, "--stop 返回成功", f"rc={rc} {out.strip().splitlines()[0] if out.strip() else ''}")
        gone = winipc.wait_gone(6.0)
        check(gone, "控制通道已经消失（实例退出）")
        try:
            proc.wait(timeout=15)
            exited = True
        except subprocess.TimeoutExpired:
            exited = False
        check(exited, "进程真的退出了（不是只停了功能）", f"returncode={proc.poll()}")
        check(not os.path.isfile(state_file()), "状态文件被清掉了（正常退出）")

        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        check("退出" in text, "日志里留下了退出记录")

        rc, out = run_agent("--stop", "--config", cfg_path)
        check(rc == 0 and "没有正在运行" in out, "再次 --stop 会提示没有实例在跑")

        print("\n[9] --silent 之后可以正常再启动（不是一次性的）")
        proc2 = start_agent(cfg_path)
        try:
            deadline = time.time() + 25
            while time.time() < deadline and not winipc.instance_running():
                time.sleep(0.25)
            check(winipc.instance_running(), "停止之后还能重新启动")
            rc, _out = run_agent("--stop", "--config", cfg_path, timeout=40)
            check(rc == 0, "第二次也能正常 --stop")
        finally:
            _cleanup(proc2)
    finally:
        _cleanup(proc)


def _cleanup(proc: subprocess.Popen) -> None:
    """无论如何都别把测试用的被控端留在后台。"""
    try:
        if winipc.instance_running():
            winipc.signal("stop")
            winipc.wait_gone(5.0)
    except Exception:
        pass
    try:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    except Exception:
        pass


def main() -> int:
    print("=" * 68)
    print("  Win 壁纸推送 —— 被控端启停控制自测（不换壁纸）")
    print("=" * 68)

    import agent  # noqa: F401  确认能导入（语法 / 依赖问题能在这里暴露）

    test_bat_files()
    test_autostart_report()
    test_install_autostart()
    test_ipc()

    if winipc.instance_running():
        print()
        skip("完整启停流程", "本机已经有被控端在运行；为了避免干扰它，本次跳过。"
                            "先执行 WallpaperAgent.exe --stop 再跑这个测试")
    else:
        with tempfile.TemporaryDirectory(prefix="wpp_ctl_") as tmp:
            test_lifecycle(tmp)

    passed = sum(1 for ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("\n" + "=" * 68)
    print(f"  结果：{passed} 项通过，{failed} 项失败")
    print("=" * 68)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass
    sys.exit(main())
