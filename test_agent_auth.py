#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""被控端口令自测（哈希 / 存放 / 闸门 / 真起进程验证）

针对的需求：
    * 第一次运行提示设置密码
    * 每次打开被控端面板前要输入密码
    * 停止、卸载（删除）也要输入密码

重点验证「闸门不能形同虚设」：
    1. 哈希与校验（加盐、错密码、坏数据一律不通过）
    2. 存放位置优先级（注册表镜像 > 配置文件；机器级最优先）
    3. 有人只删掉配置文件时，注册表镜像仍然拦得住
    4. **无人值守环境（没有输入）必须拒绝操作，而且绝不能卡住等输入**
    5. 密码错 3 次拒绝
    6. 端到端：真起一个被控端，`--stop` 没密码时停不掉，给对密码才停得掉

运行： python test_agent_auth.py
退出码 0 = 全过。测试会临时写注册表，跑完恢复原样。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

import agentauth as A

RESULTS: list[tuple[bool, str]] = []
TEST_PW = "Lan2026!"
# 命令行开关用另一个口令：清掉的时候要重新输一遍，混着用容易看不出是哪一步生效
CLI_PW = "CliTest2026!"
TEST_PORT = 39471


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


def run_py(code: str, *args: str, stdin_data: str | None = None,
           timeout: float = 90.0) -> tuple[int, str]:
    """跑一小段脚本（子进程），可控地喂 stdin 或不喂。"""
    proc = subprocess.run(
        [sys.executable, "-c", code, *args],
        input=stdin_data,
        stdin=None if stdin_data is not None else subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, cwd=os.getcwd(),
        creationflags=subprocess.CREATE_NO_WINDOW,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


REQUIRE_SNIPPET = (
    "import sys, os, json; sys.path.insert(0, os.getcwd());"
    "import agentauth as A;"
    "cfg=json.load(open(sys.argv[1],encoding='utf-8'));"
    "ok,msg=A.require(cfg,'停止被控端');"
    "print('RESULT', ok);"
    "sys.exit(0 if ok else 1)"
)


# ---------------------------------------------------------------- 1. 哈希

def test_hash() -> None:
    print("\n[1] 口令哈希（PBKDF2-HMAC-SHA256 加盐）")
    blob = A.make_hash(TEST_PW, iterations=2000)
    check(blob.get("algo") == "pbkdf2_sha256", "算法标记正确", str(blob.get("algo")))
    check(bool(blob.get("salt")) and bool(blob.get("hash")),
          "同时存了 salt 和 hash（不是明文）")
    check(TEST_PW not in json.dumps(blob), "文件里看不到明文口令")
    check(A.verify(TEST_PW, blob) is True, "正确口令通过")
    check(A.verify(TEST_PW + "x", blob) is False, "错误口令拒绝")
    check(A.verify("", blob) is False, "空口令拒绝")
    check(A.verify(TEST_PW, None) is False, "没有哈希时拒绝（默认拒绝，不是默认放行）")
    check(A.verify(TEST_PW, {}) is False, "空字典拒绝")
    check(A.verify(TEST_PW, {"algo": A.ALGO}) is False, "缺字段的坏数据拒绝")
    check(A.verify(TEST_PW, {"algo": "md5", "salt": blob["salt"],
                             "hash": blob["hash"]}) is False, "算法不匹配拒绝")

    same_pw = A.make_hash(TEST_PW, iterations=2000)
    check(same_pw["hash"] != blob["hash"], "同一个口令两次生成的哈希不同（盐不同）")
    check(A.verify(TEST_PW, same_pw) is True, "但都能校验通过")

    unicode_pw = "中文口令★"
    check(A.verify(unicode_pw, A.make_hash(unicode_pw, iterations=1000)) is True,
          "中文 / 特殊字符口令也能用")


# ---------------------------------------------------------------- 2. 存放与优先级

def test_storage(tmp: str) -> None:
    print("\n[2] 口令存放位置与优先级")
    cfg_path = os.path.join(tmp, "agent_config.json")

    # 备份注册表镜像，跑完恢复
    saved_reg = A.registry_blob()

    # 这一节只测「配置文件 vs 注册表镜像」的优先级，所以先假装机器级位置不可用。
    # 注意 C:\ProgramData 默认允许普通用户创建子目录（谁建的目录谁有完全控制权），
    # 所以「机器级只有管理员能改」这件事**必须由安装程序设好 ACL**，不能想当然。
    real_machine_blob = A.machine_blob
    real_machine_writable = A.machine_writable
    A.machine_blob = lambda: None                 # type: ignore[assignment]
    A.machine_writable = lambda: False            # type: ignore[assignment]

    try:
        cfg: dict = {}
        blob = A.make_hash(TEST_PW, iterations=2000)

        def write_cfg(c: dict) -> None:
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({k: v for k, v in c.items() if not k.startswith("_")}, f)

        places = A.save_blob(blob, cfg, write_cfg, True)
        check(bool(places), "写入成功", "、".join(places))
        check(A.is_set(cfg), "写完之后 is_set() 为真")
        got, source = A.load_blob(cfg)
        check(got == blob, "能从用户可写位置读回来", source)
        check("注册表" in source,
              "两处都写了时优先用注册表镜像（比配置文件更难被随手改）", source)
        check(json.load(open(cfg_path, encoding="utf-8")).get("password") is not None,
              "配置文件里确实写了 password 段")

        # 只有配置文件时（写不了注册表），回落到配置文件
        real_registry_blob = A.registry_blob
        A.registry_blob = lambda: None            # type: ignore[assignment]
        try:
            got_c, source_c = A.load_blob(cfg)
            check(got_c == blob and "配置文件" in source_c,
                  "只剩下配置文件时就用它", source_c)
        finally:
            A.registry_blob = real_registry_blob  # type: ignore[assignment]

        # 模拟「只删配置文件」——注册表镜像还在，必须仍然拦得住
        cfg2: dict = {}
        got2, source2 = A.load_blob(cfg2)
        check(got2 is not None, "配置被删空后仍能读到口令（注册表镜像兜底）", source2)
        check("注册表" in source2, "来源变成注册表镜像", source2)
        ok, _msg = A.require(cfg2, "停止被控端", attempts=1)
        check(not ok, "配置被删空后依然要求密码（删文件绕不过去）")

        # 两处不一致时以注册表为准（配置文件是用户最容易改的）
        cfg3 = {"password": A.make_hash("别人偷偷改的", iterations=2000)}
        got3, source3 = A.load_blob(cfg3)
        check(got3 == blob, "配置文件被改成别的口令时，仍以注册表那份为准")
        check("不一致" in source3, "并在来源说明里点出来", source3)

        # 清除
        A.save_blob(None, cfg3, write_cfg, True)
        check(not A.is_set({}), "清除后 is_set() 为假")

        # 机器级存在时必须以它为准（用注入的方式测策略，不碰真实文件系统）
        machine_only = A.make_hash("管理员设的", iterations=2000)
        A.machine_blob = lambda: machine_only     # type: ignore[assignment]
        got4, source4 = A.load_blob({"password": blob})
        check(got4 == machine_only, "机器级口令存在时以它为准（配置文件那份被忽略）",
              source4)
        check(A.verify("管理员设的", got4) and not A.verify(TEST_PW, got4),
              "校验走的是机器级那份")
    finally:
        A.machine_blob = real_machine_blob        # type: ignore[assignment]
        A.machine_writable = real_machine_writable  # type: ignore[assignment]
        A.save_blob(saved_reg, {}, None, True)


# ---------------------------------------------------------------- 3. 闸门

def test_gates(tmp: str) -> None:
    print("\n[3] 口令闸门（没有人值守时不能卡住）")
    cfg_path = os.path.join(tmp, "gate_config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"password": A.make_hash(TEST_PW, iterations=2000)}, f)

    rc, out = run_py(REQUIRE_SNIPPET, cfg_path, stdin_data=TEST_PW + "\n")
    check(rc == 0 and "RESULT True" in out, "给对密码 → 放行", out.splitlines()[-1])

    t0 = time.time()
    rc, out = run_py(REQUIRE_SNIPPET, cfg_path, stdin_data=None, timeout=30)
    cost = time.time() - t0
    check(rc != 0 and "RESULT False" in out, "完全没有输入 → 拒绝", out.splitlines()[-1])
    check(cost < 15, "而且立刻返回，不会卡住等输入", f"{cost:.1f} 秒")

    rc, out = run_py(REQUIRE_SNIPPET, cfg_path,
                     stdin_data="bad1\nbad2\nbad3\n", timeout=60)
    check(rc != 0 and "RESULT False" in out, "密码连错 3 次 → 拒绝",
          [l for l in out.splitlines() if "密码不正确" in l][:1] or out.splitlines()[-1])

    rc, out = run_py(REQUIRE_SNIPPET, cfg_path, stdin_data="\n\n", timeout=60)
    check(rc != 0 and "RESULT False" in out, "直接回车（空密码）→ 拒绝")

    # 没设密码时放行，但必须明确警告（否则用户被锁在外面，连设密码的机会都没有）
    plain = os.path.join(tmp, "plain_config.json")
    with open(plain, "w", encoding="utf-8") as f:
        json.dump({}, f)
    rc, out = run_py(REQUIRE_SNIPPET, plain, stdin_data=None, timeout=30)
    check(rc == 0, "从未设过密码时不拦（但会提示风险）", out.splitlines()[-1])


# ---------------------------------------------------------------- 3b. 面板闸门与对话框

def test_panel_gate_relaxed() -> None:
    """没设过密码时，面板不能被锁死（否则连设置密码的入口都没有）。

    这是用户实际踩过的坑：第一次运行 → 想打开面板 → 弹"请再运行一次本程序设置
    密码" → 重跑一次还是同一个（看不见的）对话框 → 永远打不开面板，而壁纸和
    通知一切正常。
    """
    import agentauth as A

    print("\n[3b] 面板闸门：没密码也要进得去")
    cfg: dict = {}
    real_ask = A.ask
    real_is_set = A.is_set
    try:
        # （a）用户在设置密码的对话框上点了取消
        A.ask = lambda *a, **k: None                     # type: ignore[assignment]
        allow, msg = A.unlock_panel_policy(cfg, prefer="gui")
        check(allow, "取消设置密码 → 面板仍然放行（不会把人锁在外面）", msg[:80])
        check("--set-password" in msg, "并且告诉他怎么补上密码", msg[:80])

        # （b）没设密码，而且这台机器上根本没有可用的输入界面
        ok = A.unlock_panel_policy(cfg, prefer="gui")
        check(ok[0], "没有任何输入界面时也放行（不能死循环）")

        # （c）设置成功 → 放行，并提示下次要输密码
        A.ask = lambda *a, **k: "testpass123"             # type: ignore[assignment]
        A.is_set = lambda c=None: False                   # type: ignore[assignment]
        allow2, msg2 = A.unlock_panel_policy(cfg, prefer="gui")
        check(allow2, "设置密码成功后放行", msg2[:80])
        check("下次打开面板" in msg2, "并说明下次打开就要输密码", msg2[:80])

        # （d）设过密码之后：取消/输错一律拒绝
        A.is_set = lambda c=None: True                    # type: ignore[assignment]
        A.ask = lambda *a, **k: None                      # type: ignore[assignment]
        allow3, msg3 = A.unlock_panel_policy(cfg, prefer="gui")
        check(not allow3, "设过密码后，取消输入 → 拒绝", msg3[:80])
    finally:
        A.ask = real_ask                                  # type: ignore[assignment]
        A.is_set = real_is_set                            # type: ignore[assignment]
    # 把 (c) 写进去的口令清掉，别污染本机
    A.save_blob(None, cfg, None, True)


def test_dialog_visible_when_hidden() -> None:
    """面板隐藏时，密码对话框必须**真的显示出来**。

    回归用例：`Toplevel + transient(root)` 在 root 处于 withdraw（静默后台的
    被控端就是这个状态）时，Windows 上根本不显示 —— 实测 viewable=False、
    尺寸 1×1，而且 deiconify/lift/-topmost/focus_force 都救不回来。
    用户看不到输入框 → 300 秒后超时算"取消" → 被判成"没设置密码" → 面板打不开。
    """
    print("\n[3c] 面板隐藏时的密码对话框可见性")
    try:
        import tkinter as tk
    except Exception:
        skip("对话框可见性", "没有 tkinter")
        return
    try:
        import agentauth as A

        root = tk.Tk()
    except Exception as e:
        skip("对话框可见性", f"没有可用的图形界面：{e}")
        return

    seen: dict = {}
    try:
        root.withdraw()
        root.update()
        real_wait = root.wait_window

        def fake_wait(win):
            win.update_idletasks()
            win.update()
            root.update()
            seen["viewable"] = bool(win.winfo_viewable())
            seen["mapped"] = bool(win.winfo_ismapped())
            seen["size"] = (win.winfo_width(), win.winfo_height())
            win.destroy()

        root.wait_window = fake_wait                # type: ignore[assignment]
        A._ask_gui("设置被控端密码", "第一次运行，请设置被控端密码", confirm=True)
        root.wait_window = real_wait                # type: ignore[assignment]
        check(seen.get("viewable") is True,
              "父窗口隐藏时，对话框仍然可见（不是 1×1 的隐形框）", str(seen))
        check(seen.get("size", (1, 1))[0] > 50,
              "而且有正常尺寸（用户能看见也能输入）", str(seen.get("size")))
    finally:
        try:
            root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------- 5. 命令行开关

def test_cli_switches(tmp: str) -> None:
    """每个命令行开关都要真的能跑（不是"看起来还在"）。

    回归用例：`cmd_set_password` 的函数定义曾被整段弄丢（只剩一段悬空 docstring，
    藏在 cmd_test_toast 的 return 后面），于是 `--set-password` 一跑就
    NameError —— 用户表现为"配不了密码、面板打不开"，而且源码里看着一切正常。
    """
    print("\n[5] 命令行开关（--set-password / --clear 真跑一遍）")
    import agent

    wanted = ["cmd_install", "cmd_uninstall", "cmd_stop", "cmd_show", "cmd_status",
              "cmd_set_password", "cmd_test_toast", "cmd_install_toast_module",
              "cmd_allow_urgent"]
    missing = [n for n in wanted if not hasattr(agent, n)]
    check(not missing, f"命令处理函数都存在（{len(wanted)} 个）",
          "缺失：" + "、".join(missing) if missing else "含 --set-password")

    cfg_path = os.path.join(tmp, "cli_config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"udp_port": 38599}, f)
    try:
        # 真用命令行设置密码（管道喂两次，模拟 `echo pw| exe --set-password`）
        proc = subprocess.run(
            [sys.executable, "agent.py", "--set-password", "--config", cfg_path],
            input=CLI_PW + "\n" + CLI_PW + "\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=os.getcwd(),
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        check(proc.returncode == 0 and "已设置密码" in out,
              "--set-password 能跑通（不会再 NameError）",
              [ln for ln in out.splitlines() if "密码" in ln][:1] or out[-120:])
        check("NameError" not in out, "输出里没有 NameError 之类的东西")

        rc, out2 = run_py(
            "import sys, os; sys.path.insert(0, os.getcwd());"
            "import agent, agentauth as A;"
            "print('IS_SET', A.is_set({}))",
        )
        check("IS_SET True" in out2, "设完之后确实生效了", out2.strip()[:80])

        # 再用命令行清掉
        proc2 = subprocess.run(
            [sys.executable, "agent.py", "--set-password", "--clear",
             "--config", cfg_path],
            input=CLI_PW + "\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=os.getcwd(),
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        out3 = (proc2.stdout or "") + (proc2.stderr or "")
        check(proc2.returncode == 0 and "取消" in out3,
              "--set-password --clear 也能跑通",
              [ln for ln in out3.splitlines() if "密码" in ln][:1] or out3[-120:])
    finally:
        # 无论如何都别在本机留下测试口令
        try:
            import agentauth as A

            A.save_blob(None, {}, None, True)
        except Exception:
            pass


# ---------------------------------------------------------------- 6. 端到端

def test_end_to_end(tmp: str) -> None:
    print("\n[4] 端到端：真起一个被控端，看 --stop 拦不拦得住")
    import winipc

    if winipc.instance_running():
        skip("端到端启停", "本机已经有一个被控端在跑，先 --stop 再跑本测试")
        return

    cfg_path = os.path.join(tmp, "e2e_config.json")
    log_path = os.path.join(tmp, "e2e.log")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"udp_port": TEST_PORT, "style": "填充", "keep": 1, "retry": 1,
                   "hello_interval": 0, "silent": True,
                   "wallpaper_dir": os.path.join(tmp, "wp"),
                   "log_file": log_path,
                   "password": A.make_hash(TEST_PW, iterations=2000)}, f,
                  ensure_ascii=False)

    proc = subprocess.Popen(
        [sys.executable, "agent.py", "--config", cfg_path, "--port", str(TEST_PORT)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.time() + 25
        while time.time() < deadline and not winipc.instance_running():
            time.sleep(0.25)
        if not check(winipc.instance_running(), "被控端已起来（带密码运行）"):
            return

        print("\n   4.1 --stop 不给密码")
        rc, out = run_py(
            "import sys, os, json; sys.path.insert(0, os.getcwd());"
            "import agent;"
            "cfg=json.load(open(sys.argv[1],encoding='utf-8'));"
            "cfg['_config_read_path']=sys.argv[1];"
            "sys.exit(agent.cmd_stop(cfg))",
            cfg_path, stdin_data=None, timeout=60)
        check(rc != 0, "--stop 没有密码时返回失败", f"rc={rc}")
        check(winipc.instance_running(), "被控端**仍在运行**（没被停掉）")

        print("\n   4.2 --stop 给对密码")
        rc, out = run_py(
            "import sys, os, json; sys.path.insert(0, os.getcwd());"
            "import agent;"
            "cfg=json.load(open(sys.argv[1],encoding='utf-8'));"
            "cfg['_config_read_path']=sys.argv[1];"
            "sys.exit(agent.cmd_stop(cfg))",
            cfg_path, stdin_data=TEST_PW + "\n", timeout=90)
        check(rc == 0, "--stop 给了正确密码后成功", f"rc={rc}")
        check(winipc.wait_gone(8.0), "被控端真的退出了")
    finally:
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
    print("  Win 壁纸推送 —— 被控端口令自测")
    print("=" * 68)

    test_hash()
    with tempfile.TemporaryDirectory(prefix="wpp_auth_") as tmp:
        test_storage(tmp)
        test_gates(tmp)
        test_panel_gate_relaxed()
        test_dialog_visible_when_hidden()
        test_cli_switches(tmp)
        test_end_to_end(tmp)

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
