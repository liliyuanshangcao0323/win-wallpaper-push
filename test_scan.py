#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网络发现自测（不换壁纸、不需要第二台机器）

专测「控制端能不能在局域网里找到客户机」这条链路，覆盖有线网段：

    1. 网卡枚举     —— 有线 / 无线 / 虚拟 / 回环都要能认出来，前缀长度按真实值算
    2. 广播目标     —— 每个网段一个定向广播地址；/16 就是 x.y.255.255，
                       不能像老版本那样按 /24 猜成 x.y.z.255
    3. 单播探测范围 —— 小网段全扫；大网段只扫本机 /24 + ARP 名单，不刷爆网络
    4. 发送路由     —— 每个目标都从「属于它那个网段」的网卡发出去
                       （默认路由是 WiFi 时，有线网段也不会被顶掉）
    5. 被控端应答   —— 单播 ping 也要有人答（交换机 / AP 拦广播时的救命通道）
    6. 端到端       —— 被控端主动报到 + 控制端深度扫描都能发现设备

运行：
    python test_scan.py

退出码 0 = 全部通过，1 = 有失败项。测试不会改动壁纸，可以随时跑。
"""

from __future__ import annotations

import socket
import sys
import tempfile
import time

import netutil as N
import protocol as P

RESULTS: list[tuple[bool, str]] = []


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


# ---------------------------------------------------------------- 假套接字

class FakeSock:
    """记录 sendto 调用的假套接字，用来验证「从哪张网卡发」。"""

    def __init__(self, name: str):
        self.name = name
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))
        return len(data)

    def targets(self) -> list[str]:
        return [a[0] for _d, a in self.sent]

    def close(self):
        pass


def wired(ip: str, prefix: int = 24) -> N.Adapter:
    return N.Adapter(ip=ip, prefix=prefix, name="以太网", desc="Intel(R) Ethernet I219-LM",
                     kind=N.KIND_WIRED, up=True)


def wireless(ip: str, prefix: int = 16) -> N.Adapter:
    return N.Adapter(ip=ip, prefix=prefix, name="WLAN", desc="Intel(R) Wi-Fi 6 AX203",
                     kind=N.KIND_WIRELESS, up=True)


# ---------------------------------------------------------------- 1. 网卡枚举

def test_adapters() -> None:
    print("\n[1] 网卡枚举")
    ads = N.list_adapters()
    check(bool(ads), "枚举到本机网卡",
          "；".join(a.describe() for a in ads) or "（一个都没有）")
    usable = [a for a in ads if a.usable]
    check(bool(usable), "至少有一张可用的局域网网卡（非回环 / 非 169.254）",
          "，".join(f"{a.ip}/{a.prefix}" for a in usable))

    order = [N._KIND_ORDER.get(a.kind, 9) for a in ads]
    check(order == sorted(order), "网卡按「有线优先」排序", str([a.kind_name for a in ads]))

    # 分类：真实网卡的描述 + 典型驱动名
    cases = [
        ((6, "以太网", "Intel(R) Ethernet Connection (2) I219-V"), N.KIND_WIRED, "有线网卡"),
        ((6, "Ethernet", "Realtek PCIe GbE Family Controller"), N.KIND_WIRED, "英文有线网卡"),
        ((71, "WLAN", "Intel(R) Wi-Fi 6 AX203"), N.KIND_WIRELESS, "无线网卡"),
        ((6, "无线局域网适配器 WLAN", "Intel(R) Wi-Fi 6 AX203"), N.KIND_WIRELESS, "标题带「无线」"),
        ((6, "vEthernet (Default Switch)", "Hyper-V Virtual Ethernet Adapter"), N.KIND_VIRTUAL, "Hyper-V 虚拟网卡"),
        ((6, "VMware Network Adapter VMnet8", "VMware Virtual Ethernet Adapter"), N.KIND_VIRTUAL, "VMware 虚拟网卡"),
        ((6, "本地连接* 9", "Microsoft Wi-Fi Direct Virtual Adapter"), N.KIND_VIRTUAL, "WiFi Direct 虚拟网卡"),
        ((6, "蓝牙网络连接", "Bluetooth Device (Personal Area Network)"), N.KIND_VIRTUAL, "蓝牙网卡"),
        ((24, "Loopback Pseudo-Interface 1", "Software Loopback Interface 1"), N.KIND_LOOPBACK, "回环"),
    ]
    bad = []
    for (if_type, name, desc), want, label in cases:
        got = N._classify(if_type, name, desc)
        if got != want:
            bad.append(f"{label} 判成了 {got}")
    check(not bad, "有线 / 无线 / 虚拟网卡分类正确", "；".join(bad) or f"{len(cases)} 个用例")

    down = N.Adapter(ip="192.168.9.9", prefix=24, name="以太网 2", up=False)
    check(not down.usable, "断开的网卡不会被当成可用网卡")
    ll = N.Adapter(ip="169.254.5.5", prefix=16, name="本地连接* 9")
    check(not ll.usable, "169.254 自动地址不会被拿去扫描")

    # ipconfig 兜底解析（ctypes 万一失效时靠它）
    fb = N._adapters_ipconfig()
    check(bool(fb), "ipconfig 兜底解析也能认出网卡",
          "；".join(f"{a.kind_name} {a.ip}/{a.prefix}" for a in fb) or "（解析为空）")


# ---------------------------------------------------------------- 2. 广播目标

def test_targets() -> None:
    print("\n[2] 广播地址计算")
    w = wired("192.168.1.50", 24)
    check(w.broadcast == "192.168.1.255", "/24 网卡广播地址正确", w.broadcast)
    check(w.netmask == "255.255.255.0", "/24 掩码正确", w.netmask)

    big = wired("10.20.30.40", 16)
    check(big.broadcast == "10.20.255.255", "/16 网卡按真实前缀算广播地址", big.broadcast)
    check(big.netmask == "255.255.0.0", "/16 掩码正确", big.netmask)

    t = N.broadcast_targets(adapters=[w, big])
    check("192.168.1.255" in t and "10.20.255.255" in t,
          "有线 + 大网段都在广播目标里", "，".join(t))
    check("255.255.255.255" in t, "保留受限广播 255.255.255.255")
    check("127.0.0.1" in t, "保留 127.0.0.1（同机自测）")
    check("10.20.30.255" not in t,
          "不再按 /24 乱猜（老版本会把 /16 猜成 x.y.z.255）",
          "，".join(t))

    t2 = N.broadcast_targets(["192.168.2.255", "10.0.0.255"], adapters=[w])
    check(t2[-2:] == ["192.168.2.255", "10.0.0.255"],
          "用户手填的额外广播地址会带上", "，".join(t2))

    real = N.subnet_broadcasts()
    check(real == [a.broadcast for a in N.list_adapters() if a.usable],
          "本机各网段的定向广播地址（含所有有线/无线网卡）",
          "，".join(real) or "（无）")


# ---------------------------------------------------------------- 3. 单播探测范围

def test_sweep() -> None:
    print("\n[3] 逐台单播探测范围")
    small = wired("192.168.1.50", 24)
    hosts, notes = N.sweep_hosts([small], include_arp=False)
    check(len(hosts) == 254, "/24 网段会逐台探测 254 个地址", f"{len(hosts)} 个")
    check("192.168.1.1" in hosts and "192.168.1.254" in hosts,
          "探测范围覆盖整段", f"{hosts[0]} … {hosts[-1]}")
    check("192.168.1.255" not in hosts and "192.168.1.0" not in hosts,
          "不会把网络地址 / 广播地址也算进去")

    big = wired("10.20.30.40", 16)
    hosts2, notes2 = N.sweep_hosts([big], include_arp=False)
    check(len(hosts2) == 254, "/16 这种大网段不会全扫（只扫本机 /24）", f"{len(hosts2)} 个")
    check(all(h.startswith("10.20.30.") for h in hosts2), "大网段只扫本机所在的那一段")
    check(any("网段过大" in n for n in notes2), "大网段会在日志里说明",
          "；".join(notes2))

    mine = set(N.local_ipv4_list())
    real, _notes = N.sweep_hosts(include_arp=False)
    check(not (set(real) & mine), "不会给自己发单播探测（同端口套接字会抢包）",
          f"本机地址 {sorted(mine)}")

    witharp, notes3 = N.sweep_hosts([small], include_arp=True)
    check(len(witharp) >= 254, "ARP 名单会补充到探测范围里",
          "；".join(n for n in notes3 if "ARP" in n) or "（本机 ARP 表为空，正常）")


# ---------------------------------------------------------------- 4. 发送路由

def test_egress() -> None:
    print("\n[4] 广播走哪张网卡")
    from controller import ControllerCore

    w = wired("192.168.1.50", 24)
    wl = wireless("10.20.30.40", 16)
    ctrl = ControllerCore({})

    main, sw, swl = FakeSock("默认路由"), FakeSock("有线"), FakeSock("无线")
    ctrl._sock = main
    ctrl.adapters = [w, wl]
    ctrl._send_socks = {w.ip: sw, wl.ip: swl}

    check(ctrl._egress_for("255.255.255.255") == [sw, swl],
          "受限广播从每张网卡各发一份（老版本只走默认路由）")
    check(ctrl._egress_for("192.168.1.255") == [sw],
          "有线网段的定向广播从有线网卡发出")
    check(ctrl._egress_for("10.20.255.255") == [swl],
          "无线网段的定向广播从无线网卡发出")
    check(ctrl._egress_for("192.168.1.77") == [sw],
          "有线网段内的单播从有线网卡发出")
    check(ctrl._egress_for("8.8.8.8") == [main],
          "网段外的地址交回系统路由")
    check(ctrl._egress_for("127.0.0.1") == [main],
          "回环地址交回系统路由（同机自测）")

    # 两台网卡同网段（笔记本插网线 + 连 WiFi 且在同一网段）时两张都要发
    w2 = wired("192.168.1.60", 24)
    s2 = FakeSock("有线2")
    ctrl.adapters = [w, w2, wl]
    ctrl._send_socks[w2.ip] = s2
    check(ctrl._egress_for("192.168.1.255") == [sw, s2],
          "两张网卡在同一网段时都发（撞上网段重叠也不漏机器）")

    # 真跑一遍 _broadcast，看每个目标实际落到哪张网卡上
    for s in (main, sw, swl, s2):
        s.sent.clear()
    ctrl.targets = N.broadcast_targets(adapters=[w, wl])
    ctrl._broadcast(P.make(P.MSG_PING, nonce="t"), times=1, gap=0)
    check(sorted(sw.targets()) == sorted(["255.255.255.255", "192.168.1.255"]),
          "有线网卡实际发到：受限广播 + 本网段定向广播", str(sw.targets()))
    check(s2.targets() == ["255.255.255.255", "192.168.1.255"],
          "同网段的第二张网卡也发一份（不漏机器）", str(s2.targets()))
    check(sorted(swl.targets()) == sorted(["255.255.255.255", "10.20.255.255"]),
          "无线网卡实际发到：受限广播 + 本网段定向广播", str(swl.targets()))
    check(main.targets() == ["127.0.0.1"],
          "默认路由只负责 127.0.0.1", str(main.targets()))

    # 逐台单播也走对应网卡
    for s in (main, sw, swl, s2):
        s.sent.clear()
    ctrl.last_sweep_count = ctrl._unicast_ping(P.dumps(P.make(P.MSG_PING, nonce="s")),
                                               ["192.168.1.7", "10.20.9.9"])
    check(ctrl.last_sweep_count == 2, "单播探测计数正确", str(ctrl.last_sweep_count))
    check(sw.targets() == ["192.168.1.7"] and swl.targets() == ["10.20.9.9"],
          "单播探测也从目标所在网段的网卡发出",
          f"有线→{sw.targets()} 无线→{swl.targets()}")


# ---------------------------------------------------------------- 5. 被控端应答单播

def test_agent_answers_unicast() -> None:
    print("\n[5] 被控端会不会应答单播 ping（广播被交换机拦住时的救命通道）")
    from agent import AgentCore

    ip = next((a.ip for a in N.list_adapters() if a.usable), "")
    if not ip:
        skip("被控端应答单播 ping", "本机没有可用网卡")
        return

    udp_port, reply_port = 39271, 39273
    with tempfile.TemporaryDirectory(prefix="wpp_scan_") as tmp:
        agent = AgentCore({
            "udp_port": udp_port, "style": "填充", "keep": 1,
            "wallpaper_dir": tmp, "retry": 1, "hello_interval": 0,
        })
        agent.start()
        time.sleep(0.3)

        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("0.0.0.0", reply_port))
        rx.settimeout(5)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tx.sendto(P.dumps(P.make(P.MSG_PING, nonce="scan1",
                                     reply_port=reply_port, ts=time.time())),
                      (ip, udp_port))
            pong = None
            try:
                data, addr = rx.recvfrom(4096)
                pong = P.loads(data)
            except socket.timeout:
                addr = ("", 0)
            check(pong is not None and pong.get("type") == P.MSG_PONG,
                  "单播 ping 收到 pong 应答（不需要广播）",
                  f"来自 {addr[0]}，主机名 {pong.get('host') if pong else '（超时）'}")
            if pong:
                check(pong.get("nonce") == "scan1", "应答里带着扫描随机数（能对上号）")
                check(bool(pong.get("host")), "应答里带着计算机名（列表里能看明白）")
        finally:
            tx.close()
            rx.close()
            agent.stop()


# ---------------------------------------------------------------- 6. 端到端

def test_end_to_end() -> None:
    print("\n[6] 端到端：主动报到 + 深度扫描")
    from agent import AgentCore
    from controller import ControllerCore

    with tempfile.TemporaryDirectory(prefix="wpp_scan_") as tmp:
        agent = AgentCore({
            "udp_port": P.UDP_PORT, "style": "填充", "keep": 1,
            "wallpaper_dir": tmp, "retry": 1, "hello_interval": 2.0,
        })
        ctrl = ControllerCore({
            "udp_port": P.UDP_PORT, "tcp_port": P.TCP_PORT,
            "reply_port": P.REPLY_PORT, "style": "填充", "targets": [],
            "sweep": True, "auto_scan": 0,
        })
        ctrl_logs: list[str] = []
        ctrl.log = lambda m, l="info": ctrl_logs.append(f"[{l}] {m}")
        try:
            ctrl.start()          # 先让控制端把端口占好
            time.sleep(0.3)
            agent.start()          # 被控端启动后会主动报到

            # 完全不扫描，只等被控端自己报到
            deadline = time.time() + 6
            while time.time() < deadline and not ctrl.online_devices():
                time.sleep(0.2)
            found = ctrl.online_devices()
            check(bool(found), "被控端主动报到，控制端不扫描也能发现它",
                  "；".join(f"{d.get('ip')} {d.get('host')}" for _k, d in found) or "（没等到）")

            print("\n[7] 深度扫描")
            ctrl.devices.clear()
            ctrl.scan(deep=True)
            check(ctrl.last_sweep_count > 0, "深度扫描发出了单播探测",
                  f"{ctrl.last_sweep_count} 个地址")
            deadline = time.time() + 6
            while time.time() < deadline and not ctrl.online_devices():
                time.sleep(0.2)
            check(len(ctrl.online_devices()) == 1,
                  "深度扫描找到 1 台设备（多网卡按计算机名合并）",
                  "；".join(f"{d.get('ip')} {d.get('host')}"
                            for _k, d in ctrl.online_devices()) or "（无）")

            ctrl.scan(deep=False)
            check(ctrl.last_sweep_count == 0, "关掉深度扫描时只广播，不发单播探测")

            info = next((m for m in ctrl_logs if "单播探测范围" in m), "")
            check(bool(info), "扫描范围会写进日志（排查时看得见）",
                  info or "；".join(ctrl_logs[-4:]))
        finally:
            agent.stop()
            ctrl.stop()
            time.sleep(0.2)


def main() -> int:
    print("=" * 68)
    print("  Win 壁纸推送 —— 网络发现自测（有线 / 无线 / 广播 / 单播）")
    print("=" * 68)
    test_adapters()
    test_targets()
    test_sweep()
    test_egress()
    test_agent_answers_unicast()
    test_end_to_end()

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
