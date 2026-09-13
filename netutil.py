# -*- coding: utf-8 -*-
"""网络小工具：按行收发 JSON、枚举本机网卡、计算广播地址。

全部只依赖标准库，打包成 exe 后不需要任何额外运行时。
"""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import re
import socket
import subprocess
import sys
import time

import protocol as P

# Windows 下隐藏子进程控制台窗口（否则 GUI 版 exe 会闪一个黑框）
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_IP_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")
# ARP 表里的一行：  IP   MAC(带横线)  动态/静态
_ARP_RE = re.compile(
    r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+"
    r"([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})\s+\S+"
)


# ---------------------------------------------------------------- 收发

def send_msg(sock: socket.socket, obj: dict) -> None:
    """发送一条 JSON 消息，以换行结尾（行协议，方便对端 readline）。"""
    sock.sendall(P.dumps(obj) + b"\n")


def recv_line(sock: socket.socket, limit: int = P.HEADER_MAX) -> bytes | None:
    """逐字节读取直到换行。

    只用于读取很小的 JSON 头（几百字节），因此逐字节读的额外开销可以忽略，
    好处是绝不会像 makefile() 那样预读并吞掉后面的二进制图片数据。
    """
    buf = bytearray()
    while len(buf) < limit:
        try:
            b = sock.recv(1)
        except OSError:
            return None
        if not b:
            return None
        if b == b"\n":
            return bytes(buf)
        if b != b"\r":
            buf += b
    return None


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """精确读取 n 个字节，不足则抛错。"""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(P.CHUNK, n - len(buf)))
        if not chunk:
            raise ConnectionError(f"连接提前中断，还差 {n - len(buf)} 字节")
        buf += chunk
    return bytes(buf)


# ================================================================ 网卡枚举
#
# 为什么不能只用 socket.gethostname() / 255.255.255.255？
#   1. gethostname() 只返回**一个**（最多两个）地址，多网卡机器（有线 + 无线 +
#      虚拟网卡）上的有线网段会被完全漏掉；
#   2. 255.255.255.255 是「受限广播」，Windows 只会从**默认路由那张网卡**发出去。
#      如果默认路由是 WiFi（或者某张虚拟网卡），广播就永远到不了有线网段。
# 所以这里老老实实把每张网卡的地址 + 前缀长度都枚举出来，逐个网段发定向广播
# （192.168.1.255 这种），并且发送时绑定到该网卡自己的地址，强制从这张网卡出去。

# 适配器类型 IF_TYPE（见 ipifcons.h）
_IF_TYPE_ETHERNET_CSMACD = 6
_IF_TYPE_PPP = 23
_IF_TYPE_SOFTWARE_LOOPBACK = 24
_IF_TYPE_IEEE80211 = 71          # 无线网卡
_IF_TYPE_TUNNEL = 131
_IF_TYPE_IEEE1394 = 144

KIND_WIRED = "wired"
KIND_WIRELESS = "wireless"
KIND_LOOPBACK = "loopback"
KIND_VIRTUAL = "virtual"
KIND_TUNNEL = "tunnel"
KIND_OTHER = "other"

KIND_NAMES = {
    KIND_WIRED: "有线",
    KIND_WIRELESS: "无线",
    KIND_LOOPBACK: "回环",
    KIND_VIRTUAL: "虚拟",
    KIND_TUNNEL: "隧道",
    KIND_OTHER: "其他",
}

# 枚举结果的显示/扫描优先级：有线排最前（客户机绝大多数是有线）
_KIND_ORDER = {
    KIND_WIRED: 0,
    KIND_WIRELESS: 1,
    KIND_OTHER: 2,
    KIND_TUNNEL: 3,
    KIND_VIRTUAL: 4,
    KIND_LOOPBACK: 5,
}

# 名字/描述里出现这些词的，一律按「虚拟」处理：
# 它们要么没有真实的局域网连通性，要么只会把广播丢进没有对端的虚拟交换机。
_VIRTUAL_HINTS = (
    "virtual", "vmware", "hyper-v", "vethernet", "virtualbox", "vbox",
    "tap-", "tun", "npcap", "loopback", "pseudo", "docker", "wsl",
    "bluetooth", "蓝牙", "vpn", "虚拟", "teredo", "isatap", "6to4",
    "wan miniport", "wi-fi direct", "wifi direct", "direct virtual",
)
_WIRELESS_HINTS = ("wi-fi", "wifi", "wlan", "wireless", "802.11", "无线")
_WIRED_HINTS = (
    "ethernet", "以太网", "本地连接", "local area connection",
    "realtek pcie", "gbe", "gigabit",
)


@dataclasses.dataclass(frozen=True)
class Adapter:
    """一张网卡的 IPv4 配置。"""

    ip: str
    prefix: int = 24
    name: str = ""            # 适配器名称（控制面板里显示的那个）
    desc: str = ""            # 驱动描述，例如 Intel(R) Ethernet Connection
    kind: str = KIND_OTHER
    up: bool = True
    if_index: int = 0

    # ---------------- 派生属性

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(f"{self.ip}/{self.prefix}", strict=False)

    @property
    def netmask(self) -> str:
        return str(self.network.netmask)

    @property
    def broadcast(self) -> str:
        """本网段的定向广播地址，例如 192.168.1.255。"""
        return str(self.network.broadcast_address)

    @property
    def is_link_local(self) -> bool:
        """169.254.x.x —— 没有拿到 DHCP 时的自动地址，不能用来通信。"""
        return self.ip.startswith("169.254.")

    @property
    def usable(self) -> bool:
        """能不能用来做局域网广播/扫描。"""
        return (
            self.up
            and self.kind != KIND_LOOPBACK
            and not self.is_link_local
            and 1 <= self.prefix <= 30
        )

    @property
    def host_count(self) -> int:
        return max(0, self.network.num_addresses - 2)

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, "其他")

    @property
    def title(self) -> str:
        return self.name or self.desc or "未命名网卡"

    def describe(self) -> str:
        """一行人类可读的说明，用于日志和自检输出。"""
        detail = self.title
        if self.desc and self.desc != self.title:
            detail += f" / {self.desc}"
        state = "" if self.up else "（已断开）"
        return (f"{self.kind_name} {self.ip}/{self.prefix} → 广播 "
                f"{self.broadcast}  [{detail}]{state}")


def _classify(if_type: int, name: str, desc: str) -> str:
    """按适配器类型 + 名称/描述关键字判断有线 / 无线 / 虚拟。"""
    text = f"{name} {desc}".lower()
    if if_type == _IF_TYPE_SOFTWARE_LOOPBACK or "loopback" in text or "回环" in text:
        return KIND_LOOPBACK
    if any(h in text for h in _VIRTUAL_HINTS):
        return KIND_VIRTUAL
    if if_type == _IF_TYPE_IEEE80211 or any(h in text for h in _WIRELESS_HINTS):
        return KIND_WIRELESS
    if if_type in (_IF_TYPE_TUNNEL, _IF_TYPE_PPP):
        return KIND_TUNNEL
    if if_type == _IF_TYPE_ETHERNET_CSMACD or any(h in text for h in _WIRED_HINTS):
        return KIND_WIRED
    return KIND_OTHER


# ---------------------------------------------------------------- 方式一：iphlpapi

def _adapters_ctypes() -> list[Adapter]:
    """用 GetAdaptersAddresses 枚举（最准：带前缀长度、类型、在线状态）。

    失败时返回空列表，由调用方退回到 ipconfig 解析。
    """
    if sys.platform != "win32":
        return []
    try:
        import ctypes
    except Exception:
        return []

    AF_INET = 2
    GAA_FLAG_SKIP_ANYCAST = 0x0002
    GAA_FLAG_SKIP_MULTICAST = 0x0004
    GAA_FLAG_SKIP_DNS_SERVER = 0x0008
    GAA_FLAG_INCLUDE_PREFIX = 0x0010
    ERROR_BUFFER_OVERFLOW = 111

    class SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.c_void_p),
                    ("iSockaddrLength", ctypes.c_int)]

    class IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
        pass

    IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
        ("Length", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
        ("Next", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
        ("Address", SOCKET_ADDRESS),
        ("PrefixOrigin", ctypes.c_int),
        ("SuffixOrigin", ctypes.c_int),
        ("DadState", ctypes.c_int),
        ("ValidLifetime", ctypes.c_uint32),
        ("PreferredLifetime", ctypes.c_uint32),
        ("LeaseLifetime", ctypes.c_uint32),
        ("OnLinkPrefixLength", ctypes.c_uint8),
    ]

    class IP_ADAPTER_ADDRESSES(ctypes.Structure):
        pass

    IP_ADAPTER_ADDRESSES._fields_ = [
        ("Length", ctypes.c_uint32),
        ("IfIndex", ctypes.c_uint32),
        ("Next", ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", ctypes.c_wchar_p),
        ("Description", ctypes.c_wchar_p),
        ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
        ("Mtu", ctypes.c_uint32),
        ("IfType", ctypes.c_uint32),
        ("OperStatus", ctypes.c_int),
        ("Ipv6IfIndex", ctypes.c_uint32),
        ("ZoneIndices", ctypes.c_uint32 * 16),
        ("FirstPrefix", ctypes.c_void_p),
    ]

    try:
        iphlpapi = ctypes.WinDLL("iphlpapi.dll")
        func = iphlpapi.GetAdaptersAddresses
    except Exception:
        return []

    func.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.POINTER(IP_ADAPTER_ADDRESSES), ctypes.POINTER(ctypes.c_uint32),
    ]
    func.restype = ctypes.c_uint32

    flags = (GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST
             | GAA_FLAG_SKIP_DNS_SERVER | GAA_FLAG_INCLUDE_PREFIX)

    size = ctypes.c_uint32(16 * 1024)
    buf = None
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        ret = func(AF_INET, flags, None,
                   ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
                   ctypes.byref(size))
        if ret == 0:
            break
        if ret != ERROR_BUFFER_OVERFLOW:
            return []
    else:
        return []

    out: list[Adapter] = []
    node = ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
    guard = 0
    while node and guard < 512:
        guard += 1
        ad = node.contents
        name = str(ad.FriendlyName or "")
        desc = str(ad.Description or "")
        kind = _classify(int(ad.IfType), name, desc)
        up = int(ad.OperStatus) == 1          # IfOperStatusUp

        uni = ad.FirstUnicastAddress
        guard2 = 0
        while uni and guard2 < 64:
            guard2 += 1
            ua = uni.contents
            sa = ua.Address
            if sa.lpSockaddr and int(sa.iSockaddrLength) >= 8:
                raw = ctypes.string_at(sa.lpSockaddr, 16)
                if raw and raw[0] == AF_INET:
                    ip = socket.inet_ntoa(raw[4:8])
                    plen = int(ua.OnLinkPrefixLength)
                    if not 1 <= plen <= 32:
                        plen = 24
                    out.append(Adapter(
                        ip=ip, prefix=plen, name=name, desc=desc,
                        kind=kind, up=up, if_index=int(ad.IfIndex),
                    ))
            uni = ua.Next
        node = ad.Next
    return out


# ---------------------------------------------------------------- 方式二：ipconfig

def _run_ipconfig() -> str:
    """跑一次 ipconfig 并解码输出（中文系统是 GBK）。"""
    try:
        proc = subprocess.run(
            ["ipconfig"], capture_output=True, timeout=6,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        return ""
    for enc in ("gbk", "utf-8", "cp936"):
        try:
            text = proc.stdout.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if text.strip():
            return text
    return proc.stdout.decode("utf-8", "ignore")


def _adapters_ipconfig() -> list[Adapter]:
    """解析 ipconfig 输出（ctypes 不可用时的兜底）。

    ipconfig 按「网卡」分段，段首是不缩进、以冒号结尾的标题行，例如
    `以太网适配器 以太网:` / `无线局域网适配器 WLAN:`。段内第一行 IP 是
    IPv4 地址，紧随其后的「子网掩码」是掩码，因此按段解析比全局按行配对稳。
    """
    text = _run_ipconfig()
    if not text.strip():
        return []

    blocks: list[tuple[str, list[str]]] = []
    title, lines = "", []
    for line in text.splitlines():
        if line.strip() and not line[:1].isspace() and line.rstrip().endswith(":"):
            if title or lines:
                blocks.append((title, lines))
            title, lines = line.rstrip().rstrip(":").strip(), []
        else:
            lines.append(line)
    if title or lines:
        blocks.append((title, lines))

    out: list[Adapter] = []
    for title, lines in blocks:
        if not title:
            continue
        ip = mask = desc = ""
        up = True
        for line in lines:
            low = line.lower()
            found = _IP_RE.findall(line)
            if not found:
                continue
            value = found[0]
            if "掩码" in line or "mask" in low:
                if ip and not mask:
                    mask = value
            elif "描述" in line or "description" in low:
                desc = line.split(":", 1)[-1].strip()
            elif ("ipv4" in low or "ip address" in low or "ip 地址" in line) and not ip:
                ip = value
        body = "\n".join(lines)
        if "媒体已断开" in body or "media disconnected" in body.lower():
            up = False
        if not ip:
            continue
        try:
            prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen if mask else 24
        except Exception:
            prefix = 24
        # 标题形如「以太网适配器 以太网」，取后半段当网卡名
        name = title.split()[-1] if title.split() else title
        out.append(Adapter(ip=ip, prefix=prefix, name=name, desc=desc,
                           kind=_classify(0, title + " " + desc, desc), up=up))
    return out


# ---------------------------------------------------------------- 方式三：主机名

def _adapters_hostname() -> list[Adapter]:
    """最后的兜底：gethostname() 解析出的地址，前缀按 /24 猜。"""
    out: list[Adapter] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return out
    for info in infos:
        ip = info[4][0]
        if ip and ip != "0.0.0.0":
            out.append(Adapter(ip=ip, prefix=24, name="(主机名解析)",
                               desc="", kind=KIND_OTHER))
    return out


def list_adapters(include_down: bool = False) -> list[Adapter]:
    """枚举本机所有 IPv4 网卡，有线/无线都包含，按「有线优先」排序。"""
    ads = _adapters_ctypes() or _adapters_ipconfig() or _adapters_hostname()
    out: list[Adapter] = []
    seen: set[tuple[str, int]] = set()
    for ad in ads:
        if not include_down and not ad.up:
            continue
        key = (ad.ip, ad.prefix)
        if key in seen:
            continue
        seen.add(key)
        out.append(ad)
    out.sort(key=lambda a: (_KIND_ORDER.get(a.kind, 9), a.ip))
    return out


# ---------------------------------------------------------------- 广播地址

def local_ipv4_list() -> list[str]:
    """尽力列出本机所有 IPv4 地址（不含 169.254 自动地址）。"""
    ips: list[str] = []
    for ad in list_adapters(include_down=True):
        if ad.ip not in ips and not ad.is_link_local:
            ips.append(ad.ip)
    if ips:
        return ips
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("169.254."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def _ipconfig_pairs() -> list[tuple[str, str]]:
    """(IPv4, 子网掩码) 配对，保留给旧代码/调试使用。"""
    return [(a.ip, a.netmask) for a in _adapters_ipconfig()]


def subnet_broadcasts(adapters: list[Adapter] | None = None) -> list[str]:
    """各网段的定向广播地址，例如 192.168.1.255（前缀长度按网卡真实值算）。"""
    out: list[str] = []
    for ad in (adapters if adapters is not None else list_adapters()):
        if ad.usable:
            b = ad.broadcast
            if b not in out:
                out.append(b)
    return out


def broadcast_targets(extra: list[str] | None = None,
                      adapters: list[Adapter] | None = None) -> list[str]:
    """汇总一次广播要发送到的全部目标地址。

    包含：
      * 255.255.255.255 —— 受限广播（控制端会从每张网卡各发一份）
      * 各网段的定向广播 —— 有线 / 无线 / 虚拟网卡全覆盖，多网卡机器才不漏
      * 127.0.0.1 —— 方便同一台电脑上同时跑控制端和被控端做自测
      * 用户在配置里手填的地址 —— 跨网段 / 指定 IP 的场景
    """
    out: list[str] = []

    def add(addr: str) -> None:
        addr = (addr or "").strip()
        if addr and addr not in out:
            out.append(addr)

    add("255.255.255.255")
    for b in subnet_broadcasts(adapters):
        add(b)
    add("127.0.0.1")
    for e in extra or []:
        for piece in re.split(r"[,;\s]+", str(e)):
            add(piece)
    return out


# ---------------------------------------------------------------- 扫描候选

def arp_neighbors() -> list[str]:
    """ARP 邻居表里「本机直连网段内」的 IPv4 地址。

    这些是最近确实通过信的机器（包括以前推过的客户机）。广播被交换机/AP
    拦掉时，拿这份名单做单播探测往往能立刻找回设备。
    """
    try:
        proc = subprocess.run(
            ["arp", "-a"], capture_output=True, timeout=6,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        return []
    text = ""
    for enc in ("gbk", "utf-8"):
        try:
            text = proc.stdout.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if text.strip():
            break
    if not text.strip():
        return []

    adapters = [a for a in list_adapters() if a.usable]
    mine = set(local_ipv4_list())
    out: list[str] = []
    for line in text.splitlines():
        m = _ARP_RE.match(line)
        if not m:
            continue
        ip, mac = m.group(1), m.group(2).lower()
        if ip in mine or mac == "ff-ff-ff-ff-ff-ff" or mac.startswith("01-00-5e"):
            continue
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        # 只收本机直连网段内的地址，避免把单播探测打到网段外
        if any(addr in a.network for a in adapters) and ip not in out:
            out.append(ip)
    return out


def sweep_hosts(adapters: list[Adapter] | None = None,
                max_hosts: int = 1022,
                include_arp: bool = True) -> tuple[list[str], list[str]]:
    """算出「逐台单播探测」要发的地址列表。

    返回 (地址列表, 说明列表)。广播在不少交换机 / AP（客户端隔离）上是被拦掉的，
    这时挨个单播一发就能把设备找回来 —— 被控端对单播 ping 一样会应答。

    网段太大（例如 /16，6 万多台）时不会全扫，只扫本机所在的那个 /24，
    其余交给定向广播和 ARP 名单。
    """
    ads = adapters if adapters is not None else list_adapters()
    hosts: list[str] = []
    notes: list[str] = []
    # 不探测自己：Windows 上绑定在同一个 UDP 端口的多个套接字会「抢」单播包，
    # 发给自己的包可能被本进程的其它套接字吃掉，白等一场。本机被控端本来就
    # 能收到 127.0.0.1 那条广播，不需要靠单播。
    mine = set(local_ipv4_list())

    def add(ip: str) -> None:
        if ip not in hosts and ip not in mine:
            hosts.append(ip)

    for ad in ads:
        if not ad.usable:
            continue
        net = ad.network
        if 0 < ad.host_count <= max_hosts:
            for h in net.hosts():
                add(str(h))
            notes.append(f"{ad.kind_name} {ad.ip}/{ad.prefix} 全网段 {ad.host_count} 台")
        else:
            small = ipaddress.IPv4Network(f"{ad.ip}/24", strict=False)
            for h in small.hosts():
                add(str(h))
            notes.append(
                f"{ad.kind_name} {ad.ip}/{ad.prefix} 网段过大（{ad.host_count} 台），"
                f"只扫本机所在 {small} 这 254 个地址，其余靠定向广播 "
                f"{ad.broadcast}"
            )

    if include_arp:
        extra = arp_neighbors()
        new = [ip for ip in extra if ip not in hosts]
        for ip in new:
            add(ip)
        if new:
            notes.append(f"ARP 名单补充 {len(new)} 台曾经通信过的机器")
    return hosts, notes


def adapter_report(adapters: list[Adapter] | None = None) -> list[str]:
    """每张网卡一行的可读说明（日志 / 自检用）。"""
    ads = adapters if adapters is not None else list_adapters()
    if not ads:
        return ["（未检测到任何网卡）"]
    return ["  " + ad.describe() for ad in ads]


def adapter_summary(adapters: list[Adapter] | None = None) -> str:
    """一行摘要，例如「有线 1 · 无线 1 ｜ 10.13.3.214/16」。"""
    ads = adapters if adapters is not None else list_adapters()
    usable = [a for a in ads if a.usable]
    if not usable:
        return ""
    counts: dict[str, int] = {}
    for a in usable:
        counts[a.kind_name] = counts.get(a.kind_name, 0) + 1
    head = " · ".join(f"{k} {v}" for k, v in
                      sorted(counts.items(), key=lambda kv: kv[0]))
    body = "、".join(f"{a.ip}/{a.prefix}({a.title})" for a in usable[:4])
    return f"{head} ｜ {body}"


# ---------------------------------------------------------------- 跑外部命令

def run_hidden(args: list[str], timeout: float = 20.0) -> tuple[int, str]:
    """跑一个控制台命令并取回输出（Windows 上不闪黑框）。

    返回 (退出码, 输出文本)；命令根本起不来时返回 (-1, 错误说明)。
    输出按系统代码页解码（中文 Windows 上是 GBK）。
    """
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout,
                              creationflags=_NO_WINDOW)
    except Exception as e:
        return -1, str(e)
    text = ""
    for enc in ("gbk", "utf-8"):
        try:
            text = proc.stdout.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not text:
        text = proc.stdout.decode("utf-8", "ignore")
    err = ""
    if proc.stderr:
        err = proc.stderr.decode("gbk", "ignore")
    return proc.returncode, (text + err).strip()


# ---------------------------------------------------------------- 日志文件

class FileLogger:
    """把日志同时落一份到文件。

    为什么必须有：被控端装成 MSI 之后是「无窗口 + 静默」后台运行，print 出去
    的东西直接进黑洞。用户报「装了没反应」时，没有日志文件就只能靠猜。
    日志超过上限就滚动一次，只留一份备份，不会把磁盘塞满。

    实例本身可以直接当 log 回调用：`core.log = FileLogger(path)`。
    """

    def __init__(self, path: str, max_kb: int = 512, backup: int = 1):
        import threading

        self.path = path
        self.max_bytes = max(1, int(max_kb)) * 1024
        self.backup = max(0, int(backup))
        self._lock = threading.Lock()
        self._fh = None
        self._size = 0
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._size = os.path.getsize(path) if os.path.isfile(path) else 0
        except OSError:
            pass

    # ---------------- 内部

    def _rotate(self) -> None:
        """写满了就把旧日志挪成 .1，重开一个空文件。"""
        try:
            if self._fh:
                self._fh.close()
        except OSError:
            pass
        self._fh = None
        try:
            if self.backup:
                old = f"{self.path}.1"
                if os.path.isfile(old):
                    os.remove(old)
                if os.path.isfile(self.path):
                    os.replace(self.path, old)
            else:
                os.remove(self.path)
        except OSError:
            pass
        self._size = 0

    def _write(self, line: str) -> None:
        try:
            if self._fh is None:
                self._fh = open(self.path, "a", encoding="utf-8", errors="replace")
            self._fh.write(line)
            self._fh.flush()
            self._size += len(line.encode("utf-8", "replace"))
        except OSError:
            self._fh = None          # 写不了就算了，绝不能因为日志把程序弄死

    # ---------------- 对外

    def __call__(self, msg, level: str = "info") -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        mark = {"ok": "OK  ", "err": "ERR ", "warn": "WARN", "dim": "    "}.get(level, "    ")
        line = f"[{stamp}] [{mark}] {msg}\n"
        with self._lock:
            if self._size + len(line.encode("utf-8", "replace")) > self.max_bytes:
                self._rotate()
            self._write(line)

    def header(self, text: str) -> None:
        """写一行分隔标题（每次启动写一条，方便区分不同次运行）。"""
        with self._lock:
            self._write(f"\n===== {text} =====\n")

    def close(self) -> None:
        with self._lock:
            try:
                if self._fh:
                    self._fh.close()
            except OSError:
                pass
            self._fh = None


def log_dir(app: str) -> str:
    """日志目录 %LOCALAPPDATA%\\<app>（永远可写，卸载 MSI 不会连日志一起删）。"""
    return local_app_data_dir(app)


# ---------------------------------------------------------------- 其他

def app_dir() -> str:
    """返回「程序所在目录」。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def program_data_dir(app: str) -> str:
    """机器级共享目录 C:\\ProgramData\\<app>（MSI 部署时配置放这里）。"""
    base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return os.path.join(base, app)


def local_app_data_dir(app: str) -> str:
    """当前用户私有目录 %LOCALAPPDATA%\\<app>（兜底，永远可写）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, app)


def is_dir_writable(path: str) -> bool:
    """目录是否可写（必要时先创建）。用来判断配置该往哪儿落。"""
    probe = os.path.join(path, f".wpp_write_probe_{os.getpid()}")
    try:
        os.makedirs(path, exist_ok=True)
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


def _stream_usable(stream) -> bool:
    """这个标准流有没有真正可写的句柄。

    无控制台打包的 exe 双击运行时 sys.stdout 是 None；但在 cmd 里
    `app.exe --status > out.txt` 这种重定向情况下，句柄是有效的文件 ——
    这时绝不能拿 CONOUT$ 覆盖它，否则重定向 / 管道就永远拿不到输出。
    """
    try:
        if stream is None:
            return False
        stream.fileno()
        return True
    except Exception:
        return False


def attach_parent_console() -> bool:
    """让「无控制台」方式打包的 exe 也能在 cmd 里输出文字。

    PyInstaller 的 --noconsole 会让 exe 没有自己的控制台，print 的内容
    全部丢弃。但用户又需要 --selftest / --status / --push 这类命令行功能，
    所以在检测到带了命令行参数时，主动把自己挂到「父进程（cmd 窗口）」的
    控制台上。

    已经被重定向的标准流（`> 文件` / 管道）保持原样，不会被控制台覆盖。

    返回 True 表示挂载成功。双击运行时没有父控制台，会安静地返回 False。
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ATTACH_PARENT_PROCESS = -1
        if not kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            return False

        # 用控制台当前的代码页解码，避免中文在 GBK 的 cmd 里变成乱码。
        # 设置了 PYTHONIOENCODING 时以它为准（方便在管道/CI 里强制指定）。
        forced = (os.environ.get("PYTHONIOENCODING") or "").split(":")[0].strip()
        cp = kernel32.GetConsoleOutputCP()
        enc = forced or (f"cp{cp}" if cp else "utf-8")
        if not _stream_usable(sys.stdout):
            sys.stdout = open("CONOUT$", "w", encoding=enc, errors="replace", buffering=1)
        if not _stream_usable(sys.stderr):
            sys.stderr = open("CONOUT$", "w", encoding=enc, errors="replace", buffering=1)
        if not _stream_usable(sys.stdin):
            sys.stdin = open("CONIN$", "r", encoding=enc, errors="replace")
        return True
    except Exception:
        return False


def load_json(path: str) -> dict:
    import json
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json(path: str, obj: dict) -> None:
    import json
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
