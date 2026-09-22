#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制端

职责：
    1. 选择一张壁纸图片，计算 sha256
    2. 用 UDP 广播「新壁纸公告」到局域网
    3. 起一个 TCP 服务，被控端回连时把图片发过去
    4. 收集被控端的在线回应与执行结果，实时显示设备列表

用法：
    python controller.py                         # 带界面运行
    python controller.py --push 图.jpg --wait 8  # 命令行推送（方便脚本化/自测）
    python controller.py --scan --wait 3         # 只扫描在线设备
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import queue
import socket
import sys
import threading
import time
import uuid

import netutil as N
import protocol as P
import sshcmd as SC
import toastspec as TS

APP_NAME = "Win壁纸推送 - 控制端"
APP_VER = "1.3.0"

ONLINE_WINDOW = 90.0   # 多少秒内有回应算「在线」
# 同一台设备隔这么久没动静又重新回应，就在日志里说一句「重新上线」。
# 取 90 秒（= ONLINE_WINDOW）：被控端每 60 秒主动报到一次，不能因此刷屏。
REDISCOVER_QUIET = 90.0

DEFAULT_CFG = {
    "udp_port": P.UDP_PORT,
    "tcp_port": P.TCP_PORT,
    "reply_port": P.REPLY_PORT,
    "style": P.DEFAULT_STYLE,
    "targets": [],        # 额外广播地址，跨网段时手填，例如 ["192.168.2.255"]
    "sweep": True,        # 深度扫描：逐台单播探测（广播被交换机/AP 拦掉时靠它）
    "auto_scan": 30.0,    # 秒；后台每隔这么久自动重扫一次，0 = 关闭
    "last_dir": "",
    "last_file": "",
    # 远程命令（SSH）那一页的设置。默认值就是用户给的原始命令那套
    # （同一批机器、同一把私钥），打开界面就能直接用。
    "ssh": SC.defaults(),
}


# ================================================================ 核心逻辑

class ControllerCore:
    """控制端的全部业务逻辑，与界面解耦。"""

    def __init__(self, cfg: dict, log=None, on_event=None):
        self.cfg = cfg
        self.udp_port = int(cfg.get("udp_port") or P.UDP_PORT)
        self.tcp_port = int(cfg.get("tcp_port") or P.TCP_PORT)
        self.reply_port = int(cfg.get("reply_port") or P.REPLY_PORT)
        self.style = P.normalize_style(cfg.get("style")) or P.DEFAULT_STYLE
        self.extra_targets = list(cfg.get("targets") or [])
        self.sweep = bool(cfg.get("sweep", True))
        self.auto_scan = max(0.0, float(cfg.get("auto_scan") or 0))
        # 在线判定窗口：远程命令页要用它区分「在线设备」和「全部已发现」
        self.online_window = ONLINE_WINDOW
        # 远程命令（SSH）设置：界面和命令行都从这里取
        self.ssh_cfg = SC.normalize_cfg(cfg.get("ssh"))

        self.log = log or (lambda msg, level="info": None)
        self.on_event = on_event or (lambda kind, data: None)

        self._sock: socket.socket | None = None
        self._reply_sock: socket.socket | None = None
        self._srv: socket.socket | None = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()

        self._payload: dict | None = None        # 当前待分发的壁纸
        self._tasks: dict[str, dict] = {}        # task -> 信息
        self._acks: dict[str, dict[str, tuple]] = {}   # task -> {ip: (ok, err, ts)}
        self.devices: dict[str, dict] = {}       # ip -> 设备信息
        self._dev_lock = threading.Lock()
        self.targets: list[str] = []
        # 用户手填的网段里要「逐台探测」的地址（由 parse_target_spec 算出来）
        self.spec_hosts: list[str] = []
        # 本机自己的地址（懒加载）：用来把"控制端自己"并成一条设备
        self._self_ips: set[str] | None = None
        # 通知用的图片：{task: {资源名: 字节}}，被控端按需用 TCP 来拉
        self._toast_assets: dict[str, dict[str, bytes]] = {}
        self.last_toast_task = ""

        # 网卡相关：每张网卡一个「绑定到它自己地址」的发送套接字，
        # 这样广播才会真的从有线网卡出去，而不是全交给默认路由（通常是 WiFi）。
        self.adapters: list[N.Adapter] = []
        self._send_socks: dict[str, socket.socket] = {}
        self._auto_thread: threading.Thread | None = None
        self._last_scan = 0.0
        self.last_sweep_count = 0

    # ---------------------------------------------------------- 启动

    def start(self) -> None:
        # 广播口：必须 SO_REUSEADDR，才能和本机被控端同时收到广播包
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.bind(("0.0.0.0", self.udp_port))

        # 回执口：独占，专门收被控端的单播确认，避免与广播口抢包
        try:
            self._reply_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._reply_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._reply_sock.bind(("0.0.0.0", self.reply_port))
        except OSError as e:
            self.log(f"回执端口 {self.reply_port} 绑定失败（{e}），改为在广播口收确认", "warn")
            self._reply_sock = None

        # 收包缓冲加大：几十台机器会在同一两秒里一起回执（push 之后），
        # 默认 64KB 的缓冲很容易溢出 —— 表现就是"在线但没回执"，而客户端其实发了。
        for s in (self._sock, self._reply_sock):
            if not s:
                continue
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
            except OSError:
                pass

        for s in (self._sock, self._reply_sock):
            if s:
                threading.Thread(target=self._udp_loop, args=(s,), daemon=True).start()
        threading.Thread(target=self._tcp_loop, name="tcp", daemon=True).start()

        self.refresh_targets()
        self.log(
            f"控制端已启动（广播 {self.udp_port} · 回执 {self.reply_port} · "
            f"传输 TCP {self.tcp_port}）",
            "ok",
        )
        self._start_auto_scan()

    def refresh_targets(self) -> list[str]:
        """重新枚举网卡并计算广播目标（换了网络环境后可以重新点一下）。

        有线、无线、虚拟网卡一个都不落下：只有把每张网卡的定向广播地址都
        发一遍，客户机在哪张网卡上都能收到。

        「额外网段」支持四种写法（见 netutil.parse_target_spec）：
        `192.168.1.0/24`、`192.168.1.255`、单台 IP、地址范围。
        """
        self.adapters = N.list_adapters()
        # 手填的网段/地址：解析出「发到哪儿」和「逐台探测哪些地址」
        spec_send, spec_hosts, spec_notes = N.parse_target_spec(
            " ".join(self.extra_targets))
        self.spec_hosts = spec_hosts
        # 只把**解析出来的**地址当发送目标：以前这里会把手填的原始字符串
        # 直接塞进目标列表，于是 `192.168.1.2-10.127.112.9` 这种范围写法
        # 会被当成域名去解析（日志里一堆 getaddrinfo failed）
        self.targets = N.broadcast_targets(spec_send, adapters=self.adapters)
        # 换发送套接字时和正在广播的线程（自动重扫 / 推送）串行，
        # 否则可能关掉它正要用的那个 socket
        with self._send_lock:
            self._close_egress_sockets()
            self._open_egress_sockets()

        usable = [a for a in self.adapters if a.usable]
        if usable:
            for line in N.adapter_report(self.adapters):
                self.log("网卡 " + line.strip(), "dim")
        else:
            self.log("没检测到可用的局域网网卡（只剩回环 / 169.254 自动地址），"
                     "请检查网线或 WiFi 是否连通", "warn")
        for ad in self.adapters:
            if not ad.usable:
                self.log(f"跳过网卡 {ad.ip or '（无地址）'}（{ad.title}）："
                         f"{'已断开' if not ad.up else '不可用于局域网广播'}", "dim")
        for note in spec_notes:
            self.log("额外网段：" + note, "ok" if "忽略" not in note else "warn")
        if self.extra_targets and not spec_send:
            self.log("额外网段里没有「发送目标」（只有探测范围），"
                     "广播仍走本机网段 —— 这些地址会逐台单播探测", "dim")
        self.log(f"广播目标 {len(self.targets)} 个：" + "，".join(self.targets), "dim")
        if not self.sweep:
            self.log("深度扫描已关闭（设置页可打开）：只发广播，不逐台单播探测 —— "
                     "交换机 / 无线 AP 拦广播时，设备会扫不全", "warn")
        return self.targets

    # ---------------------------------------------------------- 每张网卡一个发送通道

    def _open_egress_sockets(self) -> None:
        """给每张可用网卡建一个绑定到它自己地址的发送套接字。

        绑定源地址 = 强制这张包走这张网卡。不这么做的话，发往
        255.255.255.255 的广播只会从「默认路由」那张网卡出去，
        多网卡机器（笔记本同时插网线 + 连 WiFi）就会漏掉一半客户机。
        """
        if self._sock is None:
            return
        for ad in self.adapters:
            if not ad.usable or ad.ip in self._send_socks:
                continue
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((ad.ip, 0))
                self._send_socks[ad.ip] = s
            except OSError as e:
                self.log(f"网卡 {ad.ip}（{ad.title}）打开发送通道失败：{e}，"
                         f"该网段只能走默认路由", "warn")

    def _close_egress_sockets(self) -> None:
        for s in self._send_socks.values():
            try:
                s.close()
            except OSError:
                pass
        self._send_socks.clear()

    def _egress_for(self, target: str) -> list[socket.socket]:
        """选出发往 target 要用哪张网卡的套接字。"""
        fallback = [self._sock] if self._sock is not None else []
        if not self._send_socks:
            return fallback
        if target == "255.255.255.255":
            # 受限广播：每张网卡都发一份，否则只会走默认路由那一张
            return list(self._send_socks.values())
        try:
            addr = ipaddress.IPv4Address(target)
        except ValueError:
            return fallback          # 不是 IP，交给系统路由
        # 同一网段可能同时挂在两张网卡上（有线 + WiFi），那就两张都发
        socks = [self._send_socks[a.ip] for a in self.adapters
                 if a.usable and a.ip in self._send_socks and addr in a.network]
        return socks or fallback

    def _start_auto_scan(self) -> None:
        """后台定时重扫：客户机后来才开机也能自己冒出来。"""
        if self._auto_thread is not None:
            return

        def loop():
            tick = 5.0
            while not self._stop.wait(tick):
                interval = self.auto_scan
                if interval <= 0:
                    continue
                if time.time() - self._last_scan < interval:
                    continue
                try:
                    self.scan(deep=False)   # 定时扫描只广播，省流量
                except Exception as e:
                    self.log(f"自动扫描出错：{e}", "err")

        self._auto_thread = threading.Thread(target=loop, name="auto-scan", daemon=True)
        self._auto_thread.start()

    def stop(self) -> None:
        self._stop.set()
        for s in (self._sock, self._reply_sock, self._srv):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        self._close_egress_sockets()

    # ---------------------------------------------------------- UDP

    def _udp_loop(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except OSError:
                break
            except Exception as e:
                self.log(f"接收异常：{e}", "err")
                continue
            try:
                self._dispatch(data, addr)
            except Exception as e:
                self.log(f"处理消息出错：{e}", "err")

    def _dispatch(self, data: bytes, addr) -> None:
        msg = P.loads(data)
        if not msg:
            return
        mtype = msg.get("type")
        ip = addr[0]

        if mtype in (P.MSG_PONG, P.MSG_HELLO):
            host = msg.get("host", "")
            status = self._touch_device(
                ip, host, msg.get("user", ""),
                msg.get("applied", ""), msg.get("count", 0),
            )
            who = "  ".join(x for x in (ip, host, str(msg.get("user", ""))) if x)
            if status == "new":
                self.log(f"{'新设备上线' if mtype == P.MSG_HELLO else '发现设备'}：{who}", "ok")
            elif status == "back":
                self.log(f"设备重新上线：{who}", "dim")

        elif mtype == P.MSG_APPLIED:
            task = str(msg.get("task") or "")
            ok = bool(msg.get("ok"))
            err = str(msg.get("err") or "")
            host = msg.get("host", "")
            # 回执也按计算机名归并，避免同一台机器两种地址对不上号
            key = self.device_key(ip, host)
            with self._dev_lock:
                self._acks.setdefault(task, {})[key] = (ok, err, time.time(), ip)
            self._touch_device(ip, host, msg.get("user", ""), "", 0)
            kind = (self._tasks.get(task) or {}).get("kind")
            if ok:
                self.log(f"✅ {ip} {'已弹出通知' if kind == 'toast' else '已应用壁纸'}",
                         "ok")
            else:
                self.log(f"❌ {ip} {'通知失败' if kind == 'toast' else '应用失败'}：{err}",
                         "err")
            self.on_event("ack", {"ip": ip, "task": task, "ok": ok, "err": err})

    @staticmethod
    def device_key(ip: str, host: str = "") -> str:
        """设备唯一标识 = **IP**。

        以前是按计算机名归并的，本意是"同一台机器有多张网卡时只算一台"，
        但克隆镜像 / 同批装机的客户机**经常同名** —— 结果 50 台机器在设备列表里
        被合并成 1 行（用户实际遇到的问题：日志里几十台，列表里只有一个）。
        改成按 IP：
          * 同一个 IP 一定是一台机器；
          * 同名不同 IP 就是不同的机器，分开显示、分开统计回执。
        唯一例外：本机自测时被控端会同时从 127.0.0.1 和局域网 IP 各报一次，
        这种情况在 `_touch_device` 里并成一条。
        """
        return ip

    def _touch_device(self, ip, host, user, applied, count) -> str:
        """登记 / 刷新一台设备。

        返回 'new'（第一次见到）、'back'（离线一阵子后重新出现）或 ''
        （只是又一次例行回应，不必刷日志 —— 自动重扫会不断产生这种回应）。
        """
        key = self.device_key(ip, host)
        now = time.time()
        # 本机自己的地址（127.x / Hyper-V 之类虚拟网卡 / 多张网卡）都算"自己"，
        # 免得控制端把自己扫成好几台设备（现场就见过 172.25.16.1 出现在列表里，
        # 那是控制端自己的 Hyper-V 虚拟网卡）
        with self._dev_lock:
            if self._self_ips is None:
                try:
                    self._self_ips = set(N.local_ipv4_list())
                except Exception:
                    self._self_ips = set()
            self_ips = self._self_ips
        is_self = ip in self_ips or ip.startswith("127.")
        with self._dev_lock:
            d = self.devices.get(key)
            if d is None and is_self:
                # 并到"本机"那一条（优先保留局域网地址那条）
                for k, other in self.devices.items():
                    if host and str(other.get("host") or "").lower() == host.lower():
                        key = k
                        d = other
                        break
            if d is None:
                # 之前可能用过别的 key，直接接管过来，不要留下孤儿行
                d = self.devices.pop(ip, {}) if ip != key else {}
                self.devices[key] = d
                status = "new"
            else:
                status = "back" if now - d.get("last", 0) > REDISCOVER_QUIET else ""
            if host:
                d["host"] = host
            # 127.0.0.1 / 虚拟网卡只用于本机自测，展示时优先用真实局域网地址
            old_ip = d.get("ip") or ""
            if not old_ip or ((old_ip.startswith("127.") or old_ip in self_ips)
                              and not (ip.startswith("127.") or ip in self_ips)):
                d["ip"] = ip
            if user:
                d["user"] = user
            if applied:
                d["applied"] = applied
            if count:
                d["count"] = count
            d["last"] = now
        self.on_event("device", {"key": key, "ip": ip})
        return status

    # ---------------------------------------------------------- TCP 文件服务

    def _tcp_loop(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.tcp_port))
        # 设备多的时候会有几十台同时回连下壁纸：backlog 太小会直接拒掉一部分
        # （客户端那头表现就是"下载失败"，而控制端看不出原因）
        srv.listen(256)
        self._srv = srv
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except OSError:
                break
            threading.Thread(
                target=self._serve_client, args=(conn, addr), daemon=True
            ).start()

    def _serve_client(self, conn: socket.socket, addr) -> None:
        ip = addr[0]
        try:
            conn.settimeout(30)
            raw = N.recv_line(conn)
            msg = P.loads(raw)
            if not msg or msg.get("type") != P.MSG_GET:
                N.send_msg(conn, P.make("err", ok=False, err="非法请求"))
                return

            task = str(msg.get("task") or "")

            # ① 通知用的图片：被控端在 GET 里带上资源名
            asset = str(msg.get(P.FIELD_ASSET) or "")
            if asset:
                data = (self._toast_assets.get(task) or {}).get(asset)
                if data is None:
                    N.send_msg(conn, P.make("err", ok=False,
                                            err=f"通知资源「{asset}」不存在或已过期"))
                    self.log(f"{ip} 请求的通知资源已失效：{asset}", "warn")
                    return
                N.send_msg(conn, P.make(
                    "file", ok=True, name=os.path.basename(asset), size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                ))
                conn.sendall(data)
                self.log(f"已下发通知图片「{asset}」给 {ip}（{len(data) // 1024} KB）", "dim")
                return

            # ② 壁纸（原有逻辑）
            payload = self._payload
            if not payload or payload["task"] != task:
                N.send_msg(conn, P.make("err", ok=False, err="该壁纸任务已失效，请等待下次广播"))
                self.log(f"{ip} 请求的任务已失效", "warn")
                return

            N.send_msg(conn, P.make(
                "file", ok=True, name=payload["name"], size=payload["size"],
                sha256=payload["sha256"], style=self.style,
            ))
            conn.sendall(payload["data"])
            self.log(f"已下发「{payload['name']}」给 {ip}（{payload['size'] // 1024} KB）", "dim")
        except Exception as e:
            self.log(f"向 {ip} 下发失败：{e}", "err")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ---------------------------------------------------------- 广播

    def _send_packet(self, raw: bytes, targets: list[str]) -> int:
        """把一条消息发到一组地址（广播地址 / 单播地址都行），返回成功发出的次数。"""
        sent = 0
        if self._sock is None:
            return 0
        with self._send_lock:
            for t in targets or ["255.255.255.255"]:
                for s in self._egress_for(t):
                    try:
                        s.sendto(raw, (t, self.udp_port))
                        sent += 1
                    except OSError as e:
                        self.log(f"发送到 {t} 失败：{e}", "err")
        return sent

    def known_ips(self) -> list[str]:
        """已经发现过的设备地址（用来直接单播，绕开被拦掉的广播）。"""
        with self._dev_lock:
            return [str(d.get("ip") or k) for k, d in self.devices.items()]
            # 注：不判在线，离线一会儿的机器也值得试一次（它可能刚回来）

    def probe(self, ips: list[str], wait: float = 4.0) -> set[str]:
        """对指定地址各发一次单播 ping，返回在 wait 秒内**回应过**的地址集合。

        用途：推送之后排查"在线但没回执"。能回应的说明"双向网络通"，
        那没回执多半是它压根没收到公告或者下载壁纸失败；连回应都没有的，
        说明那条路不通（离线 / 防火墙 / 跨网段被挡）。
        """
        if not ips or self._sock is None:
            return set()
        before: dict[str, float] = {}
        with self._dev_lock:
            for ip in ips:
                for k, d in self.devices.items():
                    if str(d.get("ip") or k) == ip:
                        before[ip] = float(d.get("last") or 0)
                        break
        nonce = uuid.uuid4().hex[:8]
        msg = P.dumps(P.make(P.MSG_PING, nonce=nonce,
                             reply_port=self.reply_port, ts=time.time()))
        self._send_packet(msg, ips)
        time.sleep(max(0.5, wait))
        alive: set[str] = set()
        with self._dev_lock:
            for ip in ips:
                for k, d in self.devices.items():
                    if str(d.get("ip") or k) == ip:
                        if float(d.get("last") or 0) > before.get(ip, 0):
                            alive.add(ip)
                        break
        return alive

    def announce(self, obj: dict, task: str, rounds: int = 4,
                 gap: float = 5.0, label: str = "") -> None:
        """广播一条公告，**并且直接单播给已知设备**，然后对没回执的机器补发。

        为什么要这样（用户实际遇到的问题）：
          * 广播会被交换机 / 无线 AP / VLAN 隔离拦掉，设备一多总有几台收不到；
          * 光靠广播"谁在线就谁改"，实测里总有机器漏掉。
        所以这里做三层：
          1. 广播（多网卡 × 多发几遍）；
          2. 对**已知设备逐个单播**同一份公告 —— 只要它还在线，单播一定送到；
          3. 隔几秒查一次回执，谁没确认就单独再单播给它（最多 rounds 轮）。
        """
        if self._sock is None:
            return
        if not self.targets:
            self.refresh_targets()

        raw = P.dumps(obj)
        # ① 广播（多发几遍抵消 UDP 丢包）
        self._broadcast(obj, times=3, gap=0.25)

        # ② 已知设备 + 手工指定网段里的地址，直接单播
        known = self.known_ips()
        direct = list(known)
        for ip in self.spec_hosts[:1024]:
            if ip not in direct:
                direct.append(ip)
        if direct:
            sent = self._send_packet(raw, direct)
            extra = len(direct) - len(known)
            self.log(f"{label}额外单播 {len(direct)} 个地址"
                     f"（{len(known)} 台已知设备"
                     + (f" + 指定网段 {extra} 个" if extra else "")
                     + f"），共发 {sent} 个包，绕开被拦的广播", "dim")

        # ③ 谁没回执就补发
        if rounds > 1 and direct:
            def retry():
                for rnd in range(2, rounds + 1):
                    if self._stop.wait(gap):
                        return
                    with self._dev_lock:
                        acks = dict(self._acks.get(task, {}))
                        online = [str(d.get("ip") or k)
                                  for k, d in self.devices.items()]
                    # 回执是按「计算机名/IP」归并的 key 存的，值里第 4 个才是 IP
                    done = {str(v[3]) for v in acks.values() if len(v) > 3}
                    todo = [ip for ip in online if ip not in done]
                    if not todo:
                        self.log(f"{label}全部回执齐全（{len(done)} 台）", "ok")
                        return
                    self.log(f"{label}第 {rnd} 轮补发：{len(todo)} 台还没回执，"
                             f"单独单播重发", "warn")
                    self._send_packet(raw, todo[:1024])
                # 补发完还不回执的，点名报出来 —— 它们就是"壁纸没变"的那几台
                with self._dev_lock:
                    acks = dict(self._acks.get(task, {}))
                    online = [(str(d.get("ip") or k), str(d.get("host") or ""))
                              for k, d in self.devices.items()]
                done = {str(v[3]) for v in acks.values() if len(v) > 3}
                silent = [ip for ip, _h in online if ip not in done]
                if silent:
                    self.log(f"{label}补发 {rounds - 1} 轮后仍有 {len(silent)} 台没回执："
                             + "、".join(silent[:10])
                             + ("…" if len(silent) > 10 else ""), "err")
                    # 再探一次这些机器到底还在不在 —— 给出的结论要能指导下一步
                    alive = self.probe(silent[:200], wait=4.0)
                    dead = [ip for ip in silent if ip not in alive]
                    # 按 /24 汇总：一眼看出"是哪几个网段整片没通"
                    seg: dict[str, int] = {}
                    for ip in silent:
                        parts = ip.split(".")
                        key = ".".join(parts[:3]) + ".0/24" if len(parts) == 4 else ip
                        seg[key] = seg.get(key, 0) + 1
                    if seg:
                        self.log(f"{label}没回执的机器按网段汇总："
                                 + "；".join(f"{k} → {v} 台"
                                             for k, v in sorted(seg.items(),
                                                                key=lambda kv: -kv[1])),
                                 "warn")
                    if alive:
                        self.log(f"{label}其中 {len(alive)} 台**能回应扫描**：网络是通的，"
                                 f"它们多半没收到公告、或者下载壁纸失败（检查被控端日志 "
                                 f"agent.log；也确认控制端入站 TCP {self.tcp_port} 已放行）",
                                 "warn")
                    if dead:
                        # 判断"包被谁丢了"：如果这些地址和本机**在同一个网段**，
                        # 那就不存在路由问题 —— 包是被对方主机自己丢掉的
                        # （Windows 防火墙没放行入站 UDP，或被 EDR 拦），
                        # 所以第一步永远是去客户机上查那条防火墙规则。
                        same_net = 0
                        for ip in dead[:64]:
                            try:
                                addr = ipaddress.IPv4Address(ip)
                            except ValueError:
                                continue
                            if any(addr in a.network for a in self.adapters
                                   if a.usable):
                                same_net += 1
                        if same_net:
                            self.log(f"{label}其中 {same_net} 台和被控端**在同一网段**"
                                     f"（不存在路由问题）→ 包是被它们自己丢掉的："
                                     f"多半是 Windows 防火墙没放行入站 UDP {self.udp_port}"
                                     f"（旧版按 exe 路径建的规则会失效），或被安全软件拦了。"
                                     f"在那台机器上跑一次： netsh advfirewall firewall "
                                     f"add rule name=\"Win壁纸推送-UDP广播\" dir=in action=allow "
                                     f"protocol=UDP localport={self.udp_port}", "err")
                        self.log(f"{label}另有 {len(dead)} 台连扫描也不回应："
                                 f"我这边的广播/单播到不了它们。常见原因：① 客户机防火墙"
                                 f"没放行入站 UDP {self.udp_port}（它们的主动报到是**出站**，"
                                 f"所以照样能出现在列表里）；② 无线 AP 客户端隔离 / VLAN 隔离；"
                                 f"③ 那台机器有多张网卡、回包走了别的路由。"
                                 f"先按①查（最便宜）。", "err")
                else:
                    self.log(f"{label}补发结束，所有在线设备都已回执", "ok")
            threading.Thread(target=retry, name="announce-retry", daemon=True).start()

    def _broadcast(self, obj: dict, times: int = 3, gap: float = 0.25,
                   targets: list[str] | None = None) -> None:
        """把同一条消息重复广播几次，抵消 UDP 偶发丢包。

        每个目标地址都从「属于它那个网段」的网卡发出去，有线 / 无线 / 虚拟
        网卡逐个覆盖 —— 只发 255.255.255.255 的话，Windows 只会从默认路由
        那张网卡发，多网卡机器上的客户机就永远收不到。

        targets 传了就只用这份地址（例如「只发本机测试」），否则用算好的全网段。
        """
        if self._sock is None:
            return
        if targets is None:
            if not self.targets:
                self.refresh_targets()
            targets = self.targets
        raw = P.dumps(obj)
        with self._send_lock:
            for i in range(max(1, times)):
                for t in targets or ["255.255.255.255"]:
                    for s in self._egress_for(t):
                        try:
                            s.sendto(raw, (t, self.udp_port))
                        except OSError as e:
                            self.log(f"广播到 {t} 失败：{e}", "err")
                if i + 1 < times:
                    time.sleep(gap)

    def _unicast_ping(self, raw: bytes, hosts: list[str]) -> int:
        """逐台单播探测。

        很多交换机 / 无线 AP 会拦掉广播（客户端隔离），但普通的单播 UDP 是
        通的 —— 被控端对单播 ping 一样会应答。网段大时这份名单来自
        netutil.sweep_hosts()（小网段全扫，大网段扫本机 /24 + ARP 名单）。
        """
        done = 0
        with self._send_lock:
            for i, host in enumerate(hosts):
                for s in self._egress_for(host):
                    try:
                        s.sendto(raw, (host, self.udp_port))
                    except OSError:
                        pass
                done += 1
                if i % 48 == 47:
                    time.sleep(0.01)   # 别一口气把网卡队列灌满
        return done

    def scan(self, deep: bool | None = None) -> str:
        """扫描在线设备：先广播，再（默认）逐台单播兜底。"""
        nonce = uuid.uuid4().hex[:8]
        msg = P.make(P.MSG_PING, nonce=nonce, reply_port=self.reply_port,
                     ts=time.time())
        self._broadcast(msg, times=3, gap=0.2)

        want_deep = self.sweep if deep is None else bool(deep)
        self.last_sweep_count = 0
        if want_deep:
            # 除了本机网段，把「额外网段」和已知设备也一并探测
            hosts, notes = N.sweep_hosts(self.adapters or None,
                                         extra=self.spec_hosts)
            known = self.known_ips()
            for ip in known:
                if ip not in hosts:
                    hosts.append(ip)
            if known:
                notes.append(f"已知设备补充 {len(known)} 个探测目标")
            if hosts:
                for note in notes:
                    self.log("单播探测范围：" + note, "dim")
                self.last_sweep_count = self._unicast_ping(P.dumps(msg), hosts)

        self._last_scan = time.time()
        if self.last_sweep_count:
            self.log(f"已广播 + 逐台单播探测 {self.last_sweep_count} 个地址，"
                     f"等待被控端回应…", "warn")
        else:
            self.log("已广播扫描，等待被控端回应…", "warn")
        return nonce

    def push(self, path: str, style: str | None = None) -> str:
        """读取图片并广播推送。返回 task_id。"""
        if not os.path.isfile(path):
            raise FileNotFoundError(f"找不到图片：{path}")
        with open(path, "rb") as f:
            data = f.read()
        if not data:
            raise ValueError("图片内容为空")

        if style:
            self.style = P.normalize_style(style) or P.DEFAULT_STYLE
        if self.style not in P.STYLES:
            self.style = P.DEFAULT_STYLE

        task = uuid.uuid4().hex[:12]
        name = os.path.basename(path)
        sha = hashlib.sha256(data).hexdigest()

        # 先挂上待分发的文件，再广播公告；否则被控端可能比我们准备得还快
        self._payload = {"task": task, "name": name, "size": len(data),
                         "sha256": sha, "data": data}
        self._tasks[task] = {"name": name, "size": len(data), "ts": time.time(),
                             "style": self.style}
        with self._dev_lock:
            self._acks[task] = {}
            # 清理旧任务的确认记录
            for old in [t for t in self._acks if t != task][:-8]:
                self._acks.pop(old, None)

        self.announce(P.make(
            P.MSG_ANNOUNCE, task=task, name=name, size=len(data), sha256=sha,
            tcp_port=self.tcp_port, reply_port=self.reply_port,
            style=self.style, ts=time.time(),
        ), task, label="壁纸推送")

        mode = f"（广播 {len(self.targets)} 个目标 + 已知设备单播）"
        self.log(
            f"已广播壁纸「{name}」 {len(data) // 1024} KB · 契合度「{self.style}」"
            f" · 任务号 {task} {mode}",
            "ok",
        )
        return task

    # ---------------------------------------------------------- 查询

    def online_devices(self) -> list[tuple[str, dict]]:
        """返回 [(设备标识, 设备信息)]，只包含最近有回应的设备。"""
        now = time.time()
        with self._dev_lock:
            return [
                (key, dict(d)) for key, d in self.devices.items()
                if now - d.get("last", 0) <= ONLINE_WINDOW
            ]

    def ack_summary(self, task: str) -> tuple[int, int, int]:
        """返回 (成功, 失败, 待确认)。"""
        online = len(self.online_devices())
        with self._dev_lock:
            acks = self._acks.get(task, {})
        ok = sum(1 for v in acks.values() if v[0])
        fail = sum(1 for v in acks.values() if not v[0])
        pending = max(0, online - ok - fail)
        return ok, fail, pending

    def task_info(self, task: str) -> dict:
        return self._tasks.get(task, {})

    # ---------------------------------------------------------- 通知推送

    def push_toast(self, spec: dict, assets: dict[str, bytes] | None = None,
                   local_only: bool = False) -> str:
        """广播一条通知（Toast）。

        spec 是界面/命令行给的原始规格，这里再过一遍 `toastspec.normalize`
        校验；assets 是 {图片名: 字节}，只有规格里真正引用到的才会随包下发
        （被控端用 TCP 来拉，和壁纸一样的套路）。返回任务号。
        """
        ok, clean, err = TS.normalize(spec)
        if not ok:
            raise ValueError(err)

        task = uuid.uuid4().hex[:12]
        assets = assets or {}
        wanted = TS.asset_names(clean)
        keep = {name: assets[name] for name in wanted if name in assets}
        missing = [name for name in wanted if name not in keep]

        manifest: list[dict] = []
        for name, data in keep.items():
            manifest.append({"name": name, "size": len(data),
                             "sha256": hashlib.sha256(data).hexdigest()})

        with self._dev_lock:
            self._acks[task] = {}
            self._toast_assets[task] = keep
            self._tasks[task] = {"kind": "toast", "spec": clean,
                                 "assets": [a["name"] for a in manifest],
                                 "missing": missing, "ts": time.time()}
            # 只留最近 5 次通知的图片，别把内存占住
            old = sorted((t for t in self._toast_assets if t != task),
                         key=lambda t: self._tasks.get(t, {}).get("ts", 0))
            for t in old[:-5]:
                self._toast_assets.pop(t, None)

        targets = ["127.0.0.1"] if local_only else None
        self._broadcast(P.make(
            P.MSG_TOAST, task=task, spec=clean, assets=manifest,
            tcp_port=self.tcp_port, reply_port=self.reply_port, ts=time.time(),
        ), targets=targets)
        if not local_only:
            # 广播之外再直接单播给已知设备：设备多的时候广播总有漏的
            direct = self.known_ips()
            if direct:
                self._send_packet(P.dumps(P.make(
                    P.MSG_TOAST, task=task, spec=clean, assets=manifest,
                    tcp_port=self.tcp_port, reply_port=self.reply_port,
                    ts=time.time())), direct)
                self.log(f"通知已额外单播给 {len(direct)} 台已知设备", "dim")

        self.last_toast_task = task
        mode = "（只发本机测试）" if local_only else ""
        self.log(f"已广播通知：{TS.summarize(clean)} · 任务号 {task}{mode}", "ok")
        if missing:
            self.log("这些图片没找到，通知里不会带：" + "、".join(missing), "warn")
        return task

    def toast_info(self, task: str) -> dict:
        return self._tasks.get(task, {})

    # ---------------------------------------------------------- 远程改通知应用名

    def push_app_name(self, name: str, local_only: bool = False) -> str:
        """广播一条「把通知上显示的应用名改成 X」的指令，返回任务号。

        注意这是**持久化**改动：被控端会把名字写进自己的配置并重建 AppId 注册，
        重启后仍然是新名字。空字符串 = 让它们恢复默认名字。

        被控端可以拒绝（配置 `allow_remote_app_name=false`），拒绝原因会随回执回来。
        """
        ok, clean = P.normalize_app_name(name)
        if not ok:
            raise ValueError(clean)

        task = uuid.uuid4().hex[:12]
        with self._dev_lock:
            self._acks[task] = {}
            self._tasks[task] = {"kind": "setname", "name": clean,
                                 "ts": time.time()}

        targets = ["127.0.0.1"] if local_only else None
        self._broadcast(P.make(
            P.MSG_SETNAME, task=task, app_name=clean,
            reply_port=self.reply_port, ts=time.time(),
        ), targets=targets)
        if not local_only:
            direct = self.known_ips()
            if direct:
                self._send_packet(P.dumps(P.make(
                    P.MSG_SETNAME, task=task, app_name=clean,
                    reply_port=self.reply_port, ts=time.time())), direct)
                self.log(f"改名指令已额外单播给 {len(direct)} 台已知设备", "dim")

        shown = clean or "（默认：Win 壁纸推送）"
        mode = "（只发本机测试）" if local_only else ""
        self.log(f"已广播「改通知应用名」：{shown} · 任务号 {task}{mode}", "ok")
        return task

    def name_info(self, task: str) -> dict:
        return self._tasks.get(task, {})


# ================================================================ 界面

def make_thumbnail(path: str, w: int, h: int):
    """生成预览缩略图，返回 (PhotoImage, 说明文字)。没有 Pillow 也能降级工作。"""
    import tkinter as tk

    info = ""
    try:
        from PIL import Image, ImageTk  # type: ignore

        with Image.open(path) as im:
            info = f"{im.width} × {im.height}  {im.format}"
            im = im.convert("RGB")
            im.thumbnail((w, h))
            return ImageTk.PhotoImage(im), info
    except ImportError:
        pass
    except Exception as e:
        info = f"（预览失败：{e}）"

    try:  # Tk 8.6 自带 PNG/GIF 支持
        img = tk.PhotoImage(file=path)
        sw = max(1, img.width() // w, img.height() // h)
        if sw > 1:
            img = img.subsample(sw, sw)
        return img, info or f"{img.width()} × {img.height()}"
    except Exception:
        return None, info or "（无法预览该格式）"


def run_gui(core: ControllerCore, cfg: dict, cfg_path: str) -> int:
    """控制端主界面。

    排版（2026-09 重做）：
        顶部：标题 + 在线台数 + 状态徽标
        中间：左边页签「壁纸 / 通知 / 设置」   ·   右边设备列表（常驻，可拖动分栏）
        底部：运行日志（默认收起成一行，点开才占地方）

    为什么这么排：
      * 「壁纸」和「通知」以前一个是主界面、一个是弹窗 —— 编辑通知时主界面和
        设备列表全被浮窗挡住。现在都是页面，同一个窗口里切，不弹窗。
      * 设备列表（推给谁、结果如何）是这个软件最核心的信息，以前只占右侧 2/5、
        才 9 行高；现在常驻右侧并且是主体。
      * 额外广播地址 / 深度扫描 / 自动重扫 / 重新检测网卡这些偶尔才用的东西，
        全挪进「设置」页；解释性文字改成悬浮提示，界面只留字段名。
      * 日志是排查时才看的，默认收起，一眼能看到"有没有新错误"就行。
    """
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    import ui

    root = tk.Tk()
    root.title(f"{APP_NAME}  v{APP_VER}")
    ui.apply_theme(root)
    root.minsize(1000, 640)

    log_q: queue.Queue[tuple[str, str]] = queue.Queue()
    core.log = lambda msg, level="info": log_q.put((msg, level))

    # status 由工作线程写入、主线程 poll() 消费，避免跨线程直接操作控件
    state = {"path": cfg.get("last_file") or "", "task": "", "photo": None,
             "status": None}

    # ---------------- 先定义逻辑（控件回调要用它们，Python 只有在调用时才解析名字，
    #                  但直接当参数传的函数对象必须先存在）
    def ui_nic_text() -> str:
        return "本机网卡：" + (N.adapter_summary(core.adapters) or "未检测到可用网卡")

    def toggle_sweep():
        core.sweep = bool(sweep_var.get())
        cfg["sweep"] = core.sweep
        N.save_json(cfg_path, cfg)

    def toggle_auto():
        core.auto_scan = 30.0 if auto_var.get() else 0.0
        cfg["auto_scan"] = core.auto_scan
        N.save_json(cfg_path, cfg)
        log_q.put((f"自动重扫：{'每 30 秒一次' if core.auto_scan else '已关闭'}", "dim"))

    def apply_targets():
        core.extra_targets = [x for x in tgt_var.get().replace(",", " ").split() if x]
        cfg["targets"] = core.extra_targets
        N.save_json(cfg_path, cfg)
        core.refresh_targets()
    def recheck_nics():
        apply_targets()
        refresh_hint()
        log_q.put((f"已重新检测网卡：{N.adapter_summary(core.adapters) or '未检测到'}", "ok"))

    def choose():
        p = filedialog.askopenfilename(
            title="选择壁纸图片",
            initialdir=cfg.get("last_dir") or os.path.expanduser("~"),
            filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp *.gif"), ("所有文件", "*.*")],
        )
        if not p:
            return
        state["path"] = p
        file_var.set(p)
        cfg["last_file"] = p
        cfg["last_dir"] = os.path.dirname(p)
        N.save_json(cfg_path, cfg)
        load_preview(p)

    def load_preview(path: str):
        if not path or not os.path.isfile(path):
            preview_box.configure(image="", text="尚未选择图片")
            state["photo"] = None
            meta.configure(text="")
            return
        photo, info = make_thumbnail(path, 430, 260)
        state["photo"] = photo
        if photo:
            preview_box.configure(image=photo, text="", width=photo.width(),
                                  height=photo.height())
        else:
            preview_box.configure(image="", text="（无法预览该格式）")
        size_kb = os.path.getsize(path) // 1024
        meta.configure(text=f"{info}   ·   {size_kb} KB   ·   {os.path.basename(path)}")

    def do_push():
        p = file_var.get().strip()
        if not p or not os.path.isfile(p):
            messagebox.showwarning("请先选择图片", "请选择一张存在的图片文件。")
            return
        apply_targets()
        cfg["style"] = style_var.get()
        N.save_json(cfg_path, cfg)
        status.configure(text="  广播中…  ", foreground=ui.WARN)

        def work():
            try:
                task = core.push(p, style_var.get())
                state["task"] = task
                log_q.put((f"等待被控端确认（任务号 {task}）…", "dim"))
                # 设备多的时候回执会慢慢齐：等 12 秒（期间补发线程也在跑）
                deadline = time.time() + 12
                while time.time() < deadline:
                    time.sleep(0.3)
                ok_n, fail_n, pending = core.ack_summary(task)
                log_q.put((f"推送结果：成功 {ok_n} · 失败 {fail_n} · 待确认 {pending}"
                           + (f"（待确认的会在日志里点名）" if pending else ""),
                           "ok" if ok_n and not pending else "warn"))
                state["status"] = ("  已推送  ", ui.OK)
            except Exception as e:
                log_q.put((f"推送失败：{e}", "err"))
                state["status"] = ("  推送失败  ", ui.ERR)

        threading.Thread(target=work, daemon=True).start()

    def do_scan():
        apply_targets()
        cfg["sweep"] = bool(sweep_var.get())
        N.save_json(cfg_path, cfg)
        log_q.put(("正在扫描局域网设备…", "dim"))
        threading.Thread(
            target=lambda: core.scan(deep=sweep_var.get()), daemon=True
        ).start()

    def do_set_name():
        """把「通知应用名」广播给所有被控端（持久化改动，会等回执）。"""
        from tkinter import messagebox as mb

        raw = fleet_name_var.get().strip()
        if raw and raw.lower() not in ("default", "reset", "-"):
            if not mb.askyesno(
                    "确认批量改名",
                    f"把**所有在线被控端**通知上显示的名字改成：\n\n    {raw}\n\n"
                    f"这会写进它们各自的配置（重启后仍然生效）。继续？"):
                return
        else:
            raw = ""
        apply_targets()
        status.configure(text="  改名中…  ", foreground=ui.WARN)

        def work():
            try:
                task = core.push_app_name(raw)
                state["task"] = task
                state["kind"] = "setname"
                deadline = time.time() + 12
                while time.time() < deadline:
                    time.sleep(0.3)
                ok_n, fail_n, pending = core.ack_summary(task)
                shown = raw or "（默认：Win 壁纸推送）"
                log_q.put((f"改通知应用名「{shown}」：成功 {ok_n} · 失败 {fail_n}"
                           f" · 待确认 {pending}", "ok" if ok_n else "warn"))
                state["status"] = ("  已下发  ", ui.OK)
            except Exception as e:
                log_q.put((f"改名失败：{e}", "err"))
                state["status"] = ("  改名失败  ", ui.ERR)

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 顶部一行：标题 + 在线台数 + 状态
    head = ttk.Frame(root, padding=(20, 14, 20, 6))
    head.pack(fill="x")
    ttk.Label(head, text="📡  壁纸推送控制端", style="Title.TLabel").pack(side="left")
    status = ttk.Label(head, text="  就绪  ", style="Card.TLabel", foreground=ui.OK)
    status.pack(side="right", ipady=4, ipadx=8)
    online_badge = ttk.Label(head, text="", style="Card.TLabel", foreground=ui.FG_DIM)
    online_badge.pack(side="right", padx=(0, 10))

    # ---------------- 中间：左页签 + 右设备列表（可拖动分栏）
    body = ttk.Frame(root, padding=(20, 0, 20, 0))
    body.pack(fill="both", expand=True)
    paned = ttk.Panedwindow(body, orient="horizontal")
    paned.pack(fill="both", expand=True)

    left_wrap = ttk.Frame(paned)
    right = ttk.Frame(paned, style="Card.TFrame", padding=14)
    paned.add(left_wrap, weight=3)
    paned.add(right, weight=2)

    book = ttk.Notebook(left_wrap)
    book.pack(fill="both", expand=True)
    page_wall = ttk.Frame(book, padding=12)
    page_toast = ttk.Frame(book, padding=12)
    page_ssh = ttk.Frame(book, padding=12)
    page_set = ttk.Frame(book, padding=12)
    book.add(page_wall, text="  壁纸  ")
    book.add(page_toast, text="  通知  ")
    book.add(page_ssh, text="  远程命令  ")
    book.add(page_set, text="  设置  ")

    # ================= 页一：壁纸
    wall = ttk.Frame(page_wall, style="Card.TFrame", padding=14)
    wall.pack(fill="both", expand=True)

    pick_row = ttk.Frame(wall, style="Card.TFrame")
    pick_row.pack(fill="x")
    ttk.Label(pick_row, text="图片", style="Card.TLabel",
              foreground=ui.FG_DIM, width=6).pack(side="left")
    file_var = tk.StringVar(value=state["path"])
    entry = ttk.Entry(pick_row, textvariable=file_var)
    entry.pack(side="left", fill="x", expand=True)
    ttk.Button(pick_row, text="浏览…", command=lambda: choose()).pack(side="left", padx=(8, 0))

    opt = ttk.Frame(wall, style="Card.TFrame")
    opt.pack(fill="x", pady=(8, 0))
    ttk.Label(opt, text="契合度", style="Card.TLabel",
              foreground=ui.FG_DIM, width=6).pack(side="left")
    style_var = tk.StringVar(value=core.style)
    style_box = ttk.Combobox(opt, textvariable=style_var, values=P.STYLE_NAMES,
                             state="readonly", width=8)
    style_box.pack(side="left")
    ui.tooltip(style_box, "填充 = 铺满屏幕且不变形（推荐）\n"
                          "适应 = 完整显示，可能留黑边\n"
                          "拉伸 = 铺满但可能变形")

    preview_box = tk.Label(wall, bg="#15171c", fg=ui.FG_DIM, text="尚未选择图片",
                           width=44, height=9, font=ui.pick_font(9))
    preview_box.pack(fill="both", expand=True, pady=(10, 6))
    meta = ttk.Label(wall, text="", style="Card.TLabel", foreground=ui.FG_DIM)
    meta.pack(anchor="w")

    # ================= 页二：通知（内嵌编辑器，不再是弹窗）
    try:
        import toastui

        toastui.build_editor(page_toast, core, cfg, cfg_path).pack(fill="both", expand=True)
    except Exception as e:                     # 打包缺模块时也要让人看懂
        ttk.Label(page_toast, style="TLabel", foreground=ui.ERR,
                  wraplength=520, justify="left",
                  text=f"通知编辑器没加载起来：{e}\n"
                       f"（源码运行请确认 toastui.py 在，打包版请重新运行 build.bat）"
                  ).pack(anchor="w")

    # ================= 页三：远程命令（SSH）—— 在每台设备上执行命令
    # 设备列表在右边那列（控制端自己的设备表），所以这一页不用再手填网段：
    # 默认目标就是"已经发现的在线设备"，选不中再手填。
    # 为什么要留个 sel 中转：右侧设备列表 tree 是后面才建的，
    # 而这一页现在就要一个"取当前选中"的回调，所以用个可变的槽位，建好再填进去。
    sel = {"fn": None}
    try:
        import sshui

        sshui.build_panel(
            page_ssh, core, cfg, cfg_path,
            log=lambda msg, level="info": log_q.put((msg, level)),
            get_selection=lambda: (sel["fn"]() if sel["fn"] else []),
        ).pack(fill="both", expand=True)
    except Exception as e:                     # 打包缺模块时也要让人看懂
        ttk.Label(page_ssh, style="TLabel", foreground=ui.ERR,
                  wraplength=520, justify="left",
                  text=f"远程命令页没加载起来：{e}\n"
                       f"（源码运行请确认 sshui.py / sshcmd.py 在，"
                       f"打包版请重新运行 build.bat）"
                  ).pack(anchor="w")

    # ================= 页四：设置（那些偶尔才改的东西）
    net_card = ttk.Frame(page_set, style="Card.TFrame", padding=14)
    net_card.pack(fill="x")
    ttk.Label(net_card, text="网络", style="Card.TLabel",
              font=ui.pick_font(10, True)).pack(anchor="w")
    nic_label = ttk.Label(net_card, text=ui_nic_text(), style="Card.TLabel",
                          foreground=ui.FG_DIM, wraplength=560, justify="left")
    nic_label.pack(anchor="w", pady=(4, 8))
    tgt = ttk.Frame(net_card, style="Card.TFrame")
    tgt.pack(fill="x")
    ttk.Label(tgt, text="额外网段/IP", style="Card.TLabel",
              foreground=ui.FG_DIM).pack(side="left", padx=(0, 8))
    tgt_var = tk.StringVar(value=" ".join(core.extra_targets))
    tgt_entry = ttk.Entry(tgt, textvariable=tgt_var)
    tgt_entry.pack(side="left", fill="x", expand=True)
    ttk.Button(tgt, text="应用", width=6,
               command=lambda: (apply_targets(), refresh_hint())).pack(side="left", padx=(6, 0))
    ui.tooltip(tgt_entry,
               "想指定网段就填这里，四种写法都认（空格或逗号分隔多个）：\n"
               "  192.168.1.0/24      一个网段（会定向广播 + 逐台探测 254 个地址）\n"
               "  192.168.1.255       广播地址\n"
               "  192.168.1.10        单台机器（单播给它）\n"
               "  192.168.1.1-10.127.112.60   地址范围\n"
               "同网段本来就会自动广播，这里用于「有线那个网段扫不全」这类情况。")
    tgt_hint = ttk.Label(net_card, text="", style="Card.TLabel",
                         foreground=ui.FG_DIM, wraplength=560, justify="left")
    tgt_hint.pack(anchor="w", pady=(6, 0))

    def refresh_hint():
        """把「额外网段」解析成人话显示出来（广播到哪、探测多少个地址）。"""
        send, hosts, notes = N.parse_target_spec(" ".join(core.extra_targets))
        if not core.extra_targets:
            tgt_hint.configure(text="未指定额外网段：只用本机网卡自己的网段广播。",
                               foreground=ui.FG_DIM)
            return
        parts = [f"解析结果：发送到 {'、'.join(send) if send else '（无）'}",
                 f"，逐台探测 {len(hosts)} 个地址"]
        if notes:
            bad = [n for n in notes if "忽略" in n]
            parts.append("；" + "；".join(notes))
            tgt_hint.configure(text="".join(parts), foreground=ui.ERR if bad else ui.FG_DIM)
        else:
            tgt_hint.configure(text="".join(parts), foreground=ui.FG_DIM)

    refresh_hint()      # 打开界面就把当前配置解析出来看看

    scan_card = ttk.Frame(page_set, style="Card.TFrame", padding=14)
    scan_card.pack(fill="x", pady=(10, 0))
    ttk.Label(scan_card, text="扫描", style="Card.TLabel",
              font=ui.pick_font(10, True)).pack(anchor="w")
    scanrow = ttk.Frame(scan_card, style="Card.TFrame")
    scanrow.pack(fill="x", pady=(6, 0))
    sweep_var = tk.BooleanVar(value=core.sweep)
    auto_var = tk.BooleanVar(value=core.auto_scan > 0)
    cb_sweep = ui.checkbutton(scanrow, "深度扫描（逐台单播）", sweep_var, toggle_sweep)
    cb_sweep.pack(side="left")
    ui.tooltip(cb_sweep, "广播之外再逐台单播探一遍。\n"
                         "交换机 / 无线 AP 拦广播时，靠它才能找到设备 ——\n"
                         "代价是会发几百个包，慢一点。")
    cb_auto = ui.checkbutton(scanrow, "自动重扫", auto_var, toggle_auto)
    cb_auto.pack(side="left", padx=(12, 0))
    ui.tooltip(cb_auto, "每 30 秒自动扫一次，设备上线 / 下线一眼能看到")
    ttk.Button(scanrow, text="重新检测网卡", command=lambda: recheck_nics()).pack(side="right")
    scan_hint = ttk.Label(scan_card, text="", style="Card.TLabel", foreground=ui.FG_DIM)
    scan_hint.pack(anchor="w", pady=(8, 0))

    # 批量改被控端「通知上显示的应用名」（持久化：它们会写进自己的配置）
    name_card = ttk.Frame(page_set, style="Card.TFrame", padding=14)
    name_card.pack(fill="x", pady=(10, 0))
    ttk.Label(name_card, text="被控端通知应用名", style="Card.TLabel",
              font=ui.pick_font(10, True)).pack(anchor="w")
    ttk.Label(name_card,
              text="通知顶部显示的那个名字（默认「Win 壁纸推送」）。下发后各被控端会写进"
                   "自己的配置，重启仍然生效；留空 = 让它们恢复默认。",
              style="Card.TLabel", foreground=ui.FG_DIM,
              wraplength=520, justify="left").pack(anchor="w", pady=(4, 6))
    name_row = ttk.Frame(name_card, style="Card.TFrame")
    name_row.pack(fill="x")
    fleet_name_var = tk.StringVar(value="")
    name_entry = ttk.Entry(name_row, textvariable=fleet_name_var)
    name_entry.pack(side="left", fill="x", expand=True)
    ui.tooltip(name_entry, "例如「IT 运维通知」。\n"
                           "注意：改的是通知**顶部**那个名字；\n"
                           "每条消息底部的「署名」是在通知页里填的。")
    name_btn = ttk.Button(name_row, text="下发到被控端…", command=lambda: do_set_name())
    name_btn.pack(side="left", padx=(8, 0))
    ui.tooltip(name_btn, "广播给所有在线被控端（它们可以拒绝：allow_remote_app_name=false）")
    name_hint = ttk.Label(name_card, text="", style="Card.TLabel", foreground=ui.FG_DIM)
    name_hint.pack(anchor="w", pady=(6, 0))

    # ================= 右侧：设备列表
    ttk.Label(right, text="局域网设备", style="Card.TLabel",
              font=ui.pick_font(11, True)).pack(anchor="w")
    dev_top = ttk.Frame(right, style="Card.TFrame")
    dev_top.pack(fill="x", pady=(4, 8))
    dev_hint = ttk.Label(dev_top, text="还没扫描过", style="Card.TLabel",
                         foreground=ui.FG_DIM)
    dev_hint.pack(side="left")
    ttk.Button(dev_top, text="🔍 扫描", command=lambda: do_scan()).pack(side="right")
    ui.tooltip(dev_top.winfo_children()[-1], "搜局域网里的被控端（快捷键 F5）")

    cols = ("ip", "host", "user", "result", "last")
    tree = ttk.Treeview(right, columns=cols, show="headings", height=16)
    for c, t, w in (
        ("ip", "IP 地址", 120), ("host", "计算机名", 130),
        ("user", "用户", 90), ("result", "本次结果", 90), ("last", "最后在线", 90),
    ):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="w", stretch=True)
    tree.pack(fill="both", expand=True)
    tree.tag_configure("ok", foreground=ui.OK)
    tree.tag_configure("fail", foreground=ui.ERR)
    tree.tag_configure("pending", foreground=ui.WARN)
    tree.tag_configure("offline", foreground=ui.FG_DIM)
    # 「远程命令」页的「取右侧选中」按钮靠它拿到这里选中的机器
    sel["fn"] = lambda: list(tree.selection())

    # ================= 底部：日志（默认收起）
    log_box = ttk.Frame(root, padding=(20, 8, 20, 14))
    log_box.pack(fill="x", side="bottom")
    log_head = ttk.Frame(log_box)
    log_head.pack(fill="x")
    log_toggle = ttk.Button(log_head, text="▸  运行日志", command=lambda: toggle_log())
    log_toggle.pack(side="left")
    ttk.Button(log_head, text="清空", command=lambda: log_pane.clear()).pack(side="right")
    log_pane = ui.LogPane(log_box, height=8)
    log_state = {"open": False}

    def toggle_log():
        if log_state["open"]:
            log_pane.pack_forget()
            log_toggle.configure(text="▸  运行日志")
            log_state["open"] = False
        else:
            log_pane.pack(fill="both", expand=True, pady=(6, 0))
            log_toggle.configure(text="▾  运行日志")
            log_state["open"] = True

    # 推送按钮和结果行放在壁纸页底部（页面顺序：选图 → 参数 → 预览 → 动作）
    push_row = ttk.Frame(wall, style="Card.TFrame")
    push_row.pack(fill="x", pady=(10, 0))
    push_btn = ttk.Button(push_row, text="🚀  广播推送", style="Accent.TButton",
                          command=lambda: do_push())
    push_btn.pack(side="left", ipadx=10, ipady=3)
    ui.tooltip(push_btn, "把选中的图片推给所有在线被控端（快捷键 Ctrl+Enter）")
    progress = ttk.Label(push_row, text="尚未推送", style="Card.TLabel",
                         foreground=ui.FG_DIM)
    progress.pack(side="left", padx=(12, 0))

    # ---------------- 刷新循环
    def refresh_tree():
        now = time.time()
        with core._dev_lock:
            devices = dict(core.devices)
            acks = dict(core._acks.get(state["task"], {})) if state["task"] else {}

        for key, d in devices.items():
            last = d.get("last", 0)
            online = now - last <= ONLINE_WINDOW
            ip = d.get("ip") or key
            if state["task"] and key in acks:
                ok = acks[key][0]
                result, tag = ("已应用", "ok") if ok else ("失败", "fail")
            elif online and state["task"]:
                result, tag = ("待确认", "pending")
            else:
                result, tag = ("—", "pending")
            if not online:
                result, tag = "离线", "offline"

            vals = (
                ip,
                d.get("host", ""),
                d.get("user", ""),
                result,
                time.strftime("%H:%M:%S", time.localtime(last)) if last else "—",
            )
            if tree.exists(key):
                tree.item(key, values=vals, tags=(tag,))
            else:
                tree.insert("", "end", iid=key, values=vals, tags=(tag,))

        online_n = sum(1 for d in devices.values() if now - d.get("last", 0) <= ONLINE_WINDOW)
        # 同名机器要提醒一句：克隆镜像/同批装机的客户机经常同计算机名，
        # 以前按名字归并会把 50 台合成 1 行（用户实际踩过），现在按 IP 分开显示。
        hosts: dict[str, int] = {}
        for d in devices.values():
            h = str(d.get("host") or "").strip().lower()
            if h:
                hosts[h] = hosts.get(h, 0) + 1
        dup = sum(1 for n in hosts.values() if n > 1)
        dev_hint.configure(
            text=f"在线 {online_n} 台 / 共发现 {len(devices)} 台"
                 + (f"（{dup} 个计算机名重名，已按 IP 分开显示）" if dup else ""))
        online_badge.configure(
            text=f"  在线 {online_n} 台  ",
            foreground=ui.OK if online_n else ui.FG_DIM)

        nic_label.configure(text=ui_nic_text())
        sweep_txt = (f"广播 + 逐台单播（{core.last_sweep_count} 个地址）"
                     if core.last_sweep_count else
                     ("广播 + 逐台单播" if core.sweep else "仅广播（深度扫描已关）"))
        scan_hint.configure(
            text=f"扫描方式：{sweep_txt}"
                 + (f" · 自动重扫 {int(core.auto_scan)}s" if core.auto_scan else ""),
            foreground=ui.ERR if not core.sweep else ui.FG_DIM)

        if state["task"]:
            ok, fail, pending = core.ack_summary(state["task"])
            info = core.task_info(state["task"])
            progress.configure(
                text=f"任务 {state['task']} · {info.get('name','')} · "
                     f"成功 {ok} · 失败 {fail} · 待确认 {pending}"
                     + (f" · 在线 {online_n}" if online_n else "")
            )
            # 没回执的机器点名列出来：它们就是"壁纸没变"的那几台
            if info.get("kind") in ("toast", "setname") or info.get("name"):
                acked_ips = {str(v[3]) for v in acks.values() if len(v) > 3}
                silent = [str(d.get("ip") or k) for k, d in devices.items()
                          if (d.get("ip") or k) not in acked_ips
                          and now - d.get("last", 0) <= ONLINE_WINDOW]
                if silent:
                    progress.configure(text=progress.cget("text")
                                       + f" · 没回执 {len(silent)} 台："
                                       + "、".join(silent[:6])
                                       + ("…" if len(silent) > 6 else ""))
            if info.get("kind") == "setname":
                shown = info.get("name") or "（默认：Win 壁纸推送）"
                fails = [v[1] for v in acks.values() if not v[0] and v[1]]
                name_hint.configure(
                    text=f"最近一次下发「{shown}」：成功 {ok} · 失败 {fail}"
                         f" · 待确认 {pending}"
                         + ("；有机器拒绝：" + "；".join(fails[:2]) if fails else ""),
                    foreground=ui.ERR if fail and not ok else ui.FG_DIM)

    # 关闭时先撤销排队中的定时回调，再销毁窗口。
    # 否则 root.after 排的回调会在解释器销毁后触发，抛
    # `invalid command name "...poll"` 这种 Tcl 报错。
    closing = {"done": False}
    after_id = {"id": None}

    def save_geometry():
        try:
            cfg["win_w"] = root.winfo_width()
            cfg["win_h"] = root.winfo_height()
            N.save_json(cfg_path, cfg)
        except Exception:
            pass

    def on_close():
        if closing["done"]:
            return
        closing["done"] = True
        if after_id["id"]:
            try:
                root.after_cancel(after_id["id"])
            except Exception:
                pass
        save_geometry()
        core.stop()
        try:
            root.destroy()
        except Exception:
            pass

    def poll():
        if closing["done"]:
            return
        drained = 0
        while drained < 300:
            try:
                msg, level = log_q.get_nowait()
            except queue.Empty:
                break
            log_pane.write(msg, level)
            drained += 1
        if state["status"]:
            text, color = state["status"]
            status.configure(text=text, foreground=color)
            state["status"] = None
        refresh_tree()
        after_id["id"] = root.after(300, poll)

    # ---------------- 快捷键
    root.bind("<F5>", lambda _e: do_scan())
    root.bind("<Control-Return>", lambda _e: do_push())
    root.bind("<Control-l>", lambda _e: log_pane.clear())

    # ---------------- 初始化
    if state["path"] and os.path.isfile(state["path"]):
        load_preview(state["path"])
        log_pane.write(f"已载入上次使用的图片：{state['path']}", "dim")
    # 上次用过就沿用上次的窗口大小；第一次打开按内容撑开（别把任何一块挤掉）
    saved_w, saved_h = int(cfg.get("win_w") or 0), int(cfg.get("win_h") or 0)
    if saved_w >= 1000 and saved_h >= 640:
        ui.center(root, saved_w, saved_h)
        root.update_idletasks()
        # 记住的尺寸可能比内容还小（比如界面后来加了新控件 —— 实测就踩过：
        # 加了个按钮之后内容宽了 9px，窗口仍按记住的尺寸开，右边就被切掉了）
        need_w, need_h = root.winfo_reqwidth(), root.winfo_reqheight()
        if need_w + 10 > saved_w or need_h + 10 > saved_h:
            ui.fit(root, max(saved_w, need_w + 10), max(saved_h, need_h + 10))
    else:
        ui.fit(root, 1240, 800)
    # 先启动：start() 里会算好广播目标地址，再打印才不是空的
    core.start()

    log_pane.write("提示：控制端需放行 TCP 38572，被控端需放行 UDP 38571"
                   "（见 add_firewall_rules.bat，管理员运行）", "warn")
    if N.json_config_warning():
        log_pane.write(N.json_config_warning(), "err")
    log_pane.write("准备就绪：选图片 → 「广播推送」；发通知走「通知」页；"
                   "在设备上跑命令走「远程命令」页", "ok")

    # 首次扫描放到后台线程里：深度扫描会发几百个包，不能让界面卡住
    threading.Thread(target=lambda: core.scan(), daemon=True).start()
    root.protocol("WM_DELETE_WINDOW", on_close)
    poll()
    root.mainloop()
    core.stop()
    return 0


# ================================================================ 命令行模式

def load_toast_json(text: str) -> dict:
    """把 --push-toast 的参数变成规格：文件路径或直接给的 JSON 文本。"""
    import json

    raw = (text or "").strip()
    if not raw:
        raise ValueError("没有给通知内容")
    if os.path.isfile(raw):
        with open(raw, "r", encoding="utf-8-sig") as f:
            raw = f.read()
    try:
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"JSON 解析失败：{e}") from e
    if not isinstance(data, dict):
        raise ValueError("通知规格必须是一个 JSON 对象")
    return data


def run_cli_toast(core: "ControllerCore", args) -> int:
    """命令行推一条通知，等一会儿收集回执。"""
    def log(msg, level="info"):
        mark = {"ok": "OK  ", "err": "ERR ", "warn": "WARN", "dim": "    "}.get(level, "    ")
        print(f"[{time.strftime('%H:%M:%S')}] {mark} {msg}", flush=True)

    core.log = log
    try:
        spec = load_toast_json(args.push_toast)
    except ValueError as e:
        print(f"通知内容有误：{e}", file=sys.stderr)
        return 1

    ok, clean, err = TS.normalize(spec)
    if not ok:
        print(f"通知内容有误：{err}", file=sys.stderr)
        return 1

    # 图片：JSON 里写 "assets": {"名字": "本地路径"}，这里读进来随包下发
    assets: dict[str, bytes] = {}
    for name, path in (spec.get("assets") or {}).items():
        try:
            with open(path, "rb") as f:
                assets[str(name)] = f.read()
        except OSError as e:
            print(f"图片「{name}」读取失败：{e}", file=sys.stderr)
            return 1

    core.start()
    task = core.push_toast(clean, assets, local_only=bool(args.toast_local))
    wait = args.wait if args.wait is not None else 6.0
    print(f"等待 {wait:g} 秒收集被控端回执…", flush=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.3)
    with core._dev_lock:
        acks = dict(core._acks.get(task, {}))
    online = core.online_devices()
    print("-" * 62)
    print(f"在线设备：{len(online)} 台")
    for _k, d in sorted(online, key=lambda kv: kv[1].get("ip", "")):
        print(f"  {d.get('ip', '?'):<16} {d.get('host', ''):<22} {d.get('user', '')}")
    ok_n = sum(1 for v in acks.values() if v[0])
    bad_n = sum(1 for v in acks.values() if not v[0])
    print(f"通知「{TS.summarize(clean)}」：成功 {ok_n} · 失败 {bad_n}")
    for _key, (good, aerr, _ts, aip) in sorted(acks.items()):
        print(f"  {aip:<16} {'已弹出' if good else '失败 ' + aerr}")
    core.stop()
    if bad_n and not ok_n:
        return 2
    if not acks and not online:
        return 3
    return 0


def run_cli_set_app_name(core: "ControllerCore", args) -> int:
    """命令行改所有被控端的「通知上显示的应用名」，等一会儿收集回执。"""
    def log(msg, level="info"):
        mark = {"ok": "OK  ", "err": "ERR ", "warn": "WARN", "dim": "    "}.get(level, "    ")
        print(f"[{time.strftime('%H:%M:%S')}] {mark} {msg}", flush=True)

    core.log = log
    raw = args.set_app_name
    if str(raw).strip().lower() in ("default", "reset", "-"):
        raw = ""
    ok, clean = P.normalize_app_name(raw)
    if not ok:
        print(f"名字不合法：{clean}", file=sys.stderr)
        return 1

    core.start()
    task = core.push_app_name(clean, local_only=bool(args.toast_local))
    wait = args.wait if args.wait is not None else 6.0
    print(f"等待 {wait:g} 秒收集被控端回执…", flush=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.3)
    with core._dev_lock:
        acks = dict(core._acks.get(task, {}))
    online = core.online_devices()
    shown = clean or "（默认：Win 壁纸推送）"
    print("-" * 62)
    print(f"在线设备：{len(online)} 台")
    ok_n = sum(1 for v in acks.values() if v[0])
    bad_n = sum(1 for v in acks.values() if not v[0])
    print(f"把通知应用名改成「{shown}」：成功 {ok_n} · 失败 {bad_n}")
    for _key, (good, aerr, _ts, aip) in sorted(acks.items()):
        print(f"  {aip:<16} {aerr if aerr else ('已生效' if good else '失败')}")
    core.stop()
    if bad_n and not ok_n:
        return 2
    if not acks and not online:
        return 3
    return 0


def run_cli_ssh(core: "ControllerCore", args) -> int:
    """命令行执行远程命令（SSH），方便脚本化 / 定时任务。

        python controller.py --ssh-cmd "hostname"
        python controller.py --ssh-cmd "uwfmgr filter disable" --ssh-targets 192.168.1.1-56
        python controller.py --ssh-cmd "shutdown /r /t 0" --ssh-mode oneshot

    目标没给 `--ssh-targets` 时，就先扫一遍局域网，发给**当前在线的设备**
    （用 --wait 控制扫描后等多久再开跑）。
    """
    def log(msg, level="info"):
        mark = {"ok": "OK  ", "err": "ERR ", "warn": "WARN", "dim": "    "}.get(level, "    ")
        print(f"[{time.strftime('%H:%M:%S')}] {mark} {msg}", flush=True)

    cmds: list[str] = []
    for raw in (args.ssh_cmd or []):
        cmds += SC.parse_commands(raw)
    if not cmds:
        print("没有可执行的命令（--ssh-cmd 至少要给一条）", file=sys.stderr)
        return 1

    s = SC.normalize_cfg(core.cfg.get("ssh"))
    if args.ssh_user:
        s["user"] = args.ssh_user
    if args.ssh_key:
        s["key"] = args.ssh_key
    if args.ssh_mode:
        s["mode"] = args.ssh_mode
    if args.ssh_timeout:
        s["timeout"] = args.ssh_timeout
    if args.ssh_workers:
        s["workers"] = args.ssh_workers

    ok_ssh, info = SC.ssh_available(s)
    if not ok_ssh:
        print(info, file=sys.stderr)
        return 1

    if args.ssh_targets:
        ips, notes = SC.spec_ips(args.ssh_targets)
        for n in notes:
            log(n, "dim")
    else:
        core.log = log
        core.start()
        core.scan(deep=None if args.sweep is None else args.sweep)
        wait = args.wait if args.wait is not None else 4.0
        log(f"等待 {wait:g} 秒收集设备回应…", "dim")
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(0.3)
        ips = SC.clean_ips([d.get("ip") or k for k, d in core.online_devices()])
        if not ips:
            log("没有发现任何在线设备（可以用 --ssh-targets 直接指定网段）", "err")
            core.stop()
            return 3

    if not ips:
        print("目标为空：--ssh-targets 没解析出任何地址", file=sys.stderr)
        return 1

    print(SC.plan_text(ips, s, cmds), flush=True)
    print("-" * 62, flush=True)
    results = SC.run_hosts(ips, s, cmds, log=log)
    core.stop()

    print("-" * 62)
    for r in results:
        # 命令行里用纯文字状态：中文 cmd（936 代码页）显示不了 ✅/📤 这些符号
        print(f"  {r.ip:<16} {SC.plain_state(r.state):<8} {r.seconds:5.1f}s  "
              f"{r.error or SC.first_lines(r.evidence, 1, 80)}", flush=True)
    st = SC.summarize(results)
    print(f"共 {st['total']} 台：成功 {st[SC.STATE_OK]} · 已下发 {st[SC.STATE_SENT]} · "
          f"部分成功 {st[SC.STATE_PART]} · 失败 {st[SC.STATE_FAIL]}", flush=True)
    if not results:
        return 3
    if st[SC.STATE_FAIL]:
        return 2
    return 0


def run_cli_check_ip(core: "ControllerCore", args) -> int:
    """检查某一个 IP 到底在不在探测范围里、能不能联系上。

    用户问过"我有一台是 .98，探测列表里没有，是不是你设置了范围" ——
    这个命令就是回答这个问题的：先把"这个地址在不在要扫的范围里"打出来，
    再实际发一次单播探测，最后给出没回应的可能原因。
    """
    def log(msg, level="info"):
        print(msg, flush=True)

    target = str(args.check_ip or "").strip()
    try:
        import ipaddress as _ip

        addr = _ip.IPv4Address(target)
    except ValueError:
        print(f"「{target}」不是合法的 IPv4 地址", file=sys.stderr)
        return 1

    core.log = log
    core.start()          # start() 里已经枚举网卡 / 算好广播目标了

    usable = [a for a in core.adapters if a.usable]
    print("-" * 62)
    inside = []
    for ad in usable:
        net = ad.network
        if addr in net:
            inside.append(ad)
            print(f"网卡 {ad.kind_name} {ad.ip}/{ad.prefix}（{ad.title}）")
            print(f"  该地址在这个网段内 ✓  本网段要扫的地址："
                  f"{net.network_address + 1} … {net.broadcast_address - 1}"
                  f"（共 {max(0, net.num_addresses - 2)} 个，含 {addr}）")
    if not inside:
        print(f"⚠ 本机没有任何网卡覆盖 {addr} —— 它在别的网段：")
        print("   * 如果你填了「额外网段」，它会被定向广播 + 逐台单播探测；")
        print("   * 否则只能靠它主动报到（广播）被发现，扫描扫不到它。")
    hosts, notes = N.sweep_hosts(core.adapters or None, extra=core.spec_hosts)
    if str(addr) in hosts:
        print(f"实际探测列表里包含 {addr} ✓（本次要探测 {len(hosts)} 个地址）")
    else:
        print(f"实际探测列表里**不含** {addr}（本次 {len(hosts)} 个地址）"
              f" —— 原因见上面的说明")

    print(f"正在探测 {addr} …")
    alive = core.probe([str(addr)], wait=4.0)
    if str(addr) in alive:
        print(f"✓ {addr} 回应了探测：网络是通的，它应该能收到推送。")
        print("  如果它还是没换壁纸，去它自己的 agent.log 看下载那一步。")
    else:
        print(f"✗ {addr} 没有回应。按可能性排查：")
        print(f"   ① 这台机器上被控端没在跑：在它上面执行  WallpaperAgent.exe --status")
        print(f"   ② 它的入站 UDP {core.udp_port} 被防火墙/安全软件挡了（最常见）：")
        print(f"      在它上面执行  netsh advfirewall firewall show rule name=all dir=in | findstr {core.udp_port}")
        print(f"      没有输出就是没放行，补一条：")
        print(f"      netsh advfirewall firewall add rule name=\"WinWallpaperPush-UDP-Broadcast\""
              f" dir=in action=allow protocol=UDP localport={core.udp_port}")
        print("   ③ 它的地址其实是别的（多张网卡 / DHCP 换过）—— 在它上面用 ipconfig 看一眼")
        print("   ④ 老版本被控端没有「主动报到」：那它一旦收不到我们的包就完全隐身")
    core.stop()
    return 0


def run_cli(core: ControllerCore, args) -> int:
    def log(msg, level="info"):
        mark = {"ok": "OK  ", "err": "ERR ", "warn": "WARN", "dim": "    "}.get(level, "    ")
        print(f"[{time.strftime('%H:%M:%S')}] {mark} {msg}", flush=True)

    core.log = log
    core.start()

    if args.scan or not args.push:
        core.scan(deep=None if args.sweep is None else args.sweep)

    task = ""
    if args.push:
        task = core.push(args.push, args.style)

    wait = args.wait if args.wait is not None else (8 if args.push else 4)
    print(f"等待 {wait} 秒收集被控端回应…", flush=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.5)

    online = core.online_devices()
    print("-" * 62)
    print(f"在线设备：{len(online)} 台")
    for _key, d in sorted(online, key=lambda kv: kv[1].get("ip", "")):
        print(f"  {d.get('ip', '?'):<16} {d.get('host', ''):<22} {d.get('user', '')}")
    if task:
        ok, fail, pending = core.ack_summary(task)
        print(f"本次推送「{core.task_info(task).get('name', '')}」："
              f"成功 {ok} · 失败 {fail} · 待确认 {pending}")
        with core._dev_lock:
            acks = dict(core._acks.get(task, {}))
        for _key, (good, err, _ts, aip) in sorted(acks.items()):
            print(f"  {aip:<16} {'已应用' if good else '失败 ' + err}")

    core.stop()
    if task:
        ok, fail, pending = core.ack_summary(task)
        if ok:
            return 0          # 至少有一台确认换好了
        if not online:
            return 3          # 压根没发现在线设备（检查防火墙 / 是否同一网段）
        return 2              # 有设备在线，但没人确认
    return 0


# ================================================================ 入口

def main(argv=None) -> int:
    # 打包成无控制台 exe 时，带参数运行才能看到文字输出
    if argv is None and len(sys.argv) > 1:
        N.attach_parent_console()

    # 某些控制台代码页（如 437）无法表示中文，会导致 print 直接抛异常；
    # 这里降级成替换字符，保证程序不会因为「打日志」而崩掉。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        prog="controller",
        description=f"{APP_NAME} v{APP_VER} —— 广播推送 Windows 桌面壁纸",
    )
    ap.add_argument("--push", metavar="图片", help="命令行直接推送图片（不开界面）")
    ap.add_argument("--push-toast", dest="push_toast", metavar="JSON",
                    help="命令行推送一条通知：JSON 文件路径，或直接给 JSON 文本")
    ap.add_argument("--toast-local", action="store_true",
                    help="配合 --push-toast / --set-app-name：只发给本机（测试用）")
    ap.add_argument("--set-app-name", dest="set_app_name", metavar="名字",
                    help="广播改被控端「通知上显示的应用名」（持久化，重启仍生效）；"
                         "填 default 让它们恢复默认名字")
    ap.add_argument("--check-ip", dest="check_ip", metavar="IP",
                    help="检查某个 IP 在不在探测范围里、能不能联系上（排查「这台没反应」）")
    # ---- 远程命令（SSH）：用系统自带的 ssh 客户端在每台设备上执行命令
    ap.add_argument("--ssh-cmd", dest="ssh_cmd", action="append", metavar="命令",
                    help="在每台设备上执行的命令（可给多次；命令里也能用换行分多条）")
    ap.add_argument("--ssh-targets", dest="ssh_targets", metavar="网段/IP",
                    help="发给哪些地址，例如 192.168.1.1-56 或 192.168.1.0/24；"
                         "不给就先扫局域网，发给在线设备")
    ap.add_argument("--ssh-user", dest="ssh_user", metavar="用户名",
                    help="覆盖远程命令用的 SSH 用户名")
    ap.add_argument("--ssh-key", dest="ssh_key", metavar="私钥",
                    help="覆盖 ssh -i 的私钥路径")
    ap.add_argument("--ssh-mode", dest="ssh_mode", choices=[SC.MODE_SESSION, SC.MODE_ONESHOT],
                    help=f"执行方式：{SC.MODE_SESSION}（登录一次逐条发，默认）/"
                         f"{SC.MODE_ONESHOT}（每条一次连接，有退出码）")
    ap.add_argument("--ssh-timeout", dest="ssh_timeout", type=float, metavar="秒",
                    help="单条命令超时秒数（默认 25）")
    ap.add_argument("--ssh-workers", dest="ssh_workers", type=int, metavar="台",
                    help="远程命令并发台数（默认 4）")
    ap.add_argument("--scan", action="store_true", help="广播扫描在线设备")
    ap.add_argument("--wait", type=float, help="命令模式下等待回应的秒数")
    ap.add_argument("--style", help=f"壁纸契合度：{P.style_help()}")
    ap.add_argument("--port", type=int, help=f"覆盖 UDP 广播端口（默认 {P.UDP_PORT}）")
    ap.add_argument("--targets", help="额外广播地址，逗号分隔，例如 192.168.2.255")
    ap.add_argument("--no-sweep", dest="sweep", action="store_false", default=None,
                    help="只广播，不做逐台单播探测（深度扫描）")
    ap.add_argument("--sweep", dest="sweep", action="store_true",
                    help="强制逐台单播探测（默认开启）")
    ap.add_argument("--auto-scan", type=float, metavar="秒",
                    help="后台自动重扫间隔，0 = 关闭（默认 30）")
    ap.add_argument("--selftest", action="store_true", help="打印环境自检信息后退出")
    args = ap.parse_args(argv)

    if args.selftest:
        adapters = N.list_adapters()
        # 自检在配置加载之前跑，这里自己读一次配置，好把「额外网段」也算进去
        _cfg_self = dict(DEFAULT_CFG)
        _cfg_self.update(N.load_json(os.path.join(N.app_dir(),
                                                  "controller_config.json")))
        if args.targets:
            _cfg_self["targets"] = [x for x in args.targets.replace(",", " ").split() if x]
        extra_raw = [str(x) for x in (_cfg_self.get("targets") or [])]
        spec_send, spec_hosts, spec_notes = N.parse_target_spec(" ".join(extra_raw))
        hosts, notes = N.sweep_hosts(adapters, extra=spec_hosts)
        print(f"{APP_NAME} v{APP_VER}")
        print("Python :", sys.version.split()[0])
        print("本机网卡（有线 / 无线都会列出）：")
        for line in N.adapter_report(adapters):
            print(line)
        skipped = [a for a in adapters if not a.usable]
        if skipped:
            print("未参与广播的网卡：")
            for ad in skipped:
                print(f"  {ad.ip or '（无地址）'} {ad.title}"
                      f"（{'已断开' if not ad.up else '不可用于局域网广播'}）")
        print("广播目标:", ", ".join(N.broadcast_targets(spec_send, adapters=adapters)) or "（无）")
        print("本机 IP :", ", ".join(N.local_ipv4_list()) or "（未检测到）")
        if extra_raw:
            print("额外网段:", " ".join(extra_raw))
            for note in spec_notes:
                print("          " + note)
        print(f"单播探测: {len(hosts)} 个地址"
              + (f"（{hosts[0]} … {hosts[-1]}）" if hosts else ""))
        for note in notes:
            print("          " + note)
        return 0

    cfg_path = os.path.join(N.app_dir(), "controller_config.json")
    cfg = dict(DEFAULT_CFG)
    cfg.update(N.load_json(cfg_path))
    # 配置文件读不出来时必须说一句：否则用户看到的是"配置改了不生效"，
    # 然后程序还会把默认值写回去，把他的设置覆盖掉。
    if N.json_config_warning():
        print(N.json_config_warning(), file=sys.stderr)
    if args.port:
        cfg["udp_port"] = args.port
    if args.style:
        st = P.normalize_style(args.style)
        if not st:
            print(f"无法识别的契合度「{args.style}」。可选：{P.style_help()}",
                  file=sys.stderr)
            return 1
        cfg["style"] = st
    if args.targets:
        cfg["targets"] = [x for x in args.targets.replace(",", " ").split() if x]
    if args.sweep is not None:
        cfg["sweep"] = bool(args.sweep)
    if args.auto_scan is not None:
        cfg["auto_scan"] = max(0.0, float(args.auto_scan))
    # 远程命令的设置也统一在这里洗干净（界面和命令行共用同一份）
    cfg["ssh"] = SC.normalize_cfg(cfg.get("ssh"))
    N.save_json(cfg_path, cfg)

    core = ControllerCore(cfg)

    if args.push_toast:
        return run_cli_toast(core, args)

    if args.set_app_name is not None:
        return run_cli_set_app_name(core, args)

    if args.check_ip:
        return run_cli_check_ip(core, args)

    if args.ssh_cmd:
        return run_cli_ssh(core, args)

    if args.push or args.scan:
        return run_cli(core, args)

    try:
        return run_gui(core, cfg, cfg_path)
    except Exception as e:
        print(f"界面启动失败：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
