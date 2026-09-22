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


# ---------------------------------------------------------------- 3b. 指定网段

def test_target_spec() -> None:
    """用户填「额外网段」：以前只认广播地址，现在 CIDR / 范围 / 单机都认。"""
    print("\n[3b] 指定网段（额外网段解析）")

    send, hosts, notes = N.parse_target_spec("192.168.1.0/24")
    check(send == ["192.168.1.255"], "192.168.1.0/24 → 定向广播 192.168.1.255",
          "，".join(send))
    check(len(hosts) == 254 and hosts[0] == "192.168.1.1" and hosts[-1] == "192.168.1.254",
          "同一个网段会逐台探测 254 个地址", f"{len(hosts)} 个：{hosts[:2]}…{hosts[-1:]}")

    send2, hosts2, _n2 = N.parse_target_spec("192.168.1.255")
    check(send2 == ["192.168.1.255"] and len(hosts2) == 254,
          "直接填广播地址也能用（顺带扫这一整个 /24）", f"{send2} / {len(hosts2)} 个")

    send3, hosts3, _n3 = N.parse_target_spec("192.168.1.77")
    check(send3 == ["192.168.1.77"] and hosts3 == ["192.168.1.77"],
          "填单台机器 → 只单播给它", f"{send3} / {hosts3}")

    _s4, hosts4, _n4 = N.parse_target_spec("192.168.1.1-192.168.1.5")
    check(len(hosts4) == 5, "地址范围会展开成 5 个地址", str(hosts4))

    send5, hosts5, notes5 = N.parse_target_spec("10.0.0.0/16")
    check(send5 == ["10.0.255.255"] and len(hosts5) == 4094,
          "/16 这种大网段只取前 4094 个（不会把网络打爆）", f"{len(hosts5)} 个")
    check(any("太大" in n for n in notes5), "并且会说明没扫全", "；".join(notes5))

    send6, hosts6, notes6 = N.parse_target_spec("乱写的东西 192.168.5.0/24")
    check(send6 == ["192.168.5.255"] and len(hosts6) == 254,
          "非法写法被忽略、合法的照常用", "；".join(notes6))
    check(any("忽略" in n for n in notes6), "非法写法会在日志里说明原因")

    multi_send, multi_hosts, _n7 = N.parse_target_spec("192.168.1.0/24 192.168.2.0/24")
    check(len(multi_send) == 2 and len(multi_hosts) == 508,
          "多个网段可以一起填（空格/逗号分隔）", f"{multi_send} / {len(multi_hosts)} 个")

    # 指定网段要真的进到探测列表里（这才是"扫得到"的关键）
    ads = [wired("10.13.3.214", 16)]          # 模拟控制端那张 /16 的网卡
    hosts_all, notes_all = N.sweep_hosts(ads, include_arp=False,
                                         extra=["192.168.1.1", "192.168.1.2"])
    check("192.168.1.1" in hosts_all and "192.168.1.2" in hosts_all,
          "手工指定的地址会进到逐台探测列表里（大网段也照扫）",
          f"共 {len(hosts_all)} 个")
    check(any("手工指定" in n for n in notes_all), "日志里会写明补了多少个目标",
          "；".join(n for n in notes_all if "手工" in n))

    # 用户问过："我有一台是 .98，探测列表里没有，是不是你设置了范围"
    # —— 一个 /24 就是整段 .1~.254，任何一台都在里面，不该有例外
    lan = wired("192.168.1.100", 24)
    hosts24, _n24 = N.sweep_hosts([lan], include_arp=False)
    check(len(hosts24) == 254, "/24 网段会探测 254 个地址（不是抽样的子集）",
          f"{len(hosts24)} 个")
    check(all(f"10.127.112.{i}" in hosts24 for i in (1, 98, 150, 254)),
          "网段里任意一台都在探测列表里（含 .98）",
          f"范围 {hosts24[0]} … {hosts24[-1]}")
    spec_send, spec_hosts24, _n = N.parse_target_spec("192.168.1.0/24")
    check(f"192.168.1.98" in spec_hosts24,
          "手填 192.168.1.0/24 时也会把 .98 算进去", f"{len(spec_hosts24)} 个")


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
    import socket as _socket

    from agent import AgentCore
    from controller import ControllerCore

    agent_host = _socket.gethostname()
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
            # 注意：**不能断言"只有 1 台"** —— 这套自测会真的扫整个局域网，
            # 别人机器上跑着的被控端（甚至别人的控制端）都可能被扫到；
            # 之前就因为这个假设挂过一次（顺带发现新探测确实多找到了一台）。
            ours = [d for _k, d in ctrl.online_devices()
                    if d.get("host") == agent_host]
            check(bool(ours), "深度扫描找到了本机的被控端（多网卡按计算机名合并）",
                  f"共发现 {len(ctrl.online_devices())} 台；"
                  + "；".join(f"{d.get('ip')} {d.get('host')}"
                              for _k, d in ctrl.online_devices()) or "（无）")

            ctrl.scan(deep=False)
            check(ctrl.last_sweep_count == 0, "关掉深度扫描时只广播，不发单播探测")

            info = next((m for m in ctrl_logs if "单播探测范围" in m), "")
            check(bool(info), "扫描范围会写进日志（排查时看得见）",
                  info or "；".join(ctrl_logs[-4:]))

            print("\n[8] 推送时会不会单独发给已知设备（设备多时广播总有漏的）")
            # 关键回归：广播会被交换机/AP 拦掉，光广播"谁在线谁改"实测会漏机器。
            # 现在除了广播，还会把同一条公告**逐个单播给已知设备**，并对没回执的补发。
            sent_to: list[str] = []
            real_send = ctrl._send_packet

            def spy(raw, targets):
                sent_to.extend(targets)
                return real_send(raw, targets)

            ctrl._send_packet = spy            # type: ignore[assignment]
            try:
                ping = P.make("noop", ts=time.time())   # 被控端不认识这个消息，不会回执
                ctrl.announce(ping, "task-loopback-test", rounds=3, gap=0.6,
                              label="测试")
                known = ctrl.known_ips()
                check(bool(known), "能算出「已知设备」列表", "，".join(known) or "（空）")
                hit = [ip for ip in known if ip in sent_to]
                check(bool(hit), "公告会直接单播给已知设备（不只靠广播）",
                      f"单播目标 {sorted(set(sent_to))[:6]}")
                first = len(sent_to)
                time.sleep(2.0)
                check(len(sent_to) > first,
                      "没回执的机器会被自动补发（第 2 轮单播重发）",
                      f"补发前 {first} 个包 → 补发后 {len(sent_to)} 个包")
                check(any("第 2 轮补发" in m for m in ctrl_logs),
                      "补发这件事会写进日志",
                      next((m for m in ctrl_logs if "补发" in m), "（没有）"))

                # 补发完还不回执的机器：要点名 + 探一次"它到底还在不在"
                ctrl_logs.clear()
                ctrl._touch_device("192.168.1.99", "PC-LOST", "user", "", 0)
                ping2 = P.make("noop", ts=time.time())
                ctrl.announce(ping2, "task-lost-test", rounds=2, gap=0.5,
                              label="掉线测试")
                deadline = time.time() + 12
                while time.time() < deadline:
                    if any("没回执" in m for m in ctrl_logs):
                        break
                    time.sleep(0.3)
                joined = "；".join(ctrl_logs)
                check("192.168.1.99" in joined and "没回执" in joined,
                      "没回执的机器会被点名列出", joined[:100] or "（没有）")
                # 探测要等 4 秒才有结论，这里再等一会儿
                deadline = time.time() + 12
                while time.time() < deadline:
                    if any("不回应" in m for m in ctrl_logs):
                        break
                    time.sleep(0.3)
                check("连扫描也不回应" in joined or any("不回应" in m for m in ctrl_logs),
                      "会再探一次并给出结论：这台的网络根本不通",
                      next((m for m in ctrl_logs if "不回应" in m), "（没有）"))
            finally:
                ctrl._send_packet = real_send   # type: ignore[assignment]
        finally:
            agent.stop()
            ctrl.stop()
            time.sleep(0.2)


# ---------------------------------------------------------------- 9. 设备列表计数

def test_device_list_keys() -> None:
    """设备列表按 IP 计数：**同名机器必须分开显示**。

    回归用例（用户实际遇到）：客户机是克隆镜像 / 同批装的，计算机名经常一样；
    老版本按计算机名归并设备，于是 50 台在列表里被合并成 1 行 ——
    界面看起来"只发现一台"，但日志里一堆。
    """
    print("\n[9] 设备列表计数（同名机器要分开显示）")
    from controller import ControllerCore

    def new_core() -> ControllerCore:
        c = ControllerCore({"udp_port": 39921, "tcp_port": 39922,
                            "reply_port": 39923, "targets": []})
        c.log = lambda m, l="info": None
        return c

    ctrl = new_core()
    for i in range(1, 51):
        ctrl._touch_device(f"10.127.112.{i}", "PC-CLONE", "user", "", 0)
    check(len(ctrl.devices) == 50, "50 台同名机器会显示成 50 行（以前合成 1 行）",
          f"{len(ctrl.devices)} 行")
    check(len(ctrl.online_devices()) == 50, "在线台数也是 50（顶部徽标不再虚低）",
          f"{len(ctrl.online_devices())} 台")

    # 回执也要按机器分开统计：有机器没回执时能点出名字
    with ctrl._dev_lock:
        ctrl._acks["t"] = {}
        for i in range(1, 51):
            ctrl._acks["t"][f"10.127.112.{i}"] = (i % 7 != 0, "", time.time(),
                                                  f"10.127.112.{i}")
    ok, fail, pending = ctrl.ack_summary("t")
    check((ok, fail, pending) == (43, 7, 0),
          "回执按机器统计（成功 43 · 失败 7）", f"{ok}/{fail}/{pending}")

    # 本机自测那种情况仍要合成一条（127.0.0.1 + 局域网 IP 是同一台机器）
    ctrl2 = new_core()
    ctrl2._touch_device("10.13.3.214", "DESKTOP-X", "li", "", 0)
    ctrl2._touch_device("127.0.0.1", "DESKTOP-X", "li", "", 0)
    check(len(ctrl2.devices) == 1,
          "同一台机器从 127.0.0.1 和局域网 IP 各报一次仍算一台",
          "；".join(f"{d.get('ip')} {d.get('host')}" for d in ctrl2.devices.values()))


def main() -> int:
    print("=" * 68)
    print("  Win 壁纸推送 —— 网络发现自测（有线 / 无线 / 广播 / 单播）")
    print("=" * 68)
    test_adapters()
    test_targets()
    test_sweep()
    test_target_spec()
    test_egress()
    test_agent_answers_unicast()
    test_end_to_end()
    test_device_list_keys()

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
