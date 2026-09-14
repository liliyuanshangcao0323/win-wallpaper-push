#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""远程命令（SSH）引擎 —— 在每台设备上执行命令，把远端回显当证据带回来。

为什么要用 ssh.exe，而不是 paramiko 之类的库
------------------------------------------------------------------
1. Windows 10/11 自带 OpenSSH 客户端（C:\\Windows\\System32\\OpenSSH\\ssh.exe），
   被控端不用装任何东西，控制端 exe 里也不用再塞一个第三方库；
2. 执行的命令和人工在 cmd 里敲的**一模一样**，就是这条原始命令：

       ssh -i "C:\\User\\.ssh\\id_ed25519" Lonovo@<IP> "命令"

   界面上会把当前设置拼成的完整命令实时显示出来，方便逐字核对；
3. 这套「起 ssh 子进程 → 首次连接替人回答 yes → 等 shell 起来 → 发命令 →
   把远端回显当证据」的流程，来自已经在自己机房里跑通的 UWF 批量工具，
   这里把它整理成能复用的模块（不再只有固定那几个按钮）。

两种执行方式
------------------------------------------------------------------
* 会话式（session，默认）
    登录一次，然后**在同一个会话里逐条发命令**，共用同一个 cmd 环境。
    怎么判断一条命令跑完了：紧接着发一行 `echo ---WINWALL-DONE-n---`，
    远端回显到这一行就说明上一条命令执行完毕（比"死等 N 秒"可靠）。
    没有退出码 —— 成败按远端回显判断（拒绝访问 / 不是内部或外部命令 … 判失败），
    所以表格里写的是「证据」，跟 UWF 工具里看到的是同一套东西。
    适合：需要连着做几件事（禁用写保护 → 重启），或者命令之间要共享状态。

* 逐条独立（oneshot）
    每条命令单独一次连接：`ssh ... 用户@IP "命令"`，能拿到**真实退出码**，
    0 = 成功，其它 = 失败。适合"就是要知道到底成没成"的场景。
    代价：每台每条命令都要重新握手一次，慢一些，而且各条命令之间不共享状态。

安全边界（写在最前面，避免误用）
------------------------------------------------------------------
这个功能就是"用 SSH 在整批机器上执行命令"，本质上和你在 cmd 里逐台敲一样，
权限取决于用的那把私钥对应的账号（UWF 那类命令需要管理员账号）。
所以：界面里执行前一定会弹确认框列出「即将执行的完整命令 + 目标台数」，
命令行方式没有确认框（本来就是给你脚本化用的），请自己确认命令内容。
不在网络上传输私钥；私钥路径只是传给 ssh.exe 的 -i 参数。
"""

from __future__ import annotations

import ipaddress
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# ------------------------------------------------------------------ 默认值
# 前五项刻意与已跑通的 UWF 批量工具保持一致（同一批机器、同一把私钥），
# 用户打开界面就能直接用，不用重新填。
DEFAULT_KEY = os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519")
DEFAULT_USER = os.getenv("USERNAME", "Lonovo")
DEFAULT_EXTRA = "-o ConnectTimeout=10"
DEFAULT_WORKERS = 4
DEFAULT_YES_WAIT = 5.0
DEFAULT_TIMEOUT = 25.0

MODE_SESSION = "session"    # 登录一次，逐条发（共用同一个会话）
MODE_ONESHOT = "oneshot"    # 每条命令单独一次连接（有退出码）

MODE_LABELS = {
    MODE_SESSION: "会话式（登录一次，逐条发，靠回显判断）",
    MODE_ONESHOT: "逐条独立（每条一次连接，有退出码）",
}

TARGET_ONLINE = "online"    # 只发给当前在线的设备（控制端已经发现的）
TARGET_ALL = "all"          # 发给所有发现过的设备（含已离线）
TARGET_SPEC = "spec"        # 只发给下面手填的网段 / IP

TARGET_LABELS = {
    TARGET_ONLINE: "在线设备",
    TARGET_ALL: "全部已发现",
    TARGET_SPEC: "自定义网段/IP",
}

STATE_OK = "ok"
STATE_PART = "part"         # 部分命令成功
STATE_FAIL = "fail"
STATE_SENT = "sent"         # 已下发，结果未知（例如重启把会话切断了）

STATE_TEXT = {
    STATE_OK: "✅ 成功",
    STATE_PART: "⚠ 部分成功",
    STATE_FAIL: "❌ 失败",
    STATE_SENT: "📤 已下发",
}

# 纯文字的版本：给命令行用。
# 为什么要有它：中文 cmd 是 936 代码页，✅/📤 这种符号在那种控制台里显示成 "?"，
# 而界面（tkinter）里显示得好好的 —— 所以界面用上面那套，命令行用这套。
STATE_PLAIN = {
    STATE_OK: "成功",
    STATE_PART: "部分成功",
    STATE_FAIL: "失败",
    STATE_SENT: "已下发",
}


def plain_state(state: str) -> str:
    """状态的中文纯文字写法（命令行输出用）。"""
    return STATE_PLAIN.get(state, state)

# ------------------------------------------------------------------ 常用预设
# 一行一条命令；带换行的预设就是"连着做几件事"（会话式里会在同一个会话里跑）。
PRESETS: list[tuple[str, str]] = [
    ("连接测试 / 看主机名", "hostname"),
    ("查看 IP 配置", "ipconfig /all"),
    ("查看登录用户", "query user"),
    ("查看 UWF 状态", "uwfmgr get-config"),
    ("禁用 UWF 写保护", "uwfmgr filter disable"),
    ("禁用写保护并重启（两步）", "uwfmgr filter disable\nshutdown /r /t 0"),
    ("重启（立即）", "shutdown /r /t 0"),
    ("关机（立即）", "shutdown /s /t 0"),
    ("取消关机", "shutdown /a"),
    ("查看系统信息", 'systeminfo | findstr /B /C:"OS Name" /C:"OS Version"'),
    ("查看磁盘剩余空间", "wmic logicaldisk get caption,freespace,size"),
    ("查看本机 OpenSSH 客户端", "where ssh"),
    ("清理临时文件", r"del /q /f /s %TEMP%\*"),
]

# ------------------------------------------------------------------ 正则
# OpenSSH 致命错误：出现这些就没必要再等
FATAL_RE = re.compile(
    r"Connection timed out|Connection refused|Permission denied|No route to host|"
    r"Could not resolve hostname|Host key verification failed|Connection closed by|"
    r"Too many authentication failures|REMOTE HOST IDENTIFICATION HAS CHANGED|"
    r"POSSIBLE DNS SPOOFING|Connection reset by peer|Network is unreachable|"
    r"no matching host key|Identity file .* not accessible|"
    r"Bad owner or permissions|Load key .*invalid format|"
    r"Connection attempt failed|Operation timed out",
    re.IGNORECASE,
)
# 首次连接的主机密钥确认
HOSTKEY_RE = re.compile(
    r"yes/no|Are you sure you want to continue connecting|fingerprint is|"
    r"authenticity of host",
    re.IGNORECASE,
)
# 需要输密码（= 公钥在这台机器上没生效）
PASSWORD_RE = re.compile(r"password\s*:|password for|密码[:：]", re.IGNORECASE)
# 公钥没被授权（一次性连接模式下的表现）
DENIED_RE = re.compile(r"Permission denied|publickey|no supported authentication", re.IGNORECASE)
# 远端"命令本身失败"的回显特征
FAIL_RE = re.compile(
    r"拒绝访问|访问被拒绝|Access is denied|不是内部或外部命令|is not recognized|"
    r"系统找不到|系统找不到指定的文件|The system cannot find|"
    r"无法|失败|错误|error|failed|denied|拒绝",
    re.IGNORECASE,
)
# 远端 shell 已经起来的证据
SHELL_READY_RE = re.compile(
    r"Microsoft Windows|版权所有|C:\\[^>\r\n]*>|PS [A-Za-z]:\\|[$#]\s*$|>\s*$",
    re.MULTILINE,
)
# 会把会话切断的命令（重启 / 关机 / 注销）：会话断了算"已下发"，不算失败
CUT_RE = re.compile(
    r"shutdown\s+/[rs]|Restart-Computer|Stop-Computer|logoff|Remove-Computer",
    re.IGNORECASE,
)
# 私钥不在时 ssh 的报错（警告和提示分开处理）
KEY_MISSING_TEXT = "私钥文件不存在"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_SENTINEL_SEQ = iter(range(1, 10 ** 6))


def stopped(stop) -> bool:
    """统一的"要不要停下来"判断。

    调用方传进来的可能是可调用对象（`lambda: flag.is_set()`），
    也可能直接是个 threading.Event —— 两种都认，省得每个调用点都包一层。
    """
    if stop is None:
        return False
    try:
        return bool(stop() if callable(stop) else stop.is_set())
    except Exception:
        return False


# ================================================================== 小工具

def decode_bytes(raw: bytes) -> str:
    """远端回显可能是 UTF-8（英文系统）也可能是 GBK（中文系统），自动判断。

    为什么不能只试一种：Windows 中文系统 SSH 管道输出是 GBK 字节，
    但 GBK 字节有时恰好也是合法的 UTF-8 序列 → UTF-8 解码"成功"但结果是乱码
    （例如「已成功」变成 `ß~`）。这时需要改用 GBK 重解才能得到正确的汉字。

    判断依据：GBK 字节被错误地按 UTF-8 解码时，会产生大量 Latin-1 Supplement
    范围的字符（U+00C0~U+00FF，如 ß, Ã, Â, á 等），而真正的 UTF-8 中文解码
    不会出现这些字符。所以：
        UTF-8 解码有 Latin-1 Supplement + GBK 解码有汉字 → 用 GBK
        其它情况 → 用 UTF-8（英文系统或真正的 UTF-8 中文）
    """
    if not raw:
        return ""
    # 同时试两种编码
    t_utf8: str | None = None
    t_gbk: str | None = None
    try:
        t_utf8 = raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        t_gbk = raw.decode("gbk")
    except UnicodeDecodeError:
        pass
    # 两种都能解时，选更合理的那个
    if t_utf8 is not None and t_gbk is not None:
        utf8_latin = sum(1 for c in t_utf8 if "\u00c0" <= c <= "\u00ff")
        gbk_cjk = sum(1 for c in t_gbk if "\u4e00" <= c <= "\u9fff"
                       or "\u3000" <= c <= "\u303f"
                       or "\uff00" <= c <= "\uffef")
        ascii_count = sum(1 for c in t_utf8 if "\u0000" <= c <= "\u007f")
        total = len(t_utf8) or 1
        mostly_ascii = ascii_count / total > 0.7
        if utf8_latin > 0 and gbk_cjk > 0 and not mostly_ascii:
            return t_gbk              # GBK 更合理：中文为主 + UTF-8 里有 ß/Ã 乱码
        return t_utf8                 # UTF-8 更合理（英文为主，或无乱码特征）
    if t_utf8 is not None:
        return t_utf8
    if t_gbk is not None:
        return t_gbk
    for enc in ("cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def ssh_exe() -> str:
    """系统里的 ssh.exe（优先用完整路径；PATH 里找不到就返回 "ssh" 再试一次）。"""
    return shutil.which("ssh") or "ssh"


def ssh_available(cfg: dict | None = None) -> tuple[bool, str]:
    """本机到底有没有 ssh 客户端 —— 没有的话必须第一时间说清楚。

    这是最常见的"一条命令都没跑成"的原因：Windows 的 OpenSSH 客户端属于
    「可选功能」，没装的话 ssh.exe 根本不存在（表现为 Popen 抛
    FileNotFoundError，界面上只有一句英文系统错误，看不出要装什么）。
    """
    exe = (cfg or {}).get("ssh_exe")
    if isinstance(exe, (list, tuple)):
        path = str(exe[0]) if exe else ""
    else:
        path = str(exe or ssh_exe())
    if not path:
        return False, "没指定 ssh 客户端"
    ok = os.path.isfile(path) if (os.path.isabs(path) or os.sep in path) \
        else shutil.which(path) is not None
    if ok:
        return True, path
    return False, (
        "本机没有 ssh 客户端：" + path +
        "（Windows：设置 → 应用 → 可选功能 → 添加「OpenSSH 客户端」）"
    )


def defaults() -> dict:
    """远程命令的默认设置（会写进 controller_config.json 的 ssh 段）。"""
    return {
        "key": DEFAULT_KEY,
        "user": DEFAULT_USER,
        "extra": DEFAULT_EXTRA,
        "accept_new_hostkey": True,   # 首次连接自动接受主机密钥（等于替人敲 yes）
        "mode": MODE_SESSION,
        "workers": DEFAULT_WORKERS,
        "timeout": DEFAULT_TIMEOUT,
        "yes_wait": DEFAULT_YES_WAIT,
        "wait_between": 0.0,          # 两条命令之间额外等待（0 = 只靠回显判断）
        "target_mode": TARGET_ONLINE,
        "targets": "",
        "commands": "hostname",
    }


def _clamp_num(value, default: float, lo: float, hi: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n != n:                      # NaN
        return default
    return max(lo, min(hi, n))


def normalize_cfg(raw: dict | None) -> dict:
    """把配置洗干净：缺的补默认值，数字越界就夹回来。

    为什么必须做：这些值会从 JSON 文件里读回来，用户也可能手改；
    workers=0 会让线程池直接抛异常，timeout 填错会让每条命令都瞬间超时。
    """
    out = defaults()
    for k, v in dict(raw or {}).items():
        if v is not None:
            out[k] = v
    out["key"] = str(out.get("key") or "").strip() or DEFAULT_KEY
    out["user"] = str(out.get("user") or "").strip() or DEFAULT_USER
    out["extra"] = str(out.get("extra") or "").strip()
    out["mode"] = out["mode"] if out.get("mode") in (MODE_SESSION, MODE_ONESHOT) \
        else MODE_SESSION
    out["target_mode"] = out["target_mode"] if out.get("target_mode") in (
        TARGET_ONLINE, TARGET_ALL, TARGET_SPEC) else TARGET_ONLINE
    out["targets"] = str(out.get("targets") or "")
    out["commands"] = str(out.get("commands") or "")
    out["workers"] = int(_clamp_num(out.get("workers"), DEFAULT_WORKERS, 1, 32))
    out["timeout"] = _clamp_num(out.get("timeout"), DEFAULT_TIMEOUT, 3.0, 3600.0)
    out["yes_wait"] = _clamp_num(out.get("yes_wait"), DEFAULT_YES_WAIT, 0.0, 120.0)
    out["wait_between"] = _clamp_num(out.get("wait_between"), 0.0, 0.0, 600.0)
    out["accept_new_hostkey"] = bool(out.get("accept_new_hostkey", True))
    return out


def parse_commands(text: str) -> list[str]:
    """把多行文本变成命令列表。

    规则（界面上也写着）：
        * 一行一条命令，空行忽略
        * `#` 或 `::` 开头的行是注释（方便把命令存下来下次直接用）
        * 顺手去掉有人从文档里复制过来的行首提示符 `C:\\> ` / `PS C:\\> ` / `$ `
        * 去掉首尾空白
    """
    out: list[str] = []
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("::"):
            continue
        s = re.sub(r"^(?:PS\s+)?[A-Za-z]:\\[^>]*>\s*", "", s)   # C:\Users\x>
        s = re.sub(r"^PS>\s*", "", s)
        s = re.sub(r"^[$#>]\s+", "", s)
        if s:
            out.append(s)
    return out


def display_command(cfg: dict, ip: str, command: str | None = None) -> str:
    """界面上展示的等价命令（方便和人工敲的那条逐字对照）。"""
    extra = str(cfg.get("extra") or "").strip()
    shown_extra = (" " + extra) if extra else ""
    tail = f' "{command}"' if command else ""
    return f'ssh -i "{cfg.get("key", "")}"{shown_extra} {cfg.get("user", "")}@{ip}{tail}'


def _extra_args(cfg: dict) -> list[str]:
    """附加参数：顺便按需补上「首次连接自动接受主机密钥」。"""
    try:
        args = shlex.split(str(cfg.get("extra") or ""))
    except ValueError:
        args = []
    # 用户自己在附加参数里写过 StrictHostKeyChecking 就听用户的（别重复加）
    if cfg.get("accept_new_hostkey", True) and not any(
            "stricthostkeychecking" in a.lower() for a in args):
        args += ["-o", "StrictHostKeyChecking=accept-new"]
    return args


def ssh_argv(cfg: dict, ip: str, command: str | None = None) -> list[str]:
    """拼出真正要执行的命令行参数（纯函数，方便自测）。

    cfg["ssh_exe"] 可以是字符串（默认 "ssh"），也可以是列表
    —— 自测时指向一个假的 ssh，就能在没有真实 SSH 服务的情况下
    把整条链路（读输出、判成败、抽证据）跑一遍。
    """
    exe = cfg.get("ssh_exe") or ssh_exe()
    argv = list(exe) if isinstance(exe, (list, tuple)) else [str(exe)]
    argv += ["-i", str(cfg.get("key") or "")]
    argv += _extra_args(cfg)
    target = f"{cfg.get('user', '')}@{ip}"
    argv.append(target)
    if command:
        argv.append(command)
    return argv


def run_hidden_kwargs() -> dict:
    """起 ssh 子进程时的通用参数。"""
    return {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "bufsize": 0,
        "creationflags": CREATE_NO_WINDOW,
    }


# ================================================================== 目标地址

_SHORT_RANGE_RE = re.compile(r"^(\d+\.\d+\.\d+\.)(\d+)\s*-\s*(\d+)$")


def expand_short_range(text: str) -> tuple[str, list[str]]:
    """把 `10.127.112.1-56` 这种简写展开成完整范围写法（UWF 工具的习惯写法）。

    返回（展开后的文本, 说明）。netutil.parse_target_spec 只认
    `10.127.112.1-10.127.112.60` 这种两段都写全的写法，而机房里口头说的都是
    "1 到 56"，所以这里先补全再交给它。
    """
    notes: list[str] = []
    out: list[str] = []
    for piece in re.split(r"([,;\s]+)", str(text or "")):
        m = _SHORT_RANGE_RE.match(piece.strip())
        if not m:
            out.append(piece)
            continue
        prefix, lo, hi = m.group(1), m.group(2), m.group(3)
        if int(hi) < int(lo):
            lo, hi = hi, lo
        out.append(f"{prefix}{lo}-{prefix}{hi}")
        notes.append(f"「{piece.strip()}」按 {prefix}{lo} … {prefix}{hi} 展开")
    return "".join(out), notes


def spec_ips(text: str, max_hosts: int = 4094) -> tuple[list[str], list[str]]:
    """解析用户手填的网段 / IP / 范围 → (地址列表, 说明)。

    认这几种写法（和「设置」页里"额外网段"一致，多一种简写）：
        10.127.112.0/24              整个网段（最多 max_hosts 个）
        10.127.112.98                单台
        10.127.112.1-10.127.112.60   范围
        10.127.112.1-56              范围简写（前缀相同）
    """
    import netutil as N   # 延迟导入：这个模块自身不依赖项目的其他部分

    expanded, notes = expand_short_range(text)
    _send, hosts, n2 = N.parse_target_spec(expanded, max_sweep=max_hosts)
    notes += [n for n in n2 if "逐台探测" in n or "单播" in n or "忽略" in n or "太大" in n]
    return clean_ips(hosts), notes


def clean_ips(seq) -> list[str]:
    """去重 + 排掉非法项 + 按数字顺序排好（表格和日志看起来才顺眼）。"""
    seen: list[str] = []
    for item in seq or []:
        s = str(item or "").strip()
        if not s:
            continue
        try:
            ip = str(ipaddress.IPv4Address(s))
        except ValueError:
            continue
        if ip not in seen:
            seen.append(ip)
    seen.sort(key=lambda x: int(ipaddress.IPv4Address(x)))
    return seen


def split_hosts_by_reach(ips: list[str], local_ips: list[str],
                         prefixes: list[str]) -> tuple[list[str], list[str]]:
    """把地址分成「和本机同网段」和「其它」两拨，界面提示用。

    为什么值得提示：SSH 走的是普通 IP 路由，跟本软件的 UDP 广播不是一回事。
    跨网段能不能连上完全取决于路由/防火墙，用户看到"连不上"时先要知道
    这台机器到底在不在本机直连的网段里。
    """
    import netutil as N

    same, other = [], []
    nets = []
    for p in prefixes or []:
        try:
            nets.append(ipaddress.IPv4Network(p, strict=False))
        except ValueError:
            continue
    if not nets and local_ips:
        nets = [ipaddress.IPv4Network(f"{ip}/24", strict=False) for ip in local_ips]
    for ip in ips:
        try:
            a = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        (same if any(a in n for n in nets) else other).append(ip)
    return same, other


def key_state(path: str) -> tuple[bool, str]:
    """私钥在不在（返回 (存在, 说明)）。不读私钥内容，只看文件。"""
    p = str(path or "").strip()
    if not p:
        return False, "还没填私钥路径"
    if not os.path.isfile(p):
        return False, f"{KEY_MISSING_TEXT}：{p}（ssh -i 会直接失败）"
    return True, "私钥已找到"


# ================================================================== 单台执行

class HostResult:
    """一台设备执行完的结果。"""

    def __init__(self, ip: str):
        self.ip = ip
        self.state = STATE_FAIL
        self.evidence = ""          # 远端回显（截断过）—— 表格里"结果证据"那一列
        self.error = ""             # 失败原因（人话）
        self.seconds = 0.0
        self.outputs: list[tuple[str, int | None, str]] = []   # (命令, 退出码, 回显)
        self.sent_key = False       # 这次登录是不是替人回答了 yes

    @property
    def text(self) -> str:
        return STATE_TEXT.get(self.state, self.state)

    def summary(self) -> str:
        """一句话结果，进日志用。"""
        if self.state == STATE_OK:
            return f"成功（{len(self.outputs)} 条命令）"
        if self.state == STATE_PART:
            return f"部分成功：{self.error or self.evidence}"
        if self.state == STATE_SENT:
            return f"已下发（结果未知）：{self.error or self.evidence}"
        return self.error or self.evidence or "失败"


class SshSession:
    """一次交互式 SSH 会话：登录 → 应答 yes → 逐条发命令 → 取回远端回显。"""

    def __init__(self, ip: str, cfg: dict, spawn=None):
        self.ip = ip
        self.cfg = cfg
        self.raw = b""
        self.lock = threading.Lock()
        self.closed = False
        self.answered_key = False
        self.args = ssh_argv(cfg, ip)
        self.display = display_command(cfg, ip)
        self.proc = (spawn or subprocess.Popen)(self.args, **run_hidden_kwargs())
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ---------------- 底层 IO ----------------
    def _read_loop(self) -> None:
        stream = self.proc.stdout
        while stream is not None:
            try:
                chunk = stream.read(4096)
            except Exception:
                break
            if not chunk:
                break
            with self.lock:
                self.raw += chunk

    def text(self) -> str:
        with self.lock:
            return decode_bytes(self.raw)

    def mark(self) -> int:
        with self.lock:
            return len(self.raw)

    def text_since(self, mark: int) -> str:
        with self.lock:
            return decode_bytes(self.raw[mark:])

    def write(self, line: str) -> None:
        """往远端 shell 写一行。

        编码要小心：Windows 远端的 cmd.exe 是按**本机代码页**理解收到的那串字节的
        （中文系统 = 936/GBK）。纯 ASCII 命令两种写法都一样，所以命令行本身照发；
        命令里带中文时（例如 `msg * 你好`、带中文的路径）必须按 GBK 发过去，
        否则远端收到的是一堆问号。以前这里统一用 ascii+ignore，
        结果是"带中文的命令发过去变成空的了"——静默出错，最难查。
        """
        if self.closed or self.proc.stdin is None:
            return
        try:
            data = line.encode("ascii") + b"\r\n"
        except UnicodeEncodeError:
            data = line.encode("gbk", "replace") + b"\r\n"
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except Exception:
            pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def fatal(self) -> str | None:
        m = FATAL_RE.search(self.text())
        return m.group(0) if m else None

    def wait_for(self, pattern, timeout: float, stop=None) -> bool:
        """等某个模式出现在回显里；进程死了或要停止就提前返回。"""
        deadline = time.time() + max(0.2, timeout)
        while time.time() < deadline:
            if stopped(stop):
                return False
            if pattern.search(self.text()):
                return True
            if not self.alive():
                time.sleep(0.2)          # 进程已退出，再给它一点时间把尾巴吐完
                return bool(pattern.search(self.text()))
            time.sleep(0.12)
        return bool(pattern.search(self.text()))

    def sleep_check(self, seconds: float, stop=None) -> bool:
        """等待若干秒，期间出现致命错误或进程退出就提前返回 False。"""
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline:
            if stopped(stop):
                return False
            if self.fatal() or not self.alive():
                return False
            time.sleep(0.1)
        return True

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=1.5)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass
        time.sleep(0.05)

    # ---------------- 流程 ----------------
    def login(self, stop=None) -> tuple[bool, str]:
        """登录并等远端 shell 起来。判定必须有证据，绝不"看不出错就算连上了"。

        为什么要等"三件事里先出现的那一件"：以前是死等"主机密钥提示"（等 4 秒）
        再死等 shell（再等 5 秒），一台机器就要空转 9 秒 —— 48 台机器按 4 并发
        要白白等掉近两分钟。现在谁先出现就立刻往下走：
            * 主机密钥提示 → 替人回答 yes（勾了自动接受就不会出现）
            * 远端提示符   → 已经进来了
            * 致命错误     → 立刻收手（连不上 / 公钥不对 / 主机密钥变了）
        """
        ws = max(0.0, float(self.cfg.get("yes_wait", DEFAULT_YES_WAIT)))
        answered = False
        deadline = time.time() + ws + 4.0
        while time.time() < deadline:
            if stopped(stop):
                return False, "已停止"
            text = self.text()
            if not answered and HOSTKEY_RE.search(text):
                self.write("yes")
                answered = True
                self.answered_key = True
                deadline = min(deadline, time.time() + ws + 2.0)
            if SHELL_READY_RE.search(text):
                return True, "已登录"
            if PASSWORD_RE.search(text):
                return False, "登录失败: 远端要输密码 —— 这把公钥在那台机器上没生效"
            err = self.fatal()
            if err:
                return False, "登录失败: " + err
            if not self.alive():
                break
            time.sleep(0.1)

        # 循环结束：再按证据判一次（宁可报失败，也不能没证据就说连上了）
        text = self.text()
        err = self.fatal()
        if err:
            return False, "登录失败: " + err
        if PASSWORD_RE.search(text):
            return False, "登录失败: 远端要输密码 —— 这把公钥在那台机器上没生效"
        if SHELL_READY_RE.search(text):
            return True, "已登录"
        rc = self.proc.poll()
        if rc is not None and rc != 0:
            return False, f"登录失败: ssh 退出码 {rc}"
        if not text.strip():
            return False, "登录失败: 远端没有任何输出（不通 / 被防火墙拦 / 私钥不对）"
        if answered and self.alive():
            return True, "已登录"
        return False, "登录失败: 没等到远端提示符"

    def force_utf8(self, stop=None) -> None:
        """登录后尝试把远端输出编码切到 UTF-8（尽力而为，不保证成功）。

        chcp 65001 只改控制台显示代码页，对 SSH 管道输出无效 —— 但没坏处，
        某些系统（如 PowerShell 作为默认 shell）可能会因此切到 UTF-8 管道编码。
        真正保证不出乱码的是 decode_bytes 的智能检测（GBK/UTF-8 自动判断）。
        """
        sentinel = f"---WINWALL-UTF8-{next(_SENTINEL_SEQ)}---"
        self.write("chcp 65001 >nul 2>nul")
        time.sleep(0.05)
        self.write("echo " + sentinel)
        self.wait_for(re.compile(re.escape(sentinel)), timeout=1.5, stop=stop)

    def run_command(self, command: str, stop=None) -> tuple[str, str, str]:
        """在会话里执行一条命令 → (状态, 证据, 失败原因)。

        判定方式：发完命令再发一行 `echo ---WINWALL-DONE-n---`，
        远端回显到它就说明命令执行完了（比"死等 N 秒"可靠，也不用猜机器快慢）。
        """
        mark = self.mark()
        sentinel = f"---WINWALL-DONE-{next(_SENTINEL_SEQ)}---"
        self.write(command)
        time.sleep(0.05)          # 先让远端把命令本身的回显吐出来
        self.write("echo " + sentinel)

        done = self.wait_for(re.compile(re.escape(sentinel)),
                             timeout=float(self.cfg.get("timeout", DEFAULT_TIMEOUT)),
                             stop=stop)
        out = self.text_since(mark)
        evidence = strip_echo(out, command, sentinel)
        alive = self.alive()

        err = self.fatal()
        if err and not alive:
            # 会话直接断了：重启 / 关机类命令属于正常现象，其它就是异常
            if CUT_RE.search(command):
                return STATE_SENT, evidence or "远端会话已断开", "会话被远端切断（正常）"
            return STATE_FAIL, evidence, "连接中断: " + err
        if err:
            return STATE_FAIL, evidence, "连接中断: " + err
        if not done:
            if not alive:
                if CUT_RE.search(command):
                    return STATE_SENT, evidence or "远端会话已断开", "会话被远端切断（正常）"
                return STATE_FAIL, evidence, "会话意外中断（命令可能没跑完）"
            return STATE_FAIL, evidence, f"等回显超时（{self.cfg.get('timeout')} 秒）"
        if FAIL_RE.search(evidence):
            first = next((ln.strip() for ln in evidence.splitlines() if ln.strip()), "")
            return STATE_FAIL, evidence, first[:120] or "回显里有错误字样"
        first = next((ln.strip() for ln in evidence.splitlines() if ln.strip()), "")
        return STATE_OK, evidence, "" if first else "命令没有任何回显"


# 远端提示符：cmd 的 "C:\Users\x>"、PowerShell 的 "PS C:\>"、Linux 的 "$ " / "# "
_PROMPT_RE = re.compile(r"^(?:PS\s+)?[A-Za-z]:\\[^>\r\n]*>\s*|^PS>\s*|^[$#>]\s+")


def strip_echo(out: str, command: str, sentinel: str) -> str:
    """从会话回显里去掉「命令本身那一行」和结束标记，只留远端真正的输出。

    为什么要这么挑：远端 shell 会把我们的输入回显回来（前面还带个提示符，
    形如 `C:\\Users\\Lonovo>hostname`），全留着的话"结果证据"那一列就全是
    自己刚发出去的命令，看不到远端到底说了什么。

    特殊情况：登录后 force_utf8() 发的 `chcp 65001 ... powershell.exe ...` 命令
    也会被回显回来，但它的原始命令文本（不含 PowerShell 包装）不等于回显行。
    这里用两种策略处理：
        1. body 恰好等于原始命令 → 跳过（精确匹配）
        2. body 以 chcp / powershell.exe 开头 → 一定是 force_utf8 的回显，跳过
    """
    want = str(command or "").strip()
    keep: list[str] = []
    for line in str(out or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if sentinel in s:                       # 结束标记本身（连带它那行回显）
            continue
        body = _PROMPT_RE.sub("", s).strip()    # 去掉提示符后的"净内容"
        if not body:
            continue                            # 光秃秃一个提示符，不算输出
        if body == want or body == f"echo {sentinel}":
            continue                            # 我们自己发出去的那两行
        # force_utf8() 发的 PowerShell 编码设置命令：
        # 真实远端回显：C:\>chcp 65001 ... 或 C:\>powershell.exe ...
        # 假 ssh 回显：ok: chcp 65001 ... 或 ok: powershell.exe ...
        if body.startswith(("chcp ", "powershell.exe ", "ok: chcp ", "ok: powershell.exe ")):
            continue
        keep.append(line.rstrip())
    return clip("\n".join(keep).strip())


def clip(text: str, limit: int = 4000) -> str:
    """回显太长就截断（表格和日志都不需要几万行）。"""
    t = str(text or "")
    if len(t) <= limit:
        return t
    return t[:limit] + f"\n…（还有 {len(t) - limit} 个字符，见日志/导出文件）"


def first_lines(text: str, n: int = 2, width: int = 110) -> str:
    """取回显最前面的几行，塞进表格"结果证据"那一列。"""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    out = " / ".join(lines[:n])
    return out[:width] + ("…" if len(out) > width else "")


# ================================================================== 单台入口

def run_oneshot(ip: str, cfg: dict, commands: list[str], stop=None,
                log=None) -> HostResult:
    """逐条独立：每条命令一次连接，拿真实退出码。"""
    res = HostResult(ip)
    started = time.time()
    ok_n, fail_n = 0, 0
    for i, cmd in enumerate(commands, 1):
        if stopped(stop):
            res.error = "已停止"
            break
        # 一次性模式：chcp 65001 尽力切 UTF-8（对某些系统有效）；
        # 真正保证不乱码的是 decode_bytes 的 GBK/UTF-8 智能检测。
        utf8_cmd = f'cmd /c "chcp 65001 >nul 2>nul & {cmd}"'
        argv = ssh_argv(cfg, ip, utf8_cmd)
        if log:
            log(f"[{ip}] ({i}/{len(commands)}) {cmd}", "dim")
        try:
            # stdin=DEVNULL：远端没有公钥授权时 ssh 想弹密码会立刻拿到 EOF 而失败，
            # 不会在这里挂到超时。
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    bufsize=0, creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            res.outputs.append((cmd, None, ""))
            res.error = f"起 ssh 失败：{e}"
            fail_n += 1
            break
        try:
            out, _ = proc.communicate(timeout=float(cfg.get("timeout", DEFAULT_TIMEOUT)))
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                out, _ = proc.communicate(timeout=3)
            except Exception:
                out = b""
            text = clip(decode_bytes(out or b""))
            res.outputs.append((cmd, None, text))
            res.evidence = first_lines(text)
            res.error = f"超时（{cfg.get('timeout')} 秒没结束）"
            fail_n += 1
            break
        code = proc.returncode
        text = clip(decode_bytes(out or b""))
        res.outputs.append((cmd, code, text))
        if code == 0:
            ok_n += 1
        else:
            fail_n += 1
            res.evidence = first_lines(text) or f"退出码 {code}"
            if DENIED_RE.search(text) and code == 255:
                res.error = "连不上 / 公钥没生效（Permission denied）"
            elif PASSWORD_RE.search(text):
                res.error = "远端要输密码 —— 这把公钥在那台机器上没生效"
            elif code == 255:
                res.error = "SSH 连不上（超时 / 拒绝 / 主机密钥变了）"
            else:
                res.error = first_lines(text, 1) or f"退出码 {code}"
            break
        if i < len(commands) and float(cfg.get("wait_between", 0) or 0) > 0:
            time.sleep(float(cfg["wait_between"]))

    res.seconds = time.time() - started
    if ok_n and not fail_n:
        res.state = STATE_OK
        res.evidence = res.evidence or first_lines(res.outputs[-1][2]) or "命令已执行"
        res.error = ""
    elif ok_n and fail_n:
        res.state = STATE_PART
    else:
        res.state = STATE_FAIL
    return res


def run_session(ip: str, cfg: dict, commands: list[str], stop=None,
                log=None, spawn=None) -> HostResult:
    """会话式：登录一次，在同一个会话里逐条发。"""
    res = HostResult(ip)
    started = time.time()
    sess = None
    try:
        sess = SshSession(ip, cfg, spawn=spawn)
        res.sent_key = False
        ok, msg = sess.login(stop=stop)
        res.sent_key = sess.answered_key
        if not ok:
            res.state = STATE_FAIL
            res.error = msg
            return res
        if log:
            log(f"[{ip}] 已登录（{sess.display}）", "dim")
        # 切 UTF-8：远端 cmd.exe 默认是 GBK（936），SSH 非交互模式下
        # 输出编码可能不一致 → 乱码。chcp 65001 强制切 UTF-8 后统一。
        sess.force_utf8(stop=stop)
        ok_n, fail_n, sent_n = 0, 0, 0
        for i, cmd in enumerate(commands, 1):
            if stopped(stop):
                res.error = "已停止"
                break
            if not sess.alive():
                res.error = "会话已断开，剩下的命令没发出去"
                break
            state, evidence, err = sess.run_command(cmd, stop=stop)
            res.outputs.append((cmd, None, evidence))
            if state == STATE_OK:
                ok_n += 1
                if log:
                    log(f"[{ip}] ✅ {cmd} → {first_lines(evidence, 1) or '已执行'}", "ok")
            elif state == STATE_SENT:
                sent_n += 1
                if log:
                    log(f"[{ip}] 📤 {cmd} → {err or '已下发'}", "warn")
            else:
                fail_n += 1
                res.evidence = first_lines(evidence) or err
                res.error = f"{cmd}：{err}"
                if log:
                    log(f"[{ip}] ❌ {cmd} → {err}", "err")
                break
            if i < len(commands) and float(cfg.get("wait_between", 0) or 0) > 0:
                time.sleep(float(cfg["wait_between"]))

        if sent_n and not fail_n and not ok_n:
            res.state = STATE_SENT
        elif ok_n and not fail_n:
            res.state = STATE_SENT if sent_n else STATE_OK
            res.evidence = res.evidence or first_lines(res.outputs[-1][2]) or "命令已执行"
        elif ok_n or sent_n:
            res.state = STATE_PART
        else:
            res.state = STATE_FAIL
            res.error = res.error or "没有一条命令成功"
        return res
    except Exception as e:                      # 兜底：不能因为一台机器把整批带崩
        res.state = STATE_FAIL
        res.error = f"异常：{e}"
        return res
    finally:
        if sess is not None:
            sess.close()
        res.seconds = time.time() - started


def run_host(ip: str, cfg: dict, commands: list[str], stop=None, log=None,
             spawn=None) -> HostResult:
    """在一台设备上执行命令（按 cfg["mode"] 分派）。"""
    cfg = normalize_cfg(cfg)
    res = HostResult(ip)
    cmds = list(commands or [])
    if not cmds:
        res.error = "没有要执行的命令"
        return res
    ok, info = ssh_available(cfg)
    if not ok:
        res.error = info
        return res
    if cfg["mode"] == MODE_ONESHOT:
        return run_oneshot(ip, cfg, cmds, stop=stop, log=log)
    return run_session(ip, cfg, cmds, stop=stop, log=log, spawn=spawn)


def run_hosts(hosts: list[str], cfg: dict, commands: list[str], on_result=None,
              stop=None, log=None, spawn=None) -> list[HostResult]:
    """并发在每台设备上执行命令，每台一完成就回调 on_result(结果)。

    on_result 会在工作线程里被调用 —— 界面那边要把它转成主线程执行
    （控制端的做法是丢进队列）。
    """
    cfg = normalize_cfg(cfg)
    cmds = list(commands or [])
    ips = clean_ips(hosts)
    results: list[HostResult] = []
    lock = threading.Lock()
    if not ips:
        return results

    # 本机没有 ssh 客户端就没必要挨台去试（48 台会刷出 48 条一模一样的错），
    # 这里一次说清楚然后收手；界面在开始前也会先查一遍。
    ok, info = ssh_available(cfg)
    if not ok:
        if log:
            log(info, "err")
        return results

    def one(ip: str) -> None:
        if stopped(stop):
            return
        r = run_host(ip, cfg, cmds, stop=stop, log=log, spawn=spawn)
        with lock:
            results.append(r)
        if on_result:
            try:
                on_result(r)
            except Exception:
                pass

    workers = max(1, min(int(cfg.get("workers", DEFAULT_WORKERS)), len(ips)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ssh") as pool:
        list(pool.map(one, ips))
    results.sort(key=lambda r: int(ipaddress.IPv4Address(r.ip)))
    return results


def summarize(results: list[HostResult]) -> dict:
    """统计：成功 / 已下发 / 部分 / 失败。"""
    out = {"total": len(results), STATE_OK: 0, STATE_SENT: 0,
           STATE_PART: 0, STATE_FAIL: 0}
    for r in results:
        out[r.state] = out.get(r.state, 0) + 1
    return out


def plan_text(hosts: list[str], cfg: dict, commands: list[str]) -> str:
    """执行前的确认框内容：把「要干什么」原原本本列出来。"""
    ips = clean_ips(hosts)
    lines = [
        f"目标：{len(ips)} 台设备" + (f"（{ips[0]} … {ips[-1]}）" if ips else ""),
        f"方式：{MODE_LABELS.get(cfg.get('mode'), cfg.get('mode'))}",
        f"并发：{cfg.get('workers')} 台   单条超时：{cfg.get('timeout')} 秒",
        "",
        "执行命令（按顺序）：",
    ]
    lines += [f"    {i}. {c}" for i, c in enumerate(commands, 1)]
    if ips:
        lines += ["", "实际执行的完整命令（以第一台为例）：",
                  "    " + display_command(cfg, ips[0], commands[0] if commands else None)]
    return "\n".join(lines)


# ================================================================== 备用批处理

def build_batch(hosts: list[str], cfg: dict, commands: list[str],
                title: str = "remote command") -> str:
    """生成一个备用 .bat：不依赖本软件，双击就能干同样的事。

    两个必须遵守的坑（都踩过）：
      * 整个文件只用 ASCII —— cmd.exe 按字节偏移在批处理里找下一行，
        混进中文会让后面的行错位、把 echo 的参数当命令执行；
      * 换行必须是 CRLF，而且写文件时要 newline=""，
        否则 Python 会把 \\n 又翻成 \\r\\n，写出 \\r\\r\\n 把批处理弄坏。
    """
    ips = clean_ips(hosts)
    cmds = list(commands or [])
    extra = str(cfg.get("extra") or "").strip()
    shown_extra = (" " + extra) if extra else ""
    ssh = f'ssh -i "{cfg.get("key", "")}"{shown_extra} {cfg.get("user", "")}'

    lines = [
        "@echo off",
        "chcp 936 >nul",
        "setlocal",
        "echo ============================================",
        "echo Remote command batch   (generated by Win Wallpaper Push)",
        f"echo Hosts  : {len(ips)}",
        f"echo Login  : {ssh}@(IP)",
        f"echo Mode   : {cfg.get('mode')}",
        "echo ============================================",
        "echo.",
        "",
        "set OK=0",
        "set BAD=0",
    ]
    for ip in ips:
        lines.append(f"call :one {ip}")
    lines += [
        "goto :summary",
        "",
        ":one",
        "set IP=%1",
        "set FAILED=0",
        "echo --------------------------------------------",
        f"echo [%IP%] remote command",
    ]
    for i, cmd in enumerate(cmds, 1):
        safe = str(cmd).replace('"', '')
        lines.append(f'echo [%IP%] step {i}/{len(cmds)} : {safe}')
        lines.append(f'echo y | {ssh}@%IP% "{safe}"')
        if i < len(cmds):
            lines.append("if errorlevel 1 set FAILED=1")
    lines += [
        'if "%FAILED%"=="1" goto :bad',
        "echo [%IP%] done",
        "set /a OK+=1",
        "goto :eof",
        ":bad",
        "echo [%IP%] FAILED",
        "set /a BAD+=1",
        "goto :eof",
        "",
        ":summary",
        "echo ============================================",
        "echo RESULT OK=%OK% BAD=%BAD%",
        "echo ============================================",
        "pause",
    ]
    text = "\r\n".join(lines) + "\r\n"
    assert text.isascii(), "批处理必须只有 ASCII 字符"      # 自己盯住这条老坑
    return text


def save_batch(path: str, text: str) -> None:
    """按批处理的规矩写文件：ASCII + CRLF（newline="" 防止写出 \\r\\r\\n）。"""
    with open(path, "w", encoding="ascii", errors="replace", newline="") as fh:
        fh.write(text)


# ================================================================== 自测入口

def selftest() -> int:
    """打印本机能不能用 ssh（python sshcmd.py --selftest）。"""
    exe = ssh_exe()
    print("远程命令（SSH）自检")
    print("  ssh 客户端 :", exe)
    ok, msg = key_state(DEFAULT_KEY)
    print("  默认私钥   :", msg)
    print("  两种方式   :", "; ".join(MODE_LABELS.values()))
    print("  命令预设   :", len(PRESETS), "条")
    return 0


if __name__ == "__main__":
    sys.exit(selftest())
