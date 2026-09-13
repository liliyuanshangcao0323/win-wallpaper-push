# -*- coding: utf-8 -*-
"""被控端口令：第一次运行设密码，开面板 / 停止 / 卸载都要验密码。

设计要点
--------
* **只存加盐哈希**（PBKDF2-HMAC-SHA256），不存明文；比较用 compare_digest。
* 三个存放位置，按可信度排序，读到哪个用哪个：

  1. `C:\\ProgramData\\WinWallpaperPush\\secure\\agent_password.json`
     —— 机器级，**只有管理员写得进去**（文件夹 ACL 由 MSI / 管理员
     `--set-password` 设好）。存在就以它为准，普通用户既改不了也删不掉。
  2. 配置文件里的 `password` 段 —— 用户可写，方便免安装版。
  3. `HKCU\\Software\\WinWallpaperPush\\Agent` 的注册表镜像 ——
     防止有人「只删配置」就绕过。

  三者不一致时以更可信的为准，并在日志里写一条告警（可能是有人改过文件）。

能力边界（必须说清楚，免得当成安全产品）
----------------------------------------
这套口令是**防手欠级别**的：拦住同机普通用户随手点开面板、点退出、卸载。
它**不是安全边界** —— 同一台机器上的本地用户仍然可以 `taskkill` 结束进程、
直接删掉程序目录。要把这些也管住，得靠 Windows 权限 / ACL / 应用白名单，
那不是被控端自己能做主的。命令行里也有同样的说明。

非 Windows 平台一律降级成「未设置密码」，方便在多平台上跑测试。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys

IS_WINDOWS = sys.platform == "win32"

ALGO = "pbkdf2_sha256"
ITERATIONS = 200_000          # 约 0.1~0.2 秒，够挡住暴力猜，又不至于让用户等
SALT_BYTES = 16
MIN_LENGTH = 4                # 别搞成"必须大小写加符号"，现场会被骂

APP_DIR_NAME = "WinWallpaperPush"
SECURE_DIR_NAME = "secure"
PASSWORD_FILE = "agent_password.json"
REG_KEY = r"Software\WinWallpaperPush\Agent"
REG_VALUE = "PasswordHash"
CONFIG_KEY = "password"


# ================================================================ 哈希

def make_hash(password: str, iterations: int = ITERATIONS,
              salt: bytes | None = None) -> dict:
    """把明文口令变成可存盘的哈希块。"""
    salt = salt or secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 max(1, int(iterations)))
    return {
        "algo": ALGO,
        "iter": int(iterations),
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
    }


def verify(password: str, blob: dict | None) -> bool:
    """校验明文口令。blob 不合法时返回 False（绝不「解析失败就放行」）。"""
    if not blob or not isinstance(blob, dict):
        return False
    try:
        if blob.get("algo") != ALGO:
            return False
        salt = base64.b64decode(blob.get("salt") or "")
        want = base64.b64decode(blob.get("hash") or "")
        iterations = int(blob.get("iter") or ITERATIONS)
    except Exception:
        return False
    if not salt or not want:
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                              max(1, iterations))
    return hmac.compare_digest(got, want)


# ================================================================ 存放位置

def machine_dir() -> str:
    base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return os.path.join(base, APP_DIR_NAME, SECURE_DIR_NAME)


def machine_path() -> str:
    return os.path.join(machine_dir(), PASSWORD_FILE)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def machine_blob() -> dict | None:
    """机器级（管理员才能写）的口令块。"""
    if not IS_WINDOWS:
        return None
    return _read_json(machine_path())


def config_blob(cfg: dict | None) -> dict | None:
    blob = (cfg or {}).get(CONFIG_KEY)
    return blob if isinstance(blob, dict) else None


def registry_blob() -> dict | None:
    """HKCU 里的镜像。"""
    if not IS_WINDOWS:
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            raw, _t = winreg.QueryValueEx(k, REG_VALUE)
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_blob(cfg: dict | None = None) -> tuple[dict | None, str]:
    """按可信度取出当前生效的口令块，返回 (blob, 来源说明)。

    优先级：机器级 > 注册表镜像 > 配置文件。
    后面两个都是用户可写的，谁在前谁在后其实防的是「只删一处」的人；
    两边都在但不一致时用注册表那份，并在说明里点出来。
    """
    mach = machine_blob()
    conf = config_blob(cfg)
    reg = registry_blob()

    if mach:
        if machine_protected():
            note = "机器级 ProgramData\\secure（已锁：普通用户只读）"
        else:
            note = ("机器级 ProgramData\\secure（未锁：这个文件普通用户还能改 —— "
                    "用管理员身份重设一次才真的锁住）")
        if (conf and conf != mach) or (reg and reg != mach):
            note += "；注意：用户可写位置的口令与它不一致，已按机器级为准"
        return mach, note
    if reg:
        note = "注册表镜像（HKCU）"
        if conf and conf != reg:
            note += "；注意：配置文件里的口令与它不一致，已按注册表为准"
        return reg, note
    if conf:
        return conf, "配置文件"
    return None, "未设置"


def is_set(cfg: dict | None = None) -> bool:
    return load_blob(cfg)[0] is not None


def is_elevated() -> bool:
    """当前进程是不是管理员权限（提升过的令牌）。"""
    if not IS_WINDOWS:
        try:
            return os.geteuid() == 0        # 仅用于非 Windows 上的测试
        except Exception:
            return False
    try:
        import ctypes

        return bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
    except Exception:
        return False


def machine_protected() -> bool:
    """机器级口令文件是不是真的「普通用户改不动」。

    返回 False 的情形包括：文件不存在，或者当前这个普通用户还能写它
    （说明它是被普通用户建出来的，属主就是他，随时能改回去，等于没锁）。
    """
    if not IS_WINDOWS or not os.path.isfile(machine_path()):
        return False
    if is_elevated():
        return True                  # 管理员本来就该写得动，不能据此判断
    try:
        with open(machine_path(), "a", encoding="utf-8"):
            pass
        return False                 # 我们还能写 → 没锁住
    except OSError:
        return True


def machine_writable() -> bool:
    """当前进程能不能往机器级目录里写（管理员才行；写不进去不算错）。"""
    if not IS_WINDOWS:
        return False
    path = machine_path()
    probe = path + ".probe"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(probe)
        return True
    except Exception:
        try:
            os.remove(probe)
        except Exception:
            pass
        return False


def _harden_machine_file(path: str) -> None:
    """把机器级口令文件锁成「管理员可写、其他用户只读」。

    为什么必须显式设：`C:\\ProgramData` 默认允许普通用户创建子目录，而且
    **谁建的目录谁就是属主**（属主随时能改回权限）—— 所以「放在 ProgramData
    就安全」是想当然，得真的设 ACL，而且必须由管理员/SYSTEM 来建。
    用内置 SID（*S-1-5-32-544 = Administrators，*S-1-5-32-545 = Users）
    而不是 "Administrators"/"Users" 名字，避免中英文系统上的本地化差异。
    """
    if not IS_WINDOWS:
        return
    try:
        from netutil import run_hidden

        run_hidden(["icacls", path, "/inheritance:r",
                    "/grant:r", "*S-1-5-32-544:F", "*S-1-5-32-545:R"], timeout=20)
    except Exception:
        pass


def save_blob(blob: dict | None, cfg: dict, write_config,
              write_registry) -> list[str]:
    """把口令块写进各处。blob=None 表示删除。

    机器级位置能写就写，写完立刻用 icacls 锁成「管理员可写、其他用户只读」。
    不过要说清楚强度差别：由管理员（MSI / 提升过的命令行）建的文件，属主是
    Administrators，普通用户拿不回来；由普通用户自己建的文件虽然也被 ACL 挡住，
    但属主是他本人，真要改还是能改 —— 所以标签里会区分「已锁 / 未锁」。
    返回写成功的位置说明列表。
    """
    written: list[str] = []

    if machine_writable():
        try:
            if blob is None:
                if os.path.isfile(machine_path()):
                    os.remove(machine_path())
                    written.append("机器级文件（已删除）")
            else:
                with open(machine_path(), "w", encoding="utf-8") as f:
                    json.dump(blob, f, ensure_ascii=False, indent=2)
                _harden_machine_file(machine_path())
                written.append("机器级文件（普通用户只能读）")
        except OSError:
            pass

    if cfg is not None:
        if blob is None:
            cfg.pop(CONFIG_KEY, None)
        else:
            cfg[CONFIG_KEY] = blob
        if write_config:
            try:
                write_config(cfg)
                written.append("配置文件")
            except Exception:
                pass

    if IS_WINDOWS and write_registry:
        import winreg

        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                if blob is None:
                    try:
                        winreg.DeleteValue(k, REG_VALUE)
                    except OSError:
                        pass
                else:
                    winreg.SetValueEx(k, REG_VALUE, 0, winreg.REG_SZ,
                                      json.dumps(blob))
                written.append("注册表镜像")
        except OSError:
            pass
    return written


def locked_to_machine() -> bool:
    """口令被管理员放进了机器级位置：非管理员改不了。"""
    return IS_WINDOWS and machine_blob() is not None and not machine_writable()


# ================================================================ 口令提示

def _stdin_is_real_console() -> bool:
    """stdin 后面是不是一个**真正的控制台**。

    不能用 `isatty()` 判断：Windows 上 NUL 设备（`stdin=DEVNULL`、计划任务、
    服务里常见的）也会被报告成 tty，于是 getpass 会跑去读控制台按键，
    结果是**永远卡住等输入** —— 无人值守环境里这是致命的。
    这里直接问内核：这个句柄支不支持控制台模式。
    """
    if sys.stdin is None:
        return False
    if IS_WINDOWS:
        try:
            import ctypes
            import msvcrt

            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            mode = ctypes.c_uint32()
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            return bool(kernel32.GetConsoleMode(ctypes.c_void_p(handle),
                                                ctypes.byref(mode)))
        except Exception:
            return False
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _console_usable() -> bool:
    """能在这个进程里问用户要输入吗（控制台 / 管道）。"""
    try:
        if sys.stdin is None:
            return False
        sys.stdin.fileno()
        return True
    except Exception:
        return False


def _ask_console(prompt: str) -> str | None:
    """控制台问密码。

    * 真控制台 → getpass，不回显。
    * 管道/重定向（`echo 密码 | WallpaperAgent.exe --stop` 这种自动化）→
      读一行，支持脚本；读不到（EOF）就返回 None。
    * 读到 EOF 一律返回 None：无人值守环境下**绝不能卡在那里等输入**，
      拿不到密码就拒绝操作，让调用方给出明确提示。
    """
    try:
        if sys.stdin is None:
            return None
        if _stdin_is_real_console():
            try:
                import getpass

                value = getpass.getpass(prompt + "：")
            except Exception:
                value = input(prompt + "：")
        else:
            try:
                sys.stdout.write(prompt + "：")
                sys.stdout.flush()
            except Exception:
                pass
            value = sys.stdin.readline()
            if not value:
                return None
            # 管道这条路要 strip：cmd 里 `echo 密码| 程序` 送到的是 "密码 "（带一个
            # 尾空格，这是 cmd 的怪癖），不 strip 的话脚本里永远对不上口令。
            # 交互式那条路（getpass）不动它，用户真敲了尾空格就按原样算。
            value = value.strip()
        return value if value != "" else None
    except (EOFError, KeyboardInterrupt):
        return None
    except Exception:
        return None


def prompt_timeout() -> float:
    """弹窗等多久算「没人应答」（秒）；0 = 一直等。

    为什么要有超时：被控端是**后台进程**，如果它弹出一个模态框而当前没人
    坐在机器前（锁屏、远程会话断开、自动化脚本），那个框会一直挂着 ——
    连 `--stop` 都会被卡住（UI 线程在等输入）。超时后按「取消」处理：
    操作被拒绝，但进程不会僵在那里。

    环境变量 `WPP_PROMPT_TIMEOUT`（秒）可以覆盖，测试和自动化用它；
    它只能让等待变短，**不能绕过密码**（超时=拒绝，不是放行）。
    """
    raw = os.environ.get("WPP_PROMPT_TIMEOUT")
    if raw is None:
        return 300.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 300.0


def automated() -> bool:
    """是不是被测试/自动化驱动（这种场景下不要弹模态提示框）。"""
    return os.environ.get("WPP_PROMPT_TIMEOUT") is not None


def _make_dialog(title: str):
    """造一个「一定看得见」的密码对话框，返回 (窗口, 父窗口) 或 (None, None)。

    为什么不能无脑 `transient(root)`：被控端平时是**隐藏**着跑的（静默后台 /
    自启动 / 用户点了"隐藏"），而 Windows 上「owner 没有映射」的对话框根本不会
    显示出来 —— 实测：父窗口 withdraw 时，`Toplevel + transient(root)` 的
    `winfo_viewable()` 是 False、尺寸 1×1，`deiconify()/lift()/-topmost/focus_force()`
    全都救不回来；只要**不设** transient 就一切正常。

    这个坑的后果很严重：用户看不到输入框，什么都没法输，等 prompt_timeout()
    （默认 300 秒）超时后被判成"取消" → 弹出"还没有设置密码，面板不会打开" →
    照着提示重跑一次，还是同一个看不见的框 —— 于是永远设不上密码、面板永远打不开，
    而壁纸和通知一切正常（后台实例在跑）。
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return None, None
    try:
        root = getattr(tk, "_default_root", None)
        win = tk.Toplevel(root) if root is not None else tk.Toplevel()
        win.title(title)
        win.resizable(False, False)
        # 只有父窗口真的显示着才设 owner；隐藏状态下设了就等于把自己藏起来
        try:
            if root is not None and root.winfo_viewable():
                win.transient(root)
        except Exception:
            pass
        win.attributes("-topmost", True)
        win.lift()
        win.focus_force()
        # 置顶只是"确保跑到最前面"，别一直压着别的窗口
        try:
            win.after(800, lambda: _drop_topmost(win))
        except Exception:
            pass
        return win, root
    except Exception:
        return None, None


def _drop_topmost(win) -> None:
    try:
        win.attributes("-topmost", False)
    except Exception:
        pass


def _ask_gui(title: str, prompt: str, confirm: bool = False,
             first: bool = False) -> str | None:
    """用一个小对话框问密码。没有 Tk 时返回 None。"""
    try:
        from tkinter import ttk
    except Exception:
        return None

    result: dict[str, str | None] = {"value": None}
    own_root = False
    import tkinter as tk

    root = getattr(tk, "_default_root", None)
    try:
        if root is None:
            root = tk.Tk()
            root.withdraw()
            own_root = True
        win, _parent = _make_dialog(title)
        if win is None:
            return None
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=prompt, wraplength=360, justify="left").pack(anchor="w")
        entry = ttk.Entry(body, show="*", width=28)
        entry.pack(fill="x", pady=(8, 0))
        entry.focus_set()
        entry2 = None
        if confirm:
            ttk.Label(body, text="再输一次确认：").pack(anchor="w", pady=(8, 0))
            entry2 = ttk.Entry(body, show="*", width=28)
            entry2.pack(fill="x")

        def ok(_e=None):
            value = entry.get()
            if entry2 is not None and entry2.get() != value:
                ttk.Label(body, text="两次输入不一致，请重新输入",
                          foreground="#ff6b6b").pack(anchor="w", pady=(6, 0))
                entry.delete(0, "end")
                entry2.delete(0, "end")
                return
            result["value"] = value
            win.destroy()

        def cancel(_e=None):
            result["value"] = None
            win.destroy()

        row = ttk.Frame(body)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text="确定", command=ok).pack(side="right")
        ttk.Button(row, text="取消", command=cancel).pack(side="right", padx=8)
        win.bind("<Return>", ok)
        win.bind("<Escape>", cancel)
        win.protocol("WM_DELETE_WINDOW", cancel)
        # 没人应答就按取消处理：模态框不能把后台进程永远卡住
        timeout = prompt_timeout()
        if timeout > 0:
            win.after(int(timeout * 1000), cancel)
        win.update_idletasks()
        # 居中到屏幕
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{(sw - win.winfo_reqwidth()) // 2}"
                     f"+{(sh - win.winfo_reqheight()) // 3}")
        # 抓键盘之前先确认它真的显示出来了：对一个看不见的窗口 grab_set()
        # 会把整个程序的输入都锁死（用户只看到"程序没反应"）。
        try:
            if win.winfo_viewable():
                win.grab_set()
        except Exception:
            pass
        root.wait_window(win)
        return result["value"]
    except Exception:
        return None
    finally:
        if own_root and root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def ask(prompt: str, confirm: bool = False, prefer: str = "auto",
        title: str = "被控端口令") -> str | None:
    """问用户要一个口令；返回 None 表示取消 / 没有可用的输入界面。

    prefer:
      * "auto"    —— 能用控制台就用控制台（命令行里不会闪窗口），否则弹对话框
      * "console" —— 只走控制台（拿不到就 None，绝不弹窗）
      * "gui"     —— 只弹对话框（面板流程用；不能偷偷跑到控制台去问）
    """
    if prefer != "gui" and _console_usable():
        value = _ask_console(prompt)
        if value is None:
            return None
        if confirm:
            again = _ask_console("再输一次确认")
            if again != value:
                return None
        return value
    if prefer in ("auto", "gui"):
        return _ask_gui(title, prompt, confirm=confirm)
    return None


def _has_display() -> bool:
    """有没有可用的图形界面（能弹窗）。"""
    if not IS_WINDOWS and not os.environ.get("DISPLAY"):
        return False
    try:
        import tkinter as tk
    except Exception:
        return False
    try:
        root = getattr(tk, "_default_root", None)
        if root is not None:
            return True
        probe = tk.Tk()
        probe.withdraw()
        probe.destroy()
        return True
    except Exception:
        return False


# ================================================================ 对外流程

def setup_password(cfg: dict, write_config=None, write_registry: bool = True,
                   prefer: str = "auto") -> tuple[bool, str]:
    """第一次运行：让用户设一个口令。返回 (成功, 说明)。"""
    if is_set(cfg):
        return False, "已经设置过密码了（改密码用 --set-password）"
    while True:
        pw = ask("第一次运行，请设置被控端密码\n"
                 "（以后打开面板、停止、卸载都要输入它）",
                 confirm=True, prefer=prefer, title="设置被控端密码")
        if pw is None:
            return False, "已取消，未设置密码"
        if len(pw) < MIN_LENGTH:
            _warn(f"密码太短了，至少 {MIN_LENGTH} 位，请重新输入", prefer)
            continue
        blob = make_hash(pw)
        places = save_blob(blob, cfg, write_config, write_registry)
        return True, "已设置密码（保存在：" + "、".join(places or ["（没写进去？）"]) + "）"


def change_password(cfg: dict, write_config=None, write_registry: bool = True,
                    prefer: str = "auto") -> tuple[bool, str]:
    """改密码：先验旧的，再要新的（两次）。"""
    blob, source = load_blob(cfg)
    if blob is None:
        return setup_password(cfg, write_config, write_registry, prefer)
    if locked_to_machine():
        return False, ("密码由管理员放在机器级位置（ProgramData\\...\\secure），"
                       "只有管理员能改：请用管理员身份运行 --set-password")
    ok, msg = require(cfg, "修改密码", prefer=prefer, attempts=3)
    if not ok:
        return False, msg
    while True:
        pw = ask("请输入新密码", confirm=True, prefer=prefer, title="修改被控端密码")
        if pw is None:
            return False, "已取消，密码未修改"
        if len(pw) < MIN_LENGTH:
            _warn(f"密码太短了，至少 {MIN_LENGTH} 位，请重新输入", prefer)
            continue
        places = save_blob(make_hash(pw), cfg, write_config, write_registry)
        return True, "密码已修改（" + "、".join(places) + "）"


def clear_password(cfg: dict, write_config=None, write_registry: bool = True,
                   prefer: str = "auto") -> tuple[bool, str]:
    """取消密码保护（要验旧的）。"""
    blob, _source = load_blob(cfg)
    if blob is None:
        return True, "本来就没有设置密码"
    if locked_to_machine():
        return False, ("密码由管理员放在机器级位置，只有管理员能取消："
                       "请用管理员身份运行 --set-password --clear")
    ok, msg = require(cfg, "取消密码", prefer=prefer, attempts=3)
    if not ok:
        return False, msg
    places = save_blob(None, cfg, write_config, write_registry)
    return True, "已取消密码保护（" + "、".join(places) + "）"


def unlock_panel_policy(cfg: dict, prefer: str = "gui") -> tuple[bool, str]:
    """打开面板前的闸门策略（只做决定，不碰界面，方便单测）。

    * **还没设过密码** → 先问一次「现在就设一个」：
      - 设好了 → 放行（立刻生效，下次打开面板就要输密码）；
      - 用户取消 / 这台机器上没有可见的输入界面 → **仍然放行**，但明确警告。
        为什么要放行：`require()` 一直是这个原则（没设密码就不拦），而面板这条路
        以前是反着来的 —— 结果就是「没密码 ⇒ 面板打不开 ⇒ 没有入口设密码 ⇒
        永远没密码」，用户只能反复看到「请再运行一次本程序」，重跑一次还是同一个
        看不见的框（对话框 transient 到隐藏窗口时 Windows 不显示它），彻底锁死。
    * 设过密码 → 必须验对，最多 3 次；取消或连错都拒绝。
    """
    if not is_set(cfg):
        ok, msg = setup_password(cfg, write_config=None, prefer=prefer)
        if ok:
            return True, msg + "（下次打开面板就要输它）"
        return True, ("未设置面板密码 —— 任何本地用户都能打开面板/停止/卸载它。"
                      "建议现在就设一个：面板上的「修改密码…」，"
                      "或命令行 WallpaperAgent.exe --set-password"
                      f"（刚才那次设置没成：{msg}）")
    ok, msg = require(cfg, "打开被控端面板", prefer=prefer)
    return ok, msg


def _warn(text: str, prefer: str) -> None:
    """给用户一条错误提示（界面或控制台）。"""
    if prefer in ("auto", "gui") and _has_display() and not _console_usable():
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = getattr(tk, "_default_root", None)
            if root is None:
                root = tk.Tk()
                root.withdraw()
                messagebox.showwarning("被控端口令", text)
                root.destroy()
            else:
                messagebox.showwarning("被控端口令", text, parent=root)
            return
        except Exception:
            pass
    try:
        print(text)
    except Exception:
        pass


def require(cfg: dict, action: str, prefer: str = "auto",
            attempts: int = 3) -> tuple[bool, str]:
    """统一入口：需要口令的操作先过这里。

    * 没设过密码 → 放行，但明确提示「现在任何本地用户都能停掉它」。
      （不能因为没设密码就把用户锁在外面，那样连设置密码的机会都没有。）
    * 设过 → 最多问 attempts 次，全错就拒绝。
    * 没有可用的输入界面（后台/无人值守）→ 拒绝，并说明原因。
    """
    blob, source = load_blob(cfg)
    if blob is None:
        return True, ("未设置密码 —— 任何本地用户都能停掉/卸载它。"
                      "建议尽快设置：WallpaperAgent.exe --set-password")
    for i in range(max(1, attempts)):
        suffix = "" if i == 0 else f"（第 {i + 1} 次）"
        pw = ask(f"{action}需要密码{suffix}\n（口令来源：{source}）", prefer=prefer)
        if pw is None:
            return False, ("已取消：需要密码才能" + action +
                           "。没有输入界面时请在控制台/面板里操作。")
        if verify(pw, blob):
            return True, "密码正确"
    return False, f"密码不正确（已连续输错 {attempts} 次），操作已拒绝"
