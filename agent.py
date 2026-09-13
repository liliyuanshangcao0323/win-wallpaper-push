#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""被控制端（客户端）

职责：
    1. 监听 UDP 广播，接收控制端发来的「新壁纸公告」
    2. 用 TCP 回连控制端，把图片完整下载下来并校验 sha256
    3. 调用 Windows 壁纸接口 SystemParametersInfoW 更换桌面壁纸
    4. 把执行结果回报给控制端

用法：
    python agent.py                 # 带状态界面运行
    python agent.py --silent        # 无界面后台运行（部署到多台机器时用）
    python agent.py --set 图.jpg    # 只设置一次本地壁纸，然后退出
    python agent.py --selftest      # 环境自检
"""

from __future__ import annotations

import argparse
import hashlib
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time

import agentauth
import netutil as N
import protocol as P
import toast as toastmod
import toastspec as TS
import wallpaper
import winipc

APP_NAME = "Win壁纸推送 - 被控端"
APP_VER = "1.2.0"
APP_DIR_NAME = "WinWallpaperPush"      # ProgramData / LOCALAPPDATA 下的子目录名
CONFIG_NAME = "agent_config.json"
LOG_NAME = "agent.log"                 # 静默后台运行时唯一的排查线索
STATE_NAME = "agent_state.json"        # 运行状态快照，给 --status 用
# --test-toast 默认用哪种停留时长（留空 = short；命令行 --duration 可以覆盖）
TEST_TOAST_DURATION = ""


def default_wallpaper_dir() -> str:
    """壁纸落地目录：放在用户 AppData 下，保证文件不会被临时清理掉。

    注意必须是「每用户」目录：Wallpaper 是每用户的设置，多用户机器上
    每个登录会话各存各的，互不干扰。
    """
    return os.path.join(N.local_app_data_dir(APP_DIR_NAME), "wallpapers")


def default_log_path() -> str:
    """日志文件路径（每用户，永远可写，卸载 MSI 也不会被连带删掉）。"""
    return os.path.join(N.local_app_data_dir(APP_DIR_NAME), LOG_NAME)


def state_file_path() -> str:
    """运行状态快照路径。"""
    return os.path.join(N.local_app_data_dir(APP_DIR_NAME), STATE_NAME)


def config_candidates() -> list[str]:
    """配置文件候选路径，按优先级从高到低。

    1. exe 旁边            —— 便携模式（解压即用），保持和以前一致
    2. C:\\ProgramData\\...  —— MSI 安装后的机器级配置，管理员可集中下发
    3. %LOCALAPPDATA%\\...  —— 兜底，普通用户一定写得进去
    """
    return [
        os.path.join(N.app_dir(), CONFIG_NAME),
        os.path.join(N.program_data_dir(APP_DIR_NAME), CONFIG_NAME),
        os.path.join(N.local_app_data_dir(APP_DIR_NAME), CONFIG_NAME),
    ]


def resolve_config(override: str = "") -> tuple[str, str]:
    """算出 (读取路径, 写入路径)。

    为什么要分开：MSI 会把程序装到 C:\\Program Files\\，那里的
    agent_config.json 普通用户改不动。这时读取仍然以它为准（管理员集中配置），
    但写入要落到可写的位置，否则程序每次启动都会因为存配置失败而报错。
    """
    if override:
        return override, override

    cands = config_candidates()
    existing = next((p for p in cands if os.path.isfile(p)), "")
    if existing:
        if N.is_dir_writable(os.path.dirname(existing)):
            return existing, existing
        # 只读的集中配置：读它，但改动写到兜底位置
        return existing, cands[-1]

    # 一个都不存在：找第一个所在目录可写的位置来创建
    for p in cands:
        if N.is_dir_writable(os.path.dirname(p)):
            return p, p
    return cands[-1], cands[-1]


DEFAULT_CFG = {
    "udp_port": P.UDP_PORT,
    "style": P.DEFAULT_STYLE,
    "keep": 10,              # 本地最多保留几张历史壁纸
    "wallpaper_dir": "",     # 留空用默认目录
    "silent": False,
    "retry": 3,              # 下载重试次数
    "hello_interval": P.HELLO_INTERVAL,   # 主动报到的间隔（秒），0 = 不主动报到
    "log_file": "",          # 日志文件路径；留空用默认位置，填 off 关闭写文件
    "log_max_kb": 512,       # 日志文件上限（KB），超了滚动成 .1
    "toast_enabled": True,   # 收不收控制端推来的通知
    "toast_module_path": "", # BurntToast 模块目录；留空按 exe 旁边 / 自动装的位置找
    "toast_min_interval": 1.0,   # 两条通知之间至少隔几秒（防刷屏）
    "toast_auto_install": True,  # 没有 BurntToast 时自动装（需要能上 PSGallery）
    "toast_app_name": "",    # 通知上显示的应用名；留空 = 「Win 壁纸推送」
    # 允不允许「控制端广播一下就把本机通知应用名改掉」（持久化改动）。
    # 默认允许：这是给运维批量统一署名的功能；要收紧就在客户机上设为 false。
    "allow_remote_app_name": True,
}


# ================================================================ 核心逻辑

class AgentCore:
    """被控端的全部业务逻辑，与界面完全解耦（界面只是它的观察者）。"""

    def __init__(self, cfg: dict, log=None, on_event=None):
        self.cfg = cfg
        self.udp_port = int(cfg.get("udp_port") or P.UDP_PORT)
        self.style = P.normalize_style(cfg.get("style")) or P.DEFAULT_STYLE
        self.keep = max(1, int(cfg.get("keep") or 10))
        self.retry = max(1, int(cfg.get("retry") or 3))
        self.hello_interval = max(0.0, float(cfg.get("hello_interval")
                                             if cfg.get("hello_interval") is not None
                                             else P.HELLO_INTERVAL))
        self.wallpaper_dir = cfg.get("wallpaper_dir") or default_wallpaper_dir()
        self.config_path = cfg.get("_config_read_path") or ""
        self.config_write_path = cfg.get("_config_write_path") or self.config_path
        self.log_path = cfg.get("_log_path") or ""
        self.silent = bool(cfg.get("silent"))
        self.toast_enabled = bool(cfg.get("toast_enabled", True))
        self.toast_module_path = str(cfg.get("toast_module_path") or "")
        self.toast_auto_install = bool(cfg.get("toast_auto_install", True))
        try:
            self.toast_min_interval = max(0.0, float(cfg.get("toast_min_interval") or 0))
        except (TypeError, ValueError):
            self.toast_min_interval = 1.0

        self.log = log or (lambda msg, level="info": None)
        self.on_event = on_event or (lambda kind, data: None)

        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}       # task_id -> 处理时间，用于广播去重
        self._seen_ping: dict[str, float] = {}  # nonce -> 处理时间，避免重复回应扫描
        # 改通知应用名的任务去重（同上，广播会重复几遍）
        self._seen_name: dict[str, float] = {}
        # 谁最近一次远程改过通知应用名（写进状态文件，给 --status 看）
        self._remote_name: dict = {}
        self._egress: dict[str, socket.socket] = {}   # 网卡地址 -> 发送用套接字
        self._hello_count = 0
        self._started_at = time.time()
        self._last_toast_at = 0.0                     # 通知节流用
        self._toast_lock = threading.Lock()

        # 控制通道（--stop / --show）：由 main() 建好传进来
        self.signals: winipc.AgentSignals | None = None

        self.last_wallpaper = wallpaper.current_wallpaper()
        self.applied_count = 0
        self.last_task = ""
        self.rx_count = 0

    # ---------------------------------------------------------- 启动

    def start(self) -> None:
        os.makedirs(self.wallpaper_dir, exist_ok=True)

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # SO_REUSEADDR 让同一台机器上的多个监听者都能收到广播包
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.bind(("0.0.0.0", self.udp_port))

        threading.Thread(target=self._udp_loop, name="udp", daemon=True).start()

        self.log(f"被控端已启动，正在监听 UDP {self.udp_port}", "ok")
        self.log(f"壁纸目录：{self.wallpaper_dir}", "dim")
        if self.config_path:
            self.log(f"配置文件：{self.config_path}", "dim")
            if self.config_write_path and self.config_write_path != self.config_path:
                self.log(
                    f"该配置为只读（通常由管理员集中下发），参数改动将保存到："
                    f"{self.config_write_path}",
                    "warn",
                )

        # 有线和无线网卡都要能发出去：只发 255.255.255.255 的话，
        # Windows 只会从默认路由那张网卡发，插网线的机器就报不到到。
        self._open_egress_sockets()
        self.log("本机网卡：" + (N.adapter_summary() or "未检测到可用网卡"), "dim")
        self.log(wallpaper.self_test(), "dim")
        # 通知身份（AppId）注册一次就长期有效；不注册的话通知会以
        # 「Windows PowerShell」的名义弹出 —— 既让人看不懂，又像钓鱼弹窗。
        if self.toast_enabled:
            # 通知上显示的名字（配置里可自定义，默认「Win 壁纸推送」）
            toastmod.set_app_display_name(str(self.cfg.get("toast_app_name") or ""))
            ok_id, msg_id = toastmod.ensure_app_id(log=self.log)
            if not ok_id:
                self.log(f"通知身份注册失败：{msg_id}", "warn")
            # 第一次运行时把通知优先级调到最高（允许紧急通知）。
            # 只做一次：这个开关用户随时能在系统设置里关掉，每次启动都强行改回去
            # 等于跟用户对着干；--allow-urgent on/off 可以显式改。
            ok_p, msg_p = toastmod.ensure_notification_priority(log=self.log)
            self.log(msg_p, "dim" if ok_p else "warn")
        self.log(toastmod.self_test(self.toast_module_path), "dim")
        if self.log_path:
            self.log(f"日志文件：{self.log_path}", "dim")
        self._ensure_toast_module_async()

        self._write_state()
        self._start_hello()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        for s in self._egress.values():
            try:
                s.close()
            except OSError:
                pass
        self._egress.clear()
        if self.signals:
            self.signals.close()
            self.signals = None
        # 正常退出就把状态文件删掉：留着说明上次是异常结束。
        # 只删「自己写的」那份（对 pid）—— 同一台机器上可能有别的实例
        # （测试、--force 起的第二个），别把别人的状态误删了。
        try:
            st = N.load_json(state_file_path())
            if not st or int(st.get("pid") or 0) == os.getpid():
                os.remove(state_file_path())
        except OSError:
            pass
        except (TypeError, ValueError):
            pass

    # ---------------------------------------------------------- 运行状态快照

    def _write_state(self) -> None:
        """写一份「我现在是什么状态」的快照，给 `--status` 看。

        静默后台运行时没有窗口可看，管理员在客户机上跑
        `WallpaperAgent.exe --status` 就能知道端口、配置、日志都在哪。
        """
        try:
            N.save_json(state_file_path(), {
                "version": APP_VER,
                "pid": os.getpid(),
                "started": self._started_at,
                "updated": time.time(),
                "silent": self.silent,
                "udp_port": self.udp_port,
                "applied": self.applied_count,
                "toasts": getattr(self, "toast_count", 0),
                "toast": toastmod.self_test(self.toast_module_path),
                "toast_app_name": toastmod.app_display_name(),
                # 最近一次被控制端远程改名（谁改的、什么时候）—— 排查时有用
                "remote_name": self._remote_name or None,
                "last_wallpaper": os.path.basename(self.last_wallpaper or ""),
                "config": self.config_path,
                "log": self.log_path,
                "wallpaper_dir": self.wallpaper_dir,
                "host": socket.gethostname(),
                "user": os.environ.get("USERNAME", ""),
            })
        except Exception:
            # 状态文件只是给人看的，绝不能因为写不了它影响换壁纸
            pass

    # ---------------------------------------------------------- 主动报到

    def _open_egress_sockets(self) -> None:
        """每张可用网卡建一个绑定到它自己地址的发送套接字。"""
        for ad in N.list_adapters():
            if not ad.usable or ad.ip in self._egress:
                continue
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((ad.ip, 0))
                self._egress[ad.ip] = s
            except OSError:
                continue

    def send_hello(self) -> int:
        """广播一条「我上线了」。

        控制端往往一直开着，客户机是后来才开机的；只靠控制端扫描的话，
        这台机器要等到下一次扫描才被发现。这里主动报到，控制端几秒内
        就能在设备列表里看到它。

        发到 REPLY_PORT（控制端的独占回执口）：被控端不监听这个口，
        所以局域网里其它被控端不会为这条消息白跑一趟。
        """
        if self._sock is None:
            return 0
        out = P.make(
            P.MSG_HELLO,
            host=socket.gethostname(),
            user=os.environ.get("USERNAME", ""),
            applied=os.path.basename(self.last_wallpaper or ""),
            count=self.applied_count,
            port=self.udp_port,
            ts=time.time(),
        )
        raw = P.dumps(out)
        socks = list(self._egress.values()) or [self._sock]
        sent = 0
        for target in N.broadcast_targets():
            for s in socks:
                try:
                    s.sendto(raw, (target, P.REPLY_PORT))
                    sent += 1
                except OSError:
                    pass
        self._hello_count += 1
        if sent and self._hello_count == 1:
            self.log(f"已向局域网报到（广播 {sent} 个包，控制端不用扫描就能看到本机）",
                     "dim")
        elif not sent:
            self.log("主动报到失败：局域网里没有可用网卡", "warn")
        return sent

    def _start_hello(self) -> None:
        if self.hello_interval <= 0:
            self.log("主动报到已关闭（hello_interval = 0），只能等控制端扫描", "dim")
            return

        def loop():
            # 刚开机时网络可能还没就绪，隔几秒补报两次
            for delay in (1.5, 4.0, 8.0):
                if self._stop.wait(delay):
                    return
                self.send_hello()
            while not self._stop.wait(self.hello_interval):
                self.send_hello()

        threading.Thread(target=loop, name="hello", daemon=True).start()

    # ---------------------------------------------------------- 广播接收

    def _udp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65535)
            except OSError:
                break
            except Exception as e:  # 单个坏包不能弄死监听线程
                self.log(f"接收异常：{e}", "err")
                continue

            msg = P.loads(data)
            if not msg:
                continue
            mtype = msg.get("type")

            if mtype == P.MSG_ANNOUNCE:
                # 丢到子线程处理，避免下载大图时阻塞广播接收
                threading.Thread(
                    target=self._handle_announce, args=(msg, addr), daemon=True
                ).start()
            elif mtype == P.MSG_TOAST:
                # 同理：拉图片 + 起 PowerShell 都要时间，别卡住广播接收
                threading.Thread(
                    target=self._handle_toast, args=(msg, addr), daemon=True
                ).start()
            elif mtype == P.MSG_SETNAME:
                # 改「通知上显示的应用名」：要写配置 + 重建开始菜单快捷方式，
                # 也别占着广播接收线程
                threading.Thread(
                    target=self._handle_setname, args=(msg, addr), daemon=True
                ).start()
            elif mtype == P.MSG_PING:
                # 控制端会把同一次扫描广播多遍（抵消丢包），这里只回应一次
                nonce = str(msg.get("nonce") or "")
                now = time.time()
                with self._lock:
                    for k in [k for k, t in self._seen_ping.items() if now - t > 300]:
                        self._seen_ping.pop(k, None)
                    if nonce and nonce in self._seen_ping:
                        continue
                    if nonce:
                        self._seen_ping[nonce] = now
                self._reply_pong(msg, addr)

    def _reply_pong(self, msg: dict, addr) -> None:
        """回应控制端的在线扫描。"""
        out = P.make(
            P.MSG_PONG,
            nonce=msg.get("nonce"),
            host=socket.gethostname(),
            user=os.environ.get("USERNAME", ""),
            applied=os.path.basename(self.last_wallpaper or ""),
            count=self.applied_count,
            port=self.udp_port,
            ts=time.time(),
        )
        # 优先发到控制端专门收单播的回执端口，收不到再退回源端口
        port = int(msg.get("reply_port") or addr[1])
        try:
            self._sock.sendto(P.dumps(out), (addr[0], port))
            self.log(f"已回应在线扫描（{addr[0]}）", "dim")
        except OSError as e:
            self.log(f"回应扫描失败：{e}", "err")

    # ---------------------------------------------------------- 处理公告

    def _handle_announce(self, msg: dict, addr) -> None:
        task = str(msg.get("task") or "")
        if not task:
            return
        ip = addr[0]
        reply_port = int(msg.get("reply_port") or addr[1])
        now = time.time()

        with self._lock:
            # 清理 15 分钟前的去重记录
            for k in [k for k, t in self._seen.items() if now - t > 900]:
                self._seen.pop(k, None)
            if task in self._seen:
                return          # 同一条广播我们发了 3 遍，只处理一次
            self._seen[task] = now

        name = str(msg.get("name") or f"{task}.jpg")
        style = msg.get("style") or self.style
        tcp_port = int(msg.get("tcp_port") or P.TCP_PORT)
        sha_expect = msg.get("sha256") or ""
        size = int(msg.get("size") or 0)

        self.rx_count += 1
        self.last_task = task
        self.on_event("announce", {"ip": ip, "name": name, "size": size})
        self.log(f"收到广播：{name}（来自 {ip}，约 {size // 1024} KB）", "warn")

        last_err = ""
        for attempt in range(1, self.retry + 1):
            # 重试只包住「下载 + 换壁纸」这一段：只有这一步失败才值得重来。
            # 换好之后的记账动作（写状态文件、回报控制端）如果在 try 里面，
            # 一旦它们抛个异常，就会被当成「这次没换成」再下载一遍 ——
            # 结果是同一个任务把壁纸换了两次、图片白下载一次。
            try:
                data = self._download(ip, tcp_port, task, sha_expect, size)
                path = self._save(data, name, task)
                effective = wallpaper.set_wallpaper(path, style)
            except Exception as e:
                last_err = str(e)
                self.log(f"第 {attempt} 次尝试失败：{last_err}", "err")
                if attempt < self.retry:
                    time.sleep(0.8)
                continue

            self.last_wallpaper = effective
            self.applied_count += 1
            self.log(f"✅ 壁纸已更换：{os.path.basename(effective)}", "ok")
            self._write_state()
            self.on_event("applied", {"ip": ip, "path": effective, "name": name})
            self._ack(ip, reply_port, task, True, "")
            return

        # 全部失败：撤销去重记录，这样控制端重发时还能再试
        with self._lock:
            self._seen.pop(task, None)
        self.log("❌ 本次壁纸未能应用，已回报控制端", "err")
        self.on_event("failed", {"ip": ip, "err": last_err})
        self._ack(ip, reply_port, task, False, last_err)

    # ---------------------------------------------------------- 通知（Toast）

    def _ensure_toast_module_async(self) -> None:
        """后台补装 BurntToast。

        客户机可能只是"拷过去跑"（没执行过 --install），或者当时装失败了 ——
        那就一直没有这个模块，控制端发通知时报「未能加载指定模块 BurntToast」。
        这里在启动几秒后自己查一次、自己装，装不了就把怎么办写进日志。
        """
        if not self.toast_enabled:
            return

        def work():
            if self._stop.wait(8.0):        # 先让程序把该打的日志打完
                return
            if toastmod.find_module_dir(self.toast_module_path) or \
                    toastmod.module_installed(self.toast_module_path):
                return
            ok, msg = toastmod.ensure_modules(self.toast_module_path,
                                              log=self.log,
                                              auto_install=self.toast_auto_install)
            if ok:
                self.log("通知组件已就绪，可以接收通知了", "ok")
            else:
                for line in msg.splitlines():
                    self.log(line, "warn")

        threading.Thread(target=work, name="toast-module", daemon=True).start()

    def _toast_asset_dir(self, task: str) -> str:
        """本次通知的图片落地目录。"""
        return os.path.join(N.local_app_data_dir(APP_DIR_NAME), "toast_assets", task)

    def _prune_toast_assets(self, keep: int = 5) -> None:
        """只留最近几次通知的图片，别把磁盘堆满。"""
        root = os.path.join(N.local_app_data_dir(APP_DIR_NAME), "toast_assets")
        try:
            dirs = [os.path.join(root, d) for d in os.listdir(root)]
            dirs = [d for d in dirs if os.path.isdir(d)]
            dirs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for old in dirs[keep:]:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass

    def _download_asset(self, ip: str, port: int, task: str, name: str,
                        sha_expect: str, size: int) -> str:
        """从控制端拉一张通知用的图片，返回落地路径。"""
        conn = socket.create_connection((ip, port), timeout=15)
        try:
            conn.settimeout(30)
            N.send_msg(conn, P.make(P.MSG_GET, task=task, host=socket.gethostname(),
                                    **{P.FIELD_ASSET: name}))
            hdr = P.loads(N.recv_line(conn))
            if not hdr:
                raise RuntimeError("控制端返回了无法识别的响应")
            if not hdr.get("ok"):
                raise RuntimeError(f"控制端拒绝：{hdr.get('err') or '未知原因'}")
            n = int(hdr.get("size") or size or 0)
            if n <= 0:
                raise RuntimeError("资源大小不合法")
            if n > TS.MAX_ASSET_LEN:
                raise RuntimeError(f"图片太大（{n // 1024} KB > "
                                   f"{TS.MAX_ASSET_LEN // 1024} KB）")
            data = N.recv_exact(conn, n)
        finally:
            try:
                conn.close()
            except OSError:
                pass

        import hashlib

        expect = sha_expect or hdr.get("sha256") or ""
        if expect and hashlib.sha256(data).hexdigest() != expect:
            raise RuntimeError("图片校验失败（sha256 不一致）")

        target_dir = self._toast_asset_dir(task)
        os.makedirs(target_dir, exist_ok=True)
        safe = os.path.basename(name)
        path = os.path.join(target_dir, safe)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path

    def _handle_setname(self, msg: dict, addr) -> None:
        """控制端要求改「通知上显示的应用名」（顶部那个名字）。

        这是个**持久化**改动：写进本机配置 + 重建 HKCU 里的 AppId 注册和开始菜单
        快捷方式，重启后依然生效。所以被控端这边：
          1. 独立校验名字（不信任网络来的内容，控制端可能是别人伪造的）；
          2. 允许用配置 `allow_remote_app_name=false` 一刀关掉；
          3. 无论成败都回执，并把"谁在什么时候改的"写进日志和状态文件。
        """
        task = str(msg.get("task") or "")
        if not task:
            return
        ip = addr[0]
        reply_port = int(msg.get("reply_port") or addr[1])

        with self._lock:
            now = time.time()
            for k in [t for k, t in self._seen_name.items() if now - t > 900]:
                self._seen_name.pop(k, None)
            if task in self._seen_name:
                return          # 同一条广播发多遍，只处理一次
            self._seen_name[task] = now

        if not self.cfg.get("allow_remote_app_name", True):
            self.log(f"控制端（{ip}）想改通知应用名，但本机配置里禁止了"
                     f"（allow_remote_app_name=false）", "warn")
            self._ack(ip, reply_port, task, False,
                      "本机禁止远程改通知应用名（allow_remote_app_name=false）")
            return

        okname, clean = P.normalize_app_name(msg.get(P.FIELD_APP_NAME))
        if not okname:
            self.log(f"控制端（{ip}）发来的应用名被拒绝：{clean}", "err")
            self._ack(ip, reply_port, task, False, f"应用名不合法：{clean}")
            return

        try:
            self.cfg["toast_app_name"] = clean
            write_path = self.config_write_path or self.config_path
            if write_path:
                N.save_json(write_path, {k: v for k, v in self.cfg.items()
                                         if not k.startswith("_")})
            applied = toastmod.set_app_display_name(clean)
            ok_id, msg_id = toastmod.ensure_app_id(log=self.log)
        except Exception as e:                       # 写配置/注册失败也要如实回报
            self.log(f"改通知应用名失败：{e}", "err")
            self._ack(ip, reply_port, task, False, f"改名字失败：{e}")
            return

        if not ok_id:
            self._ack(ip, reply_port, task, False, f"改名字失败：{msg_id}")
            return

        self._remote_name = {"name": applied, "from": ip, "at": time.time()}
        self.log(f"控制端（{ip}）把通知应用名改成了「{applied}」"
                 f"（已写进本机配置，重启后仍然生效）", "warn")
        self._write_state()
        self.on_event("setname", {"ip": ip, "name": applied})
        self._ack(ip, reply_port, task, True, f"通知应用名已改成「{applied}」")

    def _handle_toast(self, msg: dict, addr) -> None:
        """收到一条通知推送：校验规格 → 拉图片 → 调 BurntToast 弹出来 → 回报。"""
        task = str(msg.get("task") or "")
        if not task:
            return
        ip = addr[0]
        reply_port = int(msg.get("reply_port") or addr[1])

        with self._lock:
            now = time.time()
            for k in [k for k, t in self._seen.items() if now - t > 900]:
                self._seen.pop(k, None)
            if task in self._seen:
                return          # 同一条广播发了 3 遍，只处理一次
            self._seen[task] = now

        if not self.toast_enabled:
            self.log("收到通知推送，但本机配置里关掉了通知（toast_enabled=false）", "warn")
            self._ack(ip, reply_port, task, False, "本机已关闭通知功能")
            return

        # 网络来的规格一律先校验：控制端可能是别人伪造的
        ok, spec, err = TS.normalize(msg.get("spec") or {})
        if not ok:
            self.log(f"通知规格被拒绝：{err}", "err")
            self._ack(ip, reply_port, task, False, f"规格不合法：{err}")
            return

        # 节流：别让谁刷屏
        with self._toast_lock:
            gap = time.time() - self._last_toast_at
            if self.toast_min_interval and gap < self.toast_min_interval:
                wait = self.toast_min_interval - gap
            else:
                wait = 0.0
            self._last_toast_at = time.time() + wait
        if wait:
            time.sleep(min(wait, 5.0))

        tcp_port = int(msg.get("tcp_port") or P.TCP_PORT)
        wanted = TS.asset_names(spec)
        self.log(f"收到通知：{TS.summarize(spec)}（来自 {ip}）", "warn")

        # 拉图片，并把手里的资源名换成落地路径（BurntToast 认本地路径）
        local_paths: dict[str, str] = {}
        for asset in msg.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            if name not in wanted:
                continue
            try:
                path = self._download_asset(ip, tcp_port, task, name,
                                            str(asset.get("sha256") or ""),
                                            int(asset.get("size") or 0))
                local_paths[name] = path
            except Exception as e:
                self.log(f"图片「{name}」拉取失败：{e}", "err")

        for key in ("app_logo", "hero_image"):
            name = spec.get(key) or ""
            if not name:
                continue
            if name in local_paths:
                spec[key] = local_paths[name]
            else:
                # 图没拉下来：去掉它也比整条通知失败强
                self.log(f"图片「{name}」没有拿到，这条通知不带{key}", "warn")
                spec[key] = ""

        self._prune_toast_assets()
        self.on_event("toast", {"ip": ip, "title": (spec.get("text") or [""])[0]})

        # 渲染前再确认一次模块在不在：客户机上很可能压根没装过
        ready, why = toastmod.ensure_modules(self.toast_module_path, log=self.log,
                                            auto_install=self.toast_auto_install)
        if not ready:
            self.log("❌ 本机缺少 BurntToast 模块，这条通知发不出去", "err")
            for line in why.splitlines():
                self.log("   " + line, "warn")
            with self._lock:
                self._seen.pop(task, None)
            self._ack(ip, reply_port, task, False, why)
            return

        render_spec = dict(spec)
        render_spec["unique_id"] = spec.get("unique_id") or f"wpp-{task}"
        ok2, msg2 = toastmod.render(render_spec, self.toast_module_path)
        if ok2:
            self.toast_count = getattr(self, "toast_count", 0) + 1
            self.log(f"✅ 通知已弹出：{TS.summarize(spec)}", "ok")
            self._write_state()
            self._ack(ip, reply_port, task, True, "")
            return

        self.log(f"❌ 通知弹出失败：{msg2}", "err")
        # 失败就不要去重记录，控制端重发时还能再试
        with self._lock:
            self._seen.pop(task, None)
        self._ack(ip, reply_port, task, False, msg2)

    # ---------------------------------------------------------- 下载

    def _download(self, ip: str, port: int, task: str, sha_expect: str, size: int) -> bytes:
        """TCP 回连控制端拉取图片，并校验完整性。"""
        conn = socket.create_connection((ip, port), timeout=15)
        try:
            conn.settimeout(30)
            N.send_msg(conn, P.make(P.MSG_GET, task=task, host=socket.gethostname()))

            raw = N.recv_line(conn)
            hdr = P.loads(raw)
            if not hdr:
                raise RuntimeError("控制端返回了无法识别的响应")
            if not hdr.get("ok"):
                raise RuntimeError(f"控制端拒绝：{hdr.get('err') or '未知原因'}")

            n = int(hdr.get("size") or size or 0)
            if n <= 0:
                raise RuntimeError("控制端未提供有效的文件大小")

            data = N.recv_exact(conn, n)
        finally:
            try:
                conn.close()
            except OSError:
                pass

        digest = hashlib.sha256(data).hexdigest()
        expect = sha_expect or hdr.get("sha256") or ""
        if expect and digest != expect:
            raise RuntimeError("文件校验失败（sha256 不一致，图片可能损坏）")
        return data

    # ---------------------------------------------------------- 落盘 / 清理

    def _save(self, data: bytes, name: str, task: str) -> str:
        os.makedirs(self.wallpaper_dir, exist_ok=True)
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif"):
            ext = ".jpg"
        # 文件名带上 task_id：路径变了 Windows 才会真正重绘桌面
        target = os.path.join(self.wallpaper_dir, f"wall_{task}{ext}")
        tmp = target + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
        self._cleanup()
        return target

    def _cleanup(self) -> None:
        """只保留最近 keep 张壁纸，避免长期运行把磁盘塞满。"""
        try:
            files = [
                os.path.join(self.wallpaper_dir, f)
                for f in os.listdir(self.wallpaper_dir)
                if f.startswith("wall_") and not f.endswith(".part")
            ]
            files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for old in files[self.keep:]:
                if os.path.abspath(old) == os.path.abspath(self.last_wallpaper or ""):
                    continue
                try:
                    os.remove(old)
                except OSError:
                    pass
        except OSError:
            pass

    # ---------------------------------------------------------- 回报

    def _ack(self, ip: str, port: int, task: str, ok: bool, err: str) -> None:
        out = P.make(
            P.MSG_APPLIED,
            task=task,
            ok=bool(ok),
            err=err,
            host=socket.gethostname(),
            user=os.environ.get("USERNAME", ""),
            ts=time.time(),
        )
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.sendto(P.dumps(out), (ip, int(port)))
            finally:
                s.close()
        except OSError:
            pass  # 控制端可能已经关了，回报失败不算错误


# ================================================================ 界面

def run_gui(core: AgentCore, hidden: bool = False, logger=None) -> int:
    """带界面运行。

    hidden=True 时窗口先藏起来（开机自启的静默模式），但进程仍然活着：
    再运行一次 exe 或点开始菜单的「显示窗口」，就会把这个窗口叫出来。

    面板是受保护的：**显示之前必须先过密码**（没设过密码就先让你设置）。
    密码检查放在这个进程里，而不是放在 `--show` 命令里 —— 这样谁也别想
    绕开命令行直接把窗口弄出来。
    """
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    import ui

    root = tk.Tk()
    root.title(f"{APP_NAME}  v{APP_VER}")
    ui.apply_theme(root)
    ui.center(root, 800, 660)
    root.minsize(700, 540)
    root.withdraw()            # 先把窗口藏起来，验过密码再显示

    log_q: queue.Queue[tuple[str, str]] = queue.Queue()

    def emit(msg, level="info"):
        log_q.put((msg, level))
        if logger:
            logger(msg, level)

    core.log = emit

    # ---------------- 顶部标题
    head = ttk.Frame(root, padding=(20, 16, 20, 8))
    head.pack(fill="x")
    ttk.Label(head, text="🖼  壁纸接收端", style="Title.TLabel").pack(side="left")
    state = ttk.Label(head, text="  ● 运行中  ", style="Card.TLabel")
    state.pack(side="right", ipady=4, ipadx=6)

    ttk.Label(
        root,
        text="等待控制端广播壁纸 · 收到后自动更换桌面背景 · 上线时会主动向控制端报到",
        style="Sub.TLabel",
    ).pack(anchor="w", padx=20, pady=(0, 10))

    # ---------------- 信息卡片
    card = ttk.Frame(root, style="Card.TFrame", padding=16)
    card.pack(fill="x", padx=20)
    card.columnconfigure(1, weight=1)

    info_labels = {}

    def add_row(row: int, key: str, title: str, value: str = "—",
                wrap: int = 0):
        ttk.Label(card, text=title, style="Card.TLabel", foreground=ui.FG_DIM).grid(
            row=row, column=0, sticky="w", pady=3, padx=(0, 14)
        )
        # wrap>0 时超长文字会自动折行，而不是被格子切掉半截
        lb = ttk.Label(card, text=value, style="Card.TLabel",
                       wraplength=wrap or 0, justify="left")
        lb.grid(row=row, column=1, sticky="w", pady=3)
        info_labels[key] = lb
        return lb

    add_row(0, "port", "监听端口", f"UDP {core.udp_port}")
    add_row(1, "count", "已更换次数", "0")
    add_row(2, "wallpaper", "当前壁纸", os.path.basename(core.last_wallpaper or "") or "（未设置）")
    add_row(3, "dir", "壁纸目录", core.wallpaper_dir)
    add_row(4, "cfg", "配置文件", core.config_path or "（默认）")
    add_row(5, "nic", "本机网卡", N.adapter_summary() or "未检测到可用网卡")
    add_row(6, "hello", "主动报到",
            f"每 {int(core.hello_interval)} 秒一次" if core.hello_interval
            else "已关闭（只能等控制端扫描）")
    add_row(7, "log", "日志文件", core.log_path or "（未写文件）")

    # 开机自启：界面上直接勾一下即可，不用再去跑批处理。
    # MSI 装的机器上自启项在 HKLM（由安装包管理），这时不给勾选，免得
    # 用户点了取消却看起来没反应（HKLM 那项不是我们该动的）。
    auto_entries = autostart_entries()
    managed_by_msi = any(e["hive"] == "HKLM" for e in auto_entries)
    autostart_var = tk.BooleanVar(value=bool(auto_entries))
    if managed_by_msi:
        add_row(8, "autostart", "开机自启",
                "由 MSI 安装包管理（HKLM Run，卸载 MSI 即取消）")
    else:
        add_row(8, "autostart", "开机自启", "")

        def toggle_autostart():
            if autostart_var.get():
                ok, msg = enable_autostart()
            else:
                ok, msg = disable_autostart()
            log_q.put((msg, "ok" if ok else "err"))
            autostart_var.set(bool(autostart_entries()))

        ui.checkbutton(card, "登录时自动运行（静默后台，随时可取消）",
                       autostart_var, toggle_autostart).grid(
            row=8, column=1, sticky="w", pady=1)

    # 防火墙：收不到控制端广播时，这里一眼能看出来并且能一键修
    fw_state = firewall_rule_ok()
    add_row(9, "firewall", "防火墙", {
        True: "已放行入站 UDP 38571",
        False: "【未放行 —— 收不到控制端的推送】",
        None: "状态未知（netsh 查询失败）",
    }[fw_state])

    def fix_firewall():
        ok, msg = add_firewall_rule()
        log_q.put((msg, "ok" if ok else "err"))
        info_labels["firewall"].configure(text=msg)

    if fw_state is not True:
        ttk.Button(card, text="放行…", command=fix_firewall).grid(
            row=9, column=2, sticky="w", padx=8)

    # 面板口令状态
    _blob, _pw_source = agentauth.load_blob(core.cfg)
    add_row(10, "password", "面板密码",
            f"已设置（来源：{_pw_source}）" if _blob
            else "未设置 —— 任何本地用户都能打开面板/停止/卸载")
    ttk.Button(card, text="修改密码…", command=lambda: change_password_dialog()).grid(
        row=10, column=2, sticky="w", padx=8)

    # 通知优先级（允许紧急通知）：第一次运行自动打开，这里随时能改回去 ——
    # 用户不该为了关掉一个通知开关去翻注册表。
    prio_var = tk.BooleanVar(value=toastmod.allow_urgent_state() == 1)
    add_row(11, "priority", "通知优先级", toastmod.notification_priority_state(),
            wrap=470)

    def toggle_priority():
        ok, msg = toastmod.set_notification_priority(prio_var.get())
        log_q.put((msg, "ok" if ok else "err"))
        prio_var.set(toastmod.allow_urgent_state() == 1)
        info_labels["priority"].configure(
            text=toastmod.notification_priority_state())

    ui.checkbutton(card, "设为最高（允许紧急通知）", prio_var,
                   toggle_priority).grid(row=11, column=2, sticky="w", padx=8)

    # 通知上显示的应用名（默认「Win 壁纸推送」，可改成「IT 运维通知」之类）
    # 注意别和通知里的「署名」搞混：署名是每条消息底部那行小字（控制端填），
    # 这里是**通知顶部**那个名字（每台机器各自）。
    def current_app_name() -> str:
        return toastmod.set_app_display_name(
            str(core.cfg.get("toast_app_name") or ""))

    add_row(12, "appname", "通知应用名",
            f"顶部显示：{current_app_name()}", wrap=470)

    def rename_notice():
        from tkinter import simpledialog

        new_name = simpledialog.askstring(
            "通知顶部显示什么名字",
            "通知**顶部**显示的就是这个名字（现在：「"
            + current_app_name() + "」）。\n"
            "比如改成「IT 运维通知」。留空 = 恢复默认「Win 壁纸推送」。\n\n"
            "和通知底部那行小字（署名）不是一回事 —— 署名是控制端发消息时\n"
            "填的，跟着每条消息走；这里是本机固定的。",
            initialvalue=current_app_name(), parent=root)
        if new_name is None:
            return
        clean = " ".join(new_name.split())[:toastmod.MAX_APP_NAME_LEN]
        core.cfg["toast_app_name"] = clean
        N.save_json(core.config_write_path or core.config_path or "",
                    {k: v for k, v in core.cfg.items() if not k.startswith("_")})
        applied = toastmod.set_app_display_name(clean)
        ok, msg = toastmod.ensure_app_id()
        log_q.put((msg, "ok" if ok else "err"))
        info_labels["appname"].configure(text=f"顶部显示：{applied}")

    ttk.Button(card, text="改名…", command=rename_notice).grid(
        row=12, column=2, sticky="w", padx=8)

    # ---------------- 按钮
    bar = ttk.Frame(root, padding=(20, 14, 20, 4))
    bar.pack(fill="x")

    def do_local_test():
        p = filedialog.askopenfilename(
            title="选择一张图片立即设为壁纸（本地测试）",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp *.gif"), ("所有文件", "*.*")],
        )
        if not p:
            return
        try:
            eff = wallpaper.set_wallpaper(p, core.style)
            core.last_wallpaper = eff
            log_q.put((f"本地测试成功：{eff}", "ok"))
            refresh()
        except Exception as e:
            log_q.put((f"本地测试失败：{e}", "err"))

    def open_dir():
        try:
            os.startfile(core.wallpaper_dir)  # type: ignore[attr-defined]
        except Exception as e:
            log_q.put((f"打开目录失败：{e}", "err"))

    def open_log():
        target = core.log_path or default_log_path()
        try:
            if not os.path.isfile(target):
                log_q.put((f"日志文件还不存在：{target}", "warn"))
                return
            os.startfile(target)  # type: ignore[attr-defined]
        except Exception as e:
            log_q.put((f"打开日志失败：{e}", "err"))

    ttk.Button(bar, text="本地图片测试", command=do_local_test).pack(side="left")
    ttk.Button(bar, text="打开壁纸目录", command=open_dir).pack(side="left", padx=8)
    ttk.Button(bar, text="打开日志", command=open_log).pack(side="left")
    ttk.Button(bar, text="清空日志", command=lambda: log_pane.clear()).pack(side="left", padx=8)
    ttk.Button(bar, text="退出（停止接收）", command=lambda: on_close()).pack(side="right")
    ttk.Button(bar, text="隐藏到后台", command=lambda: hide_window()).pack(side="right", padx=8)

    # ---------------- 日志
    ttk.Label(root, text="运行日志", style="Sub.TLabel").pack(anchor="w", padx=20, pady=(12, 4))
    log_pane = ui.LogPane(root, height=12)
    log_pane.pack(fill="both", expand=True, padx=20)

    ttk.Label(
        root,
        text=(f"提示：再运行一次本程序（或开始菜单里的「显示窗口」）就能把这个窗口叫出来；"
              f"停止运行用 --stop     版本 {APP_VER}"),
        style="Sub.TLabel",
    ).pack(anchor="w", padx=20, pady=(6, 14))

    # ---------------- 刷新 / 轮询
    def refresh():
        info_labels["count"].configure(text=str(core.applied_count))
        info_labels["wallpaper"].configure(
            text=os.path.basename(core.last_wallpaper or "") or "（未设置）"
        )
        state.configure(text="  ● 运行中  ", foreground=ui.OK)

    def hide_window(*_a):
        """藏到后台继续运行（不占任务栏）。"""
        try:
            root.withdraw()
            emit("窗口已隐藏到后台，仍在正常接收壁纸推送", "dim")
        except Exception:
            pass

    def unlock_panel(target_hidden: bool) -> bool:
        """面板闸门：没设过密码就先设置（设不上也放行，并明确提醒），设过就验密码。

        策略都在 agentauth.unlock_panel_policy 里（那边有单测），这里只负责
        把结果变成界面上的提示。
        """
        allow, msg = agentauth.unlock_panel_policy(core.cfg, prefer="gui")
        emit(msg, "ok" if allow else "warn")
        if not allow:
            _panel_refused(
                "密码不正确或已取消，面板不会打开。\n\n"
                "被控端仍在后台正常运行（继续接收壁纸推送）。\n\n"
                "忘了密码只能清掉重设：删掉\n"
                "  C:\\ProgramData\\WinWallpaperPush\\secure\\agent_password.json\n"
                "  配置文件里的 password 段\n"
                "  注册表 HKCU\\...\\WinWallpaperPush\\Agent\\PasswordHash\n"
                "三处，再运行 WallpaperAgent.exe --set-password")
            return False
        if not agentauth.is_set(core.cfg):
            # 放行是因为"还没有密码可验"，不是"密码对了" —— 必须让用户知道，
            # 否则他会以为闸门坏了。
            _popup_warn("还没有设置面板密码",
                        "面板现在没有密码保护：任何本地用户都能打开面板、"
                        "停止或卸载被控端。\n\n"
                        "建议马上设置：「修改密码…」按钮，或命令行\n"
                        "WallpaperAgent.exe --set-password")
        return True

    def _panel_refused(text: str) -> None:
        """面板被拒绝打开时的提示。

        自动化/测试场景（设了 WPP_PROMPT_TIMEOUT）只写日志，不弹模态框 ——
        否则没人点「确定」，调用方就永远等下去了。
        """
        _popup_warn("被控端面板受保护", text, first_line_only=True)

    def _popup_warn(title: str, text: str, first_line_only: bool = False) -> None:
        if agentauth.automated():
            emit("（自动模式：跳过弹窗提示）"
                 + (text.splitlines()[0] if first_line_only else text.splitlines()[0]),
                 "warn")
            return
        # 父窗口可能是隐藏状态（静默后台）：给它当 owner 的窗口有时不会显示出来，
        # 那就别挂 owner，保证这个提示一定看得见。
        try:
            parent = root if root.winfo_viewable() else None
        except Exception:
            parent = None
        try:
            messagebox.showwarning(title, text, parent=parent)
        except Exception:
            pass

    def show_window(*_a):
        """把窗口显示出来 —— 必须先过密码。"""
        if not unlock_panel(False):
            return
        try:
            root.deiconify()
            root.lift()
            root.focus_force()
        except Exception:
            pass

    def change_password_dialog():
        ok, msg = agentauth.change_password(core.cfg, write_config=None, prefer="gui")
        emit(msg, "ok" if ok else "warn")

    # 关闭时先撤销排队中的定时回调，再销毁窗口。
    # 否则 root.after 排的回调会在解释器销毁后触发，抛
    # `invalid command name "...poll"` 这种 Tcl 报错。
    closing = {"done": False}
    after_id = {"id": None}

    def on_close():
        if closing["done"]:
            return
        closing["done"] = True
        if after_id["id"]:
            try:
                root.after_cancel(after_id["id"])
            except Exception:
                pass
        core.stop()
        try:
            root.destroy()
        except Exception:
            pass

    def poll():
        if closing["done"]:
            return
        drained = 0
        while drained < 200:
            try:
                msg, level = log_q.get_nowait()
            except queue.Empty:
                break
            log_pane.write(msg, level)
            drained += 1
        if drained:
            refresh()
        # 控制通道：--stop 让它退出，--show（或再点一次程序图标）把窗口叫出来
        if core.signals:
            for sig in core.signals.take():
                if sig == "stop":
                    emit("收到停止指令，正在退出…", "warn")
                    on_close()
                    return
                if sig == "show":
                    show_window()
        after_id["id"] = root.after(150, poll)

    core.start()
    # 按内容实际需要的高度定窗口，保证底部的日志面板和提示不会被挤掉
    ui.fit(root, 800, 660)
    log_pane.write("已就绪，等待控制端广播壁纸…", "ok")
    if hidden:
        root.withdraw()
        log_pane.write("（静默模式：窗口已隐藏；再运行一次本程序即可叫出窗口，"
                       "叫出时需要输入面板密码）", "dim")
    else:
        # 面板受保护：先过密码（第一次运行会让你设置），过了才显示
        show_window()
    if not auto_entries:
        log_pane.write("提示：本机还没设置开机自启 —— 勾选上面的「登录时自动运行」，"
                       "或在命令行跑 WallpaperAgent.exe --install（会顺带放行防火墙）",
                       "warn")
    elif managed_by_msi:
        log_pane.write("开机自启由 MSI 安装包配置（HKLM Run），无需手动设置", "dim")
    if fw_state is False:
        log_pane.write("警告：防火墙没有放行 UDP 38571，控制端的广播进不来 —— "
                       "点「防火墙」那一行的「放行…」按钮修一下", "warn")
    if not _blob:
        log_pane.write("警告：还没设置面板密码 —— 任何本地用户都能打开面板、停止或卸载它。"
                       "点「修改密码…」或在命令行跑 WallpaperAgent.exe --set-password",
                       "warn")
    root.protocol("WM_DELETE_WINDOW", on_close)
    if hidden:
        root.withdraw()
        log_pane.write("（静默模式：窗口已隐藏，再运行一次本程序即可叫出窗口）", "dim")
    poll()
    root.mainloop()
    core.stop()
    return 0


def run_headless(core: AgentCore) -> int:
    """纯后台模式（连 Tk 都起不来时的兜底）。

    注意：这个模式下没有窗口可以叫出来，只能靠 `--stop` 或任务管理器结束。
    正常情况（包括 MSI 静默安装后的自启）走的是 run_gui(hidden=True)，
    窗口只是藏起来，随时能叫出来。
    """
    def log(msg, level="info"):
        try:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
        except Exception:
            pass

    core.log = log
    core.start()
    log("已进入后台静默模式（无窗口）。停止：WallpaperAgent.exe --stop")
    try:
        while not core._stop.is_set():
            if core.signals and "stop" in core.signals.wait(1.0):
                log("收到停止指令，正在退出…")
                break
            if not core.signals:
                core._stop.wait(1.0)
    except KeyboardInterrupt:
        log("收到退出信号")
    finally:
        core.stop()
    return 0


# ================================================================ 控制命令
#
# 被控端常见的部署方式是「开机自启 + 静默后台」：没窗口、没托盘图标。
# 下面这几个命令就是给这种场景准备的「遥控器」，实现见 winipc.py。

def cmd_status() -> int:
    """看一眼到底有没有在跑、端口 / 配置 / 日志都在哪。"""
    running = winipc.instance_running()
    st = N.load_json(state_file_path())
    print(f"{APP_NAME} v{APP_VER}")
    print("运行状态:", "正在运行" if running else "未运行")
    if st:
        started = float(st.get("started") or 0)
        when = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
                if started else "?")
        uptime = f"（已运行 {int(time.time() - started)} 秒）" if running and started else ""
        print("进程号  :", st.get("pid", "?"))
        print("启动时间:", when, uptime)
        print("监听端口: UDP", st.get("udp_port", "?"))
        print("已更换  :", st.get("applied", 0), "次，最近一张:",
              st.get("last_wallpaper") or "（还没换过）")
        print("配置文件:", st.get("config") or "（默认）")
        print("日志文件:", st.get("log") or "（未写文件）")
        print("壁纸目录:", st.get("wallpaper_dir") or default_wallpaper_dir())
        if st.get("silent"):
            print("运行方式: 静默后台（窗口已隐藏）")
        if not running:
            print("注意    : 状态文件是上次运行留下的 —— 上次不是正常退出"
                  "（被强杀 / 断电 / 崩溃），本机当前没有被控端在跑。")
    else:
        print("没有状态文件：本机从未启动过被控端，或状态目录被清理了。")
        print("日志文件:", default_log_path(),
              "（存在）" if os.path.isfile(default_log_path()) else "（不存在）")
    # 开机自启状态：重启后「没自启」时，这一块直接给出原因
    print()
    for line in describe_autostart():
        print(line)
    # 面板口令状态
    read_path, _write_path = resolve_config("")
    _cfg = dict(DEFAULT_CFG)
    _cfg.update(N.load_json(read_path))
    blob, source = agentauth.load_blob(_cfg)
    print()
    if blob:
        print(f"面板密码：已设置（来源：{source}）—— 打开面板 / 停止 / 卸载都要输入")
    else:
        print("面板密码：未设置 —— 任何本地用户都能打开面板、停止或卸载它")
        print("          用 WallpaperAgent.exe --set-password 设置")
    # 通知能力（缺 BurntToast 时通知发不出去，这里一眼能看出来）
    toast_state = toastmod.self_test(str(_cfg.get("toast_module_path") or ""))
    print(toast_state.replace("**", ""))
    toastmod.set_app_display_name(str(_cfg.get("toast_app_name") or ""))
    print(toastmod.app_id_state())
    print(toastmod.notification_priority_state())
    if not running:
        print()
        print("启动：直接双击 WallpaperAgent.exe，或运行  WallpaperAgent.exe --silent")
    return 0 if running else 1


def cmd_stop(cfg: dict | None = None, require_password: bool = True) -> int:
    """让正在运行的实例退出（用户最需要的那条命令）。

    设置过密码的话，这里要先验密码 —— 「关掉它」正是要被保护的动作。
    """
    if require_password:
        ok, msg = agentauth.require(cfg or {}, "停止被控端", prefer="auto")
        print(msg)
        if not ok:
            return 1
    if not winipc.instance_running():
        print("没有正在运行的被控端，无需停止。")
        return 0
    winipc.signal("stop")
    if winipc.wait_gone(8.0):
        print("已停止。开机自启项还在，下次登录会重新启动；")
        print("要彻底关掉自启请用 disable_autostart.bat 或卸载 MSI。")
        return 0
    print("已发出停止信号，但进程还没退出（界面可能正卡在别的事情上）。")
    print("可以强制结束：taskkill /IM WallpaperAgent.exe /F /T")
    return 1


def cmd_show(cfg: dict | None = None) -> int:
    """把正在运行的实例的窗口叫出来。

    这里**故意不验密码**：真正弹面板的是已经在跑的那个实例，由它自己把关
    （见 run_gui 里的 unlock_panel）。这样无论谁绕过本命令直接发信号，
    面板都不会被打开。
    """
    if not winipc.instance_running():
        return -1                       # 交给调用方按「正常启动」处理
    if winipc.signal("show"):
        print("已请求显示面板 —— 会先要求输入密码（没设过密码的话会先让你设置）。")
        return 0
    print("叫窗口失败：控制通道打不开（可能是权限或会话不同）。")
    print("可以先用 --stop 停掉，再重新启动。")
    return 1


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
        prog="agent",
        description=f"{APP_NAME} v{APP_VER} —— 接收广播并更换 Windows 桌面壁纸",
    )
    ap.add_argument("--silent", action="store_true",
                    help="静默后台运行（窗口藏起来，之后随时能叫出来）")
    ap.add_argument("--install", action="store_true",
                    help="便携版一键装好：写开机自启 + 放行防火墙 + 立即静默启动")
    ap.add_argument("--task", action="store_true",
                    help="配合 --install：改用「登录计划任务」而不是 Run 项（需管理员）")
    ap.add_argument("--no-start", action="store_true",
                    help="配合 --install：只设置自启，不立刻启动")
    ap.add_argument("--uninstall", action="store_true",
                    help="取消开机自启并停掉正在运行的实例（不删程序文件）")
    ap.add_argument("--stop", action="store_true", help="让正在运行的被控端退出")
    ap.add_argument("--status", action="store_true",
                    help="打印运行状态（端口 / 配置 / 日志 / 自启），在跑返回 0，没跑返回 1")
    ap.add_argument("--show", action="store_true", help="把正在运行的被控端窗口叫出来")
    ap.add_argument("--set-password", dest="set_password", action="store_true",
                    help="设置 / 修改被控端面板密码（需要旧密码）")
    ap.add_argument("--clear", action="store_true",
                    help="配合 --set-password：取消密码保护（需要旧密码）")
    ap.add_argument("--install-toast-module", dest="install_toast",
                    action="store_true",
                    help="安装通知组件 BurntToast（客户端首次安装会自动做）")
    ap.add_argument("--source", metavar="目录",
                    help="配合 --install-toast-module：从本地目录离线安装")
    ap.add_argument("--test-toast", dest="test_toast", action="store_true",
                    help="在本机弹一条测试通知，检查通知功能是否正常")
    ap.add_argument("--duration", metavar="short|long|until_dismissed",
                    help="配合 --test-toast：横幅停留时长（短≈5 秒 / 长≈25 秒 / "
                         "一直显示到用户处理）；不填用配置里的默认值")
    ap.add_argument("--allow-urgent", dest="allow_urgent", metavar="on|off",
                    help="把本软件的通知优先级设为最高 / 关掉（写 "
                         "HKCU\\...\\Notifications\\Settings 下本软件的允许紧急通知）")
    ap.add_argument("--set-toast-app-name", dest="toast_app_name", metavar="名字",
                    help="改通知上显示的应用名（默认「Win 壁纸推送」，例如「IT 运维通知」）；"
                         "写进配置并立刻重新注册，填 default 恢复默认")
    ap.add_argument("--force", action="store_true",
                    help="忽略「已经在运行」，强制再启动一个实例")
    ap.add_argument("--port", type=int, help=f"覆盖 UDP 监听端口（默认 {P.UDP_PORT}）")
    ap.add_argument("--style", help=f"壁纸契合度：{P.style_help()}")
    ap.add_argument("--config", metavar="文件", help="指定配置文件路径（覆盖默认查找顺序）")
    ap.add_argument("--set", dest="setfile", metavar="图片", help="直接设置本地壁纸后退出")
    ap.add_argument("--selftest", action="store_true", help="打印环境自检信息后退出")
    args = ap.parse_args(argv)

    # 配置要早点读出来：停止 / 卸载 / 改密码都要拿它来验口令
    read_path, write_path = resolve_config(args.config or "")
    cfg = dict(DEFAULT_CFG)
    cfg.update(N.load_json(read_path))
    if args.port:
        cfg["udp_port"] = args.port
    if args.style:
        st = P.normalize_style(args.style)
        if not st:
            print(f"无法识别的契合度「{args.style}」。可选：{P.style_help()}",
                  file=sys.stderr)
            return 1
        cfg["style"] = st
    cfg["_config_read_path"] = read_path
    cfg["_config_write_path"] = write_path

    # ---------------- 控制 / 安装类命令：只做事，不启动自己
    if args.set_password:
        return cmd_set_password(cfg, write_path, clear=args.clear)
    if args.install_toast:
        return cmd_install_toast_module(args.source or "", write_path, cfg)
    if args.test_toast:
        return cmd_test_toast(cfg, args.duration or "")
    if args.allow_urgent:
        return cmd_allow_urgent(args.allow_urgent)
    if args.toast_app_name is not None:
        return cmd_set_toast_app_name(cfg, write_path, args.toast_app_name)
    if args.install:
        return cmd_install(cfg, write_path, use_task=args.task,
                           skip_start=args.no_start)
    if args.uninstall:
        return cmd_uninstall(cfg)
    if args.stop:
        return cmd_stop(cfg)
    if args.status:
        return cmd_status()

    if args.selftest:
        print(f"{APP_NAME} v{APP_VER}")
        print("Python :", sys.version.split()[0], f"({sys.executable})")
        print("程序目录:", N.app_dir())
        print("配置文件:", read_path, "（存在）" if os.path.isfile(read_path) else "（尚未创建）")
        if write_path != read_path:
            print("写入位置:", write_path)
        print("运行状态:", "正在运行" if winipc.instance_running() else "未运行")
        print("日志文件:", default_log_path())
        print("壁纸接口:", wallpaper.self_test())
        print("壁纸目录:", default_wallpaper_dir())
        toastmod.set_app_display_name(str(cfg.get("toast_app_name") or ""))
        print(toastmod.app_id_state())
        print(toastmod.notification_priority_state())
        print("本机网卡（有线 / 无线都会列出）：")
        for line in N.adapter_report():
            print(line)
        print("广播网段:", ", ".join(N.broadcast_targets()))
        for line in describe_autostart():
            print(line)
        return 0

    if args.setfile:
        try:
            print("已设置壁纸：", wallpaper.set_wallpaper(args.setfile, args.style))
            return 0
        except Exception as e:
            print("设置失败：", e, file=sys.stderr)
            return 1

    # ---------------- 窗口是显示还是藏起来
    #   1. 显式 --show   一定显示（用户就是在找窗口）
    #   2. 显式 --silent 一定隐藏（自启项用的就是这个，两种部署方式都会带）
    #   3. 都没写 → 看配置里的 silent
    #   例外：已经有实例在跑时，用户这次启动多半是想「找到它」，
    #   所以不带 --silent 就把窗口叫出来，而不是再藏一个。
    explicit_show = bool(args.show)
    if args.silent:
        hidden = True
    elif explicit_show:
        hidden = False
    else:
        hidden = bool(cfg.get("silent"))

    # ---------------- 单实例
    # 自启项（HKLM Run）、开始菜单快捷方式、用户双击，都可能把程序拉起来。
    # 同一台机器上跑两个被控端没有意义（广播会被处理两遍），所以已有实例时：
    #   想开界面（双击 / --show）→ 把它的窗口叫出来
    #   想静默启动（--silent）  → 安静退出，什么都不做
    if not args.force and winipc.instance_running():
        if hidden and not explicit_show:
            print("被控端已经在后台运行，这次不重复启动。")
            print("  看窗口：WallpaperAgent.exe --show（会要求输入面板密码）")
            print("  停运行：WallpaperAgent.exe --stop（也要密码）")
        else:
            cmd_show(cfg)
        return 0
    if explicit_show:
        hidden = False          # 没有实例在跑，--show 就等于正常启动

    # ---------------- 日志文件（静默后台时唯一的排查线索）
    log_path = ""
    logger = None
    want = cfg.get("log_file")
    if want is None or str(want).strip().lower() not in ("off", "none", "-", "false"):
        log_path = str(want).strip() if str(want or "").strip() else default_log_path()
        try:
            logger = N.FileLogger(log_path, max_kb=cfg.get("log_max_kb") or 512)
            logger.header(
                f"{APP_NAME} v{APP_VER} 启动 pid={os.getpid()} "
                f"{'（静默后台）' if hidden else '（带界面）'}"
            )
        except Exception as e:
            logger = None
            log_path = ""
            print(f"日志文件不可用（{e}），继续以无日志模式运行", file=sys.stderr)
    cfg["_log_path"] = log_path

    N.save_json(write_path, {k: v for k, v in cfg.items() if not k.startswith("_")})

    # ---------------- 控制通道
    signals = winipc.AgentSignals.create()
    if signals is None and not args.force:
        print("被控端已经在运行（控制通道已存在），这次不重复启动。")
        if logger:
            logger.close()
        return 0

    core = AgentCore(cfg)
    core.signals = signals
    core.silent = hidden

    try:
        # 静默模式也走界面代码：窗口只是被藏起来，这样用户之后还能把它叫出来
        # 点「退出」，而不是只能去任务管理器结束进程。
        return run_gui(core, hidden=hidden, logger=logger)
    except Exception as e:
        print(f"界面启动失败（{e}），自动切换为纯后台模式", file=sys.stderr)
        if logger:
            logger(f"界面启动失败（{e}），切换为纯后台模式", "warn")
        return run_headless(core)
    finally:
        if logger:
            logger("被控端退出", "warn")
            logger.close()


def _autostart_value() -> str:
    """读出一行「开机自启」摘要（保留给旧调用方）。"""
    entries = autostart_entries()
    if not entries:
        return ""
    e = entries[0]
    return f"{e['hive']}: {e['command']}"


# 开机自启相关的注册表位置
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_KEY_WOW = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
_APPROVED_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
                 r"\Explorer\StartupApproved\Run")
# 两个名字：MSI 写 HKLM 的 WinWallpaperPushAgent，免安装版写 HKCU 的 WinWallpaperAgent
AUTOSTART_NAMES = ("WinWallpaperPushAgent", "WinWallpaperAgent")
# 免安装版自己写自启时用的值名 / 计划任务名 / 防火墙规则名
RUN_VALUE_NAME = "WinWallpaperAgent"
LOGON_TASK_NAME = "WinWallpaperPushAgent"
FW_RULE_NAME = "WinWallpaperPush-Agent-UDP-38571"
FW_RULE_LEGACY = "WinWallpaperPush-UDP-Broadcast"


def _startup_approved_state(hive, name: str):
    """任务管理器「启动」里这一项是被启用还是被禁用。

    Windows 把用户对启动项的开关记在 StartupApproved\\Run 下的二进制值里：
    第一个字节 bit0 = 1 表示**已禁用**（0x02 → 启用，0x03 → 禁用）。
    没有这个值 = 从没被改过 = 启用。

    这一条很关键：**注册表里有自启项，不等于登录时真的会执行**。用户自己、
    或者某个"优化大师/安全卫士"，在任务管理器里关掉它之后，注册表值还在，
    Windows 却不会再运行它 —— 这正是「重启后没自启」最常见的原因之一。
    """
    if not wallpaper.IS_WINDOWS:
        return None
    import winreg

    for key_path in (_APPROVED_KEY, _APPROVED_KEY.replace(r"\Run", r"\Run32")):
        try:
            with winreg.OpenKey(hive, key_path) as k:
                data, _t = winreg.QueryValueEx(k, name)
        except OSError:
            continue
        if isinstance(data, (bytes, bytearray)) and data:
            return not (data[0] & 0x01)
        return None              # 有这个值但格式不认识
    # 没有记录 = 从来没有被禁用过（任务管理器第一次看到它时才写这个值）
    return True


def autostart_entries() -> list[dict]:
    """列出所有与本工具相关的开机自启项（HKLM + HKCU，64/32 位视图都看）。"""
    if not wallpaper.IS_WINDOWS:
        return []
    import winreg

    out: list[dict] = []
    for hive, where in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"),
                        (winreg.HKEY_CURRENT_USER, "HKCU")):
        for key_path, view in ((_RUN_KEY, "64"), (_RUN_KEY_WOW, "32")):
            try:
                with winreg.OpenKey(hive, key_path) as k:
                    for name in AUTOSTART_NAMES:
                        try:
                            value, _t = winreg.QueryValueEx(k, name)
                        except OSError:
                            continue
                        out.append({
                            "hive": where,
                            "view": view,
                            "name": name,
                            "command": str(value),
                            "enabled": _startup_approved_state(hive, name),
                        })
            except OSError:
                continue
    return out


def _command_target(command: str) -> str:
    """从 `"路径" 参数` 里取出可执行文件路径。"""
    text = (command or "").strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 1:
            return text[1:end]
    return text.split(" ")[0]


def describe_autostart(entries: list[dict] | None = None) -> list[str]:
    """把自启项整理成给人看（也给脚本看）的几行说明。

    纯函数（不读注册表），方便测试直接喂数据。
    """
    items = autostart_entries() if entries is None else entries
    if not items:
        return [
            "开机自启：未设置 —— 重启/重新登录后不会自己起来。",
            "          免安装版：WallpaperAgent.exe --install（或界面上勾「开机自动运行」）",
            "          MSI 版由安装包写 HKLM Run，不用手动设置。",
        ]
    lines = ["开机自启："]
    for e in items:
        state = {True: "已启用", False: "【已被禁用】", None: "状态未知"}[e.get("enabled")]
        target = _command_target(e.get("command", ""))
        exists = "目标存在" if os.path.isfile(target) else "【目标文件不存在】"
        lines.append(f"  {e['hive']} Run\\{e['name']}  [{state}，{exists}]")
        lines.append(f"      {e.get('command', '')}")
        if not os.path.isfile(target):
            lines.append("      → 程序被挪走或删了：把它放回原位置再跑 --install / 重装 MSI")
        if e.get("enabled") is False:
            lines.append("      → 到「任务管理器 → 启动」里启用它，或重新跑 --install")
    lines.append("  说明：自启项是在「用户登录时」执行，不是开机时 —— "
                 "桌面壁纸是每用户设置，没人登录就没有壁纸可改。")
    if all(e["hive"] == "HKCU" for e in items):
        lines.append("  注意：只有 HKCU 一项，它只对「写它的那个用户」登录时生效。")
    return lines


# ================================================================ 自安装
#
# 免安装版以前得再跑一个 enable_autostart.bat 才算装好（写自启 + 放行防火墙）。
# 现在这两件事 exe 自己就能做，一条命令或者界面上勾一下就行：
#
#     WallpaperAgent.exe --install      写自启 + 放行防火墙 + 立即静默启动
#     WallpaperAgent.exe --uninstall    停掉运行中的实例 + 删掉自启
#
# 防火墙那一步需要管理员：普通权限跑 --install 会弹一次 UAC 只为加规则，
# 被控端本身仍然以普通权限运行（提权运行的话，普通权限的 --stop 会打不开它的
# 控制通道，又会变成「关不掉」）。

def _self_argv(extra: list[str]) -> list[str]:
    """拼出「再启动一次自己」的命令行参数表（打包前后都对）。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, *extra]
    return [sys.executable, os.path.abspath(__file__), *extra]


def autostart_command(silent: bool = True) -> str:
    """开机自启要执行的那条命令行（引号已经加好）。"""
    argv = _self_argv(["--silent"] if silent else [])
    return " ".join(f'"{a}"' if " " in a else a for a in argv)


def enable_autostart(silent: bool = True) -> tuple[bool, str]:
    """写 HKCU 自启项（免安装版）。返回 (成功, 说明)。"""
    if not wallpaper.IS_WINDOWS:
        return False, "只有 Windows 支持开机自启"
    import winreg

    cmd = autostart_command(silent)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUN_VALUE_NAME, 0, winreg.REG_SZ, cmd)
    except OSError as e:
        return False, (f"写注册表失败：{e}（HKCU 可能被组策略锁了，"
                       f"可以改用  --install --task）")
    # 同一台机器上只留一套机制：用了 Run 项就把登录计划任务清掉
    enabled_task = _delete_logon_task()
    extra = "（同时清掉了旧的登录计划任务）" if enabled_task else ""
    return True, f"已设置开机自启，登录后静默运行：{cmd}{extra}"


def disable_autostart() -> tuple[bool, str]:
    """删掉免安装版的自启项（Run 项 + 登录计划任务）。"""
    if not wallpaper.IS_WINDOWS:
        return False, "只有 Windows 支持开机自启"
    import winreg

    removed: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE_NAME)
        removed.append("HKCU Run 项")
    except FileNotFoundError:
        pass
    except OSError as e:
        return False, f"删注册表失败：{e}"
    if _delete_logon_task():
        removed.append("登录计划任务")
    if not removed:
        return True, "本来就没设置开机自启"
    return True, "已取消开机自启（" + "、".join(removed) + "）"


def _delete_logon_task() -> bool:
    """删掉 enable_autostart.bat /task 建的那个登录计划任务。"""
    if not wallpaper.IS_WINDOWS:
        return False
    code, _out = N.run_hidden(["schtasks", "/delete", "/tn", LOGON_TASK_NAME, "/f"])
    return code == 0


def firewall_rule_ok() -> bool | None:
    """入站 UDP 38571 放行了没有（查不出来返回 None）。"""
    code, out = N.run_hidden(["netsh", "advfirewall", "firewall", "show", "rule",
                              "name=all", "dir=in"], timeout=25)
    if code != 0 and not out:
        return None
    return "38571" in out


def _shell_execute_runas(exe: str, params: str, timeout: float = 90.0) -> tuple[bool, str]:
    """用 UAC 提权跑一个命令，并等它结束。用户点「否」时返回失败。"""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception as e:
        return False, f"无法调用系统接口：{e}"

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.nShow = SW_HIDE
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        hint = "，用户取消了 UAC 授权" if err == 1223 else ""
        return False, f"提权失败（错误码 {err}{hint}）"
    try:
        kernel32.WaitForSingleObject(sei.hProcess, int(max(5.0, timeout) * 1000))
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return code.value == 0, f"退出码 {code.value}"
    finally:
        try:
            kernel32.CloseHandle(sei.hProcess)
        except Exception:
            pass


def add_firewall_rule(elevate: bool = True) -> tuple[bool, str]:
    """放行入站 UDP 38571（被控端要收广播 + 逐台单播）。

    非管理员时会用 UAC 单独提权跑一次 netsh（只提权这一件事）。
    """
    state = firewall_rule_ok()
    if state is True:
        return True, "防火墙已放行入站 UDP 38571"

    args = ["advfirewall", "firewall", "add", "rule", f"name={FW_RULE_NAME}",
            "dir=in", "action=allow", "protocol=UDP", "localport=38571"]
    code, _out = N.run_hidden(["netsh", *args])
    if code == 0:
        return True, "已放行入站 UDP 38571"

    if not elevate:
        return False, "放行失败：需要管理员权限"
    ok, msg = _shell_execute_runas("netsh.exe", " ".join(args))
    if ok:
        return True, "已放行入站 UDP 38571（刚才弹的那次 UAC 就是为了这个）"
    return False, (f"自动放行失败（{msg}）：请以管理员身份运行 "
                   f"add_firewall_rules.bat")


def _child_env() -> dict:
    """「再启动一份自己」时要用的干净环境变量。

    PyInstaller 单文件 exe 在环境里留了 _MEIPASS / _PYI_* 之类的标记，
    直接 Popen 自己会踩坑：新进程以为自己是被解包出来的子进程，去复用
    父进程的临时目录 —— 结果它起不来（父进程一清理临时目录就被带走），
    而父进程还在原地等，表现出来就是「命令卡住不返回、后台也没起来」。
    """
    env = dict(os.environ)
    for key in ("_MEIPASS", "_MEIPASS2", "_PYI_ARCHIVE_FILE",
                "_PYI_PARENT_PROCESS_LEVEL", "_PYI_APPLICATION_HOME_DIR",
                "_PYI_SPLASH_IPC", "PYINSTALLER_RESET_ENVIRONMENT"):
        env.pop(key, None)
    # 官方开关：告诉 bootloader「这一次当全新启动处理」
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _start_self_silent() -> tuple[bool, str]:
    """立刻以静默方式把自己再启动一份（后台，不占控制台）。"""
    if winipc.instance_running():
        return True, "被控端已经在运行"
    flags = 0x00000008 | 0x08000000        # DETACHED_PROCESS | CREATE_NO_WINDOW
    try:
        subprocess.Popen(_self_argv(["--silent"]), creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         env=_child_env())
    except Exception as e:
        return False, f"启动失败：{e}（手动运行一次 WallpaperAgent.exe 即可）"
    # 别急着说成功：等一会儿看它到底有没有活下来
    for _ in range(24):
        if winipc.instance_running():
            return True, "已在后台静默启动"
        time.sleep(0.25)
    return False, (f"启动后没检测到运行中的实例，请看日志：{default_log_path()}")


def cmd_install(cfg: dict, cfg_path: str = "", use_task: bool = False,
                skip_start: bool = False) -> int:
    """便携版一键装好：写自启 + 放行防火墙 + 设面板密码 + 立刻静默跑起来。"""
    print(f"{APP_NAME} v{APP_VER} —— 便携版一键安装")
    print()
    rc = 0

    # MSI 装的机器上自启项在 HKLM，由安装包管理，再写一份 HKCU 只会多此一举
    if any(e["hive"] == "HKLM" for e in autostart_entries()):
        print("[--]   本机已经由 MSI 配置了开机自启（HKLM Run），跳过自启设置")
    elif use_task:
        ok, msg = enable_logon_task()
        print(("[OK]   " if ok else "[FAIL] ") + msg)
        if not ok:
            rc = 1
    else:
        ok, msg = enable_autostart()
        print(("[OK]   " if ok else "[FAIL] ") + msg)
        if not ok:
            rc = 1

    ok2, msg2 = add_firewall_rule()
    print(("[OK]   " if ok2 else "[WARN] ") + msg2)

    # 通知组件：第一次安装时顺手装上（没网/装不上不影响壁纸功能）
    if toastmod.module_installed(str(cfg.get("toast_module_path") or "")):
        print("[OK]   通知组件 BurntToast 已就绪")
    else:
        print("[..]   正在安装通知组件 BurntToast（首次安装）…")
        ok_t, msg_t = toastmod.install_module()
        print(("[OK]   " if ok_t else "[WARN] ") + msg_t)
    # 注册通知身份：让通知显示成我们自己的名字而不是「Windows PowerShell」
    toastmod.set_app_display_name(str(cfg.get("toast_app_name") or ""))
    ok_id, msg_id = toastmod.ensure_app_id()
    print(("[OK]   " if ok_id else "[WARN] ") + msg_id)
    # 第一次装的时候把通知优先级调到最高（允许紧急通知）
    ok_p, msg_p = toastmod.ensure_notification_priority()
    print(("[OK]   " if ok_p else "[WARN] ") + msg_p)


    # 面板密码：第一次装的时候顺手设掉，别等用户哪天想起来
    if agentauth.is_set(cfg):
        _blob, source = agentauth.load_blob(cfg)
        print(f"[OK]   面板密码已经设置过（来源：{source}）")
    elif agentauth._console_usable() or agentauth._has_display():
        # 有界面就问一次；装好之后面板、停止、卸载都要它
        def write_config(c: dict) -> None:
            if cfg_path:
                N.save_json(cfg_path, {k: v for k, v in c.items()
                                       if not k.startswith("_")})

        ok_pw, msg_pw = agentauth.setup_password(
            cfg, write_config, prefer="auto")
        print(("[OK]   " if ok_pw else "[WARN] ") + msg_pw)
        if not ok_pw:
            print("       没有密码保护时，本机任何用户都能打开面板 / 停止 / 卸载它。")
            print("       之后可以随时补上：WallpaperAgent.exe --set-password")
    else:
        print("[WARN] 无人值守环境，无法交互设置面板密码 —— "
              "可以用 --set-password 设置，或把哈希写进配置集中下发")

    if skip_start:
        print("[--]   已跳过「立即启动」（--no-start）")
    else:
        ok3, msg3 = _start_self_silent()
        print(("[OK]   " if ok3 else "[WARN] ") + msg3)

    print()
    for line in describe_autostart():
        print(line)
    if not ok2:
        print()
        print("提示：防火墙没放行的话，被控端收不到控制端的广播。")
    print()
    print("卸载自启：WallpaperAgent.exe --uninstall（需要面板密码）")
    return rc


def enable_logon_task() -> tuple[bool, str]:
    """改用「登录计划任务」而不是 Run 项（需要管理员）。

    给那些用组策略封了 Run 项、或者被"优化软件"反复禁用启动项的机器用。
    """
    code, out = N.run_hidden(["schtasks", "/create", "/tn", LOGON_TASK_NAME,
                              "/tr", autostart_command(True),
                              "/sc", "onlogon", "/f"], timeout=30)
    if code != 0:
        return False, (f"创建登录计划任务失败（需要管理员权限）："
                       f"{out.splitlines()[-1] if out else '未知原因'}")
    # 用了计划任务就把 Run 项清掉，避免两套机制同时拉
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE_NAME)
    except OSError:
        pass
    return True, f"已创建登录计划任务 {LOGON_TASK_NAME}（每次登录静默启动）"


def cmd_uninstall(cfg: dict | None = None) -> int:
    """取消自启并停掉正在运行的实例（不删程序文件）。需要密码。"""
    print(f"{APP_NAME} v{APP_VER} —— 取消开机自启")
    print()
    ok_pw, msg_pw = agentauth.require(cfg or {}, "卸载/删除被控端", prefer="auto")
    print(msg_pw)
    if not ok_pw:
        return 1
    if winipc.instance_running():
        winipc.signal("stop")
        gone = winipc.wait_gone(8.0)
        print(("[OK]   " if gone else "[WARN] ") +
              ("已停止正在运行的被控端" if gone else
               "发出了停止信号，但进程还在（稍后可用 stop_agent.bat 强制结束）"))
    else:
        print("[--]   当前没有正在运行的被控端")

    ok, msg = disable_autostart()
    print(("[OK]   " if ok else "[FAIL] ") + msg)
    print()
    for line in describe_autostart():
        print(line)
    print()
    print("程序文件没有被删除，也没有删防火墙规则：")
    print("  - 想彻底清理：删掉 WallpaperAgent.exe 所在目录")
    print("  - 想删防火墙规则：remove_firewall_rules.bat（管理员）")
    return 0 if ok else 1


def cmd_allow_urgent(value: str = "on") -> int:
    """把本软件的通知优先级调高 / 关掉（对应系统里「允许紧急通知」那个开关）。

    第一次运行时被控端会自己设成"开"；之后想改（比如用户觉得太吵）
    用 `--allow-urgent off`，不要指望它下次启动又自动打开。
    """
    want = str(value or "on").strip().lower()
    if want in ("on", "1", "yes", "true", "开", "打开"):
        on = True
    elif want in ("off", "0", "no", "false", "关", "关闭"):
        on = False
    else:
        print(f"只能填 on 或 off（收到的是「{value}」）", file=sys.stderr)
        return 1
    print(f"{APP_NAME} v{APP_VER} —— 通知优先级")
    ok, msg = toastmod.set_notification_priority(on)
    print(("[OK]   " if ok else "[FAIL] ") + msg)
    print(toastmod.notification_priority_state())
    if on:
        print()
        print("说明：打开后，带「紧急」标记的通知会被系统当成重要通知 —— 能穿透")
        print("      专注助手（勿扰），并且在通知中心里排在前面。控制端发通知时")
        print("      勾上「紧急」就生效。")
    return 0 if ok else 1


def cmd_set_toast_app_name(cfg: dict, cfg_path: str, name: str) -> int:
    """改「通知上显示的应用名」（通知标题左边那个名字 + 通知中心里的分组名）。

    这是**每台机器各自**的设置（写在 HKCU 和开始菜单快捷方式里），
    控制端推消息时管不到它 —— 想让每条通知都带自己的落款，用通知里的
    「署名」字段（控制端「通知」页 → 高级选项 → 署名），那个是跟着消息走的。
    """
    print(f"{APP_NAME} v{APP_VER} —— 通知上显示的应用名")
    clean = " ".join(str(name or "").split())[:toastmod.MAX_APP_NAME_LEN]
    # 空字符串从命令行不好传（会被 shell 吃掉），所以给几个"恢复默认"的写法
    if clean.lower() in ("default", "reset", "-", "默认", "恢复默认"):
        clean = ""
    cfg["toast_app_name"] = clean
    if cfg_path:
        N.save_json(cfg_path, {k: v for k, v in cfg.items() if not k.startswith("_")})
    applied = toastmod.set_app_display_name(clean)
    ok, msg = toastmod.ensure_app_id()
    print(("[OK]   " if ok else "[FAIL] ") + msg)
    if ok:
        print()
        print(f"现在通知会显示成：{applied}")
        if not clean:
            print("（已恢复默认名字）")
        print("改完对**之后弹出**的通知生效；已经躺在通知中心里的旧通知还是老名字。")
        aumid = toastmod.shortcut_aumid()
        if aumid == toastmod.APP_ID:
            print("开始菜单快捷方式已带上 AppId（改名能被 Windows 认到）。")
        else:
            print(f"[!] 快捷方式上的 AppId 是「{aumid or '空'}」—— 这个名字可能不会被认到，")
            print("    请把这条输出发我：Windows 要求快捷方式带 System.AppUserModel.ID。")
        print("如果新弹出的通知还是旧名字：注销重登一次（shell 会缓存应用名）。")
        print("另外：被控端面板上也能改（「通知应用名」那一行的『改名…』按钮）。")
    return 0 if ok else 1


def cmd_install_toast_module(source: str = "", cfg_path: str = "",
                             cfg: dict | None = None) -> int:
    """装 BurntToast（客户端第一次安装时自动调用；也可手工跑）。"""
    print(f"{APP_NAME} v{APP_VER} —— 安装通知组件（BurntToast）")
    print()
    configured = str((cfg or {}).get("toast_module_path") or "")
    if toastmod.module_installed(configured):
        print("[OK]   " + toastmod.self_test(configured).replace("**", ""))
        return 0
    ok, msg = toastmod.install_module(source)
    print(("[OK]   " if ok else "[FAIL] ") + msg)
    if ok and cfg is not None and cfg_path:
        N.save_json(cfg_path, {k: v for k, v in cfg.items() if not k.startswith("_")})
    print()
    print(toastmod.self_test(configured).replace("**", ""))
    if not ok:
        print()
        print("离线部署办法：在能上网的机器上执行")
        print("    powershell -Command \"Save-Module BurntToast -Path .\\\"")
        print("把生成的 BurntToast 目录放到被控端 exe 旁边，然后：")
        print("    WallpaperAgent.exe --install-toast-module --source <那个目录>")
    return 0 if ok else 1


def cmd_test_toast(cfg: dict | None = None, duration: str = "") -> int:
    """本机弹一条测试通知（排查用）。

    `--duration` 可以现场试三种停留时长 —— 这个只能靠眼睛看，
    所以留一个"弹一条，你盯着屏幕数秒"的命令最实在。
    """
    print(f"{APP_NAME} v{APP_VER} —— 通知自检")
    configured = str((cfg or {}).get("toast_module_path") or "")
    toastmod.set_app_display_name(str((cfg or {}).get("toast_app_name") or ""))
    ok_id, msg_id = toastmod.ensure_app_id()
    print(("[OK]   " if ok_id else "[WARN] ") + msg_id)
    print(toastmod.self_test(configured).replace("**", ""))
    ok_p, msg_p = toastmod.ensure_notification_priority()
    print(("[OK]   " if ok_p else "[WARN] ") + msg_p)
    app_name = toastmod.app_display_name()
    dur = (duration or TEST_TOAST_DURATION or TS.DURATION_SHORT).strip()
    if dur not in TS.DURATIONS:
        print(f"不认识的停留时长「{dur}」（可选：{'、'.join(TS.DURATIONS)}）")
        return 1
    spec = {
        "text": [f"{APP_NAME} 通知自检", f"本机：{socket.gethostname()}",
                 time.strftime("%Y-%m-%d %H:%M:%S")],
        "sound": "Alarm2" if dur == TS.DURATION_UNTIL else "Default",
        "attribution": app_name,
        "unique_id": "wpp-toast-selftest",
        "expire_minutes": 10,
        "duration": dur,
    }
    ok, clean, err = TS.normalize(spec)
    if not ok:
        print("规格有误：", err)
        return 1
    print(f"停留时长：{TS.DURATION_LABELS.get(clean.get('duration'), '短（约 5 秒）')}"
          f"　署名：{app_name}")
    if clean.get("duration") == TS.DURATION_UNTIL:
        print("（这条会一直显示并循环响铃，要你点掉它才结束）")
    ok2, msg = toastmod.render(clean, configured)
    print(("[OK]   " if ok2 else "[FAIL] ") + msg)
    return 0 if ok2 else 1


def cmd_set_password(cfg: dict | None = None, cfg_path: str = "",
                     clear: bool = False) -> int:
    """设置 / 修改 / 取消被控端密码。"""
    def write_config(c: dict) -> None:
        N.save_json(cfg_path, {k: v for k, v in c.items() if not k.startswith("_")})

    print(f"{APP_NAME} v{APP_VER} —— 被控端密码")
    print()
    if clear:
        ok, msg = agentauth.clear_password(cfg, write_config)
    elif agentauth.is_set(cfg):
        ok, msg = agentauth.change_password(cfg, write_config)
    else:
        ok, msg = agentauth.setup_password(cfg, write_config)
    print(("[OK]   " if ok else "[!]    ") + msg)
    if ok:
        blob, source = agentauth.load_blob(cfg)
        print()
        print(f"现在生效的口令来源：{source}")
        if agentauth.IS_WINDOWS and not agentauth.machine_writable():
            print("提示：想让它更难被本地用户绕过，可以用管理员身份再运行一次本命令，")
            print("      口令会写进 C:\\ProgramData\\WinWallpaperPush\\secure\\"
                  "（普通用户只能读、不能改）。")
        print()
        print("这个哈希可以直接复制到别的机器的 agent_config.json 里，")
        print("这样整个机群用同一个密码，不用一台台设。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
