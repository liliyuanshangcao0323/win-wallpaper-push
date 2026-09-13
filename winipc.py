# -*- coding: utf-8 -*-
"""被控端控制通道（Windows 命名事件）。

为什么需要这个模块
------------------
被控端最常见的部署方式是「开机自启 + 静默后台」：没有窗口、没有托盘图标。
以前想关掉它只能去任务管理器里结束进程 —— 用户既不知道它在跑，也找不到关的地方。
这里用 Windows 命名内核对象做一条很小的控制通道：

    WallpaperAgent.exe            已经在跑 → 把它的窗口叫出来；没跑 → 正常启动
    WallpaperAgent.exe --silent   静默启动（后台，窗口藏起来，之后可以叫出来）
    WallpaperAgent.exe --stop     让正在运行的实例退出
    WallpaperAgent.exe --status   看一眼到底有没有在跑、日志在哪儿

为什么用 `Local\\` 前缀
-----------------------
被控端本来就跑在每个用户自己的登录会话里（桌面壁纸是每用户设置），所以
「同一会话内可见」正好合适：不需要管理员权限，也不会串到别人的会话里去。
`Global\\` 需要 SeCreateGlobalPrivilege，普通用户没有，会有创建失败的风险。

非 Windows 平台一律降级成空操作，方便在别的系统上跑测试。
"""

from __future__ import annotations

import sys
import time

IS_WINDOWS = sys.platform == "win32"

_PREFIX = "Local\\WinWallpaperPushAgent."
KINDS = ("stop", "show")

EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

_K32 = None


def _kernel32():
    """延迟加载 kernel32（非 Windows 上直接抛错，由调用方兜住）。"""
    global _K32
    if _K32 is None:
        import ctypes

        _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return _K32


def event_name(kind: str) -> str:
    return _PREFIX + kind.capitalize()


# ---------------------------------------------------------------- 问 / 喊

def instance_running() -> bool:
    """当前会话里有没有被控端在跑。

    命名事件由被控端持有，进程一退出（哪怕是被强杀）内核对象立刻消失，
    所以「能打开事件」等价于「有实例活着」，不会留下过期状态。
    """
    if not IS_WINDOWS:
        return False
    try:
        k = _kernel32()
        for kind in KINDS:
            handle = k.OpenEventW(EVENT_MODIFY_STATE | SYNCHRONIZE, False,
                                  event_name(kind))
            if handle:
                k.CloseHandle(handle)
                return True
    except Exception:
        return False
    return False


def signal(kind: str) -> bool:
    """给正在运行的实例打一个信号（stop / show）。返回是否真的送到了。"""
    if not IS_WINDOWS or kind not in KINDS:
        return False
    try:
        k = _kernel32()
        handle = k.OpenEventW(EVENT_MODIFY_STATE, False, event_name(kind))
        if not handle:
            return False
        try:
            return bool(k.SetEvent(handle))
        finally:
            k.CloseHandle(handle)
    except Exception:
        return False


def wait_gone(timeout: float = 8.0, interval: float = 0.2) -> bool:
    """等实例消失（`--stop` 之后用，脚本才好判断结果）。"""
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not instance_running():
            return True
        time.sleep(interval)
    return not instance_running()


# ---------------------------------------------------------------- 实例自己用

class AgentSignals:
    """被控端自己持有的命名事件集合。

    `create()` 会先确认没有别的实例 —— 命名事件是「同名即同一个对象」，
    如果不加判断，第二个实例会拿到第一个实例的事件句柄，把对方的信号吃掉。
    """

    def __init__(self, handles: dict[str, int]):
        self._handles = handles
        self._ctypes = None

    @classmethod
    def create(cls) -> "AgentSignals | None":
        """创建事件；已经有实例在跑时返回 None。"""
        if not IS_WINDOWS:
            return cls({})
        if instance_running():
            return None
        import ctypes

        k = _kernel32()
        handles: dict[str, int] = {}
        for kind in KINDS:
            # 自动重置（manual reset = False）：一次 SetEvent 只唤醒一次等待
            handle = k.CreateEventW(None, False, False, event_name(kind))
            if not handle:
                for h in handles.values():
                    k.CloseHandle(h)
                return None
            handles[kind] = handle
        obj = cls(handles)
        obj._ctypes = ctypes
        return obj

    # ---------------- 取信号

    def take(self) -> list[str]:
        """非阻塞收一遍信号（界面定时器里调）。"""
        return self._wait(0)

    def wait(self, timeout: float = 1.0) -> list[str]:
        """等信号，最多等 timeout 秒（后台模式里调）。"""
        return self._wait(int(max(0.0, timeout) * 1000))

    def _wait(self, timeout_ms: int) -> list[str]:
        if not self._handles:
            # 非 Windows：没有信号源，退化成「睡一会儿」
            if timeout_ms > 0:
                time.sleep(timeout_ms / 1000.0)
            return []
        try:
            k = _kernel32()
            ctypes = self._ctypes
            kinds = [kind for kind in KINDS if kind in self._handles]
            array = (ctypes.c_void_p * len(kinds))(
                *[self._handles[kind] for kind in kinds])
            got: list[str] = []
            # 一次只等一个（waitAll = False）：自动重置事件收到就自动复位，
            # 循环几轮把这一批信号都收干净，避免同一个信号被处理两次。
            for _ in range(len(kinds)):
                rc = k.WaitForMultipleObjects(len(kinds), array, False, timeout_ms)
                if rc == WAIT_TIMEOUT or rc < WAIT_OBJECT_0 or rc >= WAIT_OBJECT_0 + len(kinds):
                    break
                got.append(kinds[rc - WAIT_OBJECT_0])
                timeout_ms = 0        # 剩下的只做「顺带收一下」
            return got
        except Exception:
            return []

    def close(self) -> None:
        if not self._handles:
            return
        try:
            k = _kernel32()
            for handle in self._handles.values():
                k.CloseHandle(handle)
        except Exception:
            pass
        self._handles = {}
