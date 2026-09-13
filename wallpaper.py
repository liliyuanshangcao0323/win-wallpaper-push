# -*- coding: utf-8 -*-
"""Windows 壁纸接口封装 —— 这是「被控制端」真正干活的地方。

核心是 Win32 API::

    BOOL SystemParametersInfoW(
        UINT  uiAction,   // SPI_SETDESKWALLPAPER = 20
        UINT  uiParam,    // 0
        PVOID pvParam,    // 壁纸图片的绝对路径（宽字符）
        UINT  fWinIni     // SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    );

配套要写两个注册表值，否则图片会被拉伸/平铺得很奇怪：

    HKCU\\Control Panel\\Desktop
        WallpaperStyle  "10"=填充 "6"=适应 "2"=拉伸 "0"=居中/平铺 "22"=跨区
        TileWallpaper   "1"=平铺 "0"=不平铺
"""

from __future__ import annotations

import ctypes
import os
import sys

import protocol as P

IS_WINDOWS = sys.platform == "win32"

SPI_SETDESKWALLPAPER = 20
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02

_DESKTOP_KEY = r"Control Panel\Desktop"

# 提前声明好参数类型，避免 64 位下指针被截断
if IS_WINDOWS:
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SystemParametersInfoW.argtypes = [
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    _user32.SystemParametersInfoW.restype = wintypes.BOOL
else:  # pragma: no cover - 仅为了在非 Windows 上也能 import 成功
    _user32 = None


# ---------------------------------------------------------------- 注册表样式

def set_style(style: str = P.DEFAULT_STYLE) -> None:
    """写入壁纸契合度（填充 / 适应 / 拉伸 …）。"""
    if not IS_WINDOWS:
        return
    import winreg

    ws, tile = P.STYLES.get(style, P.STYLES[P.DEFAULT_STYLE])
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _DESKTOP_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "WallpaperStyle", 0, winreg.REG_SZ, ws)
        winreg.SetValueEx(key, "TileWallpaper", 0, winreg.REG_SZ, tile)


def get_style() -> str:
    """读取当前壁纸契合度，返回中文名（读不到就返回默认值）。"""
    if not IS_WINDOWS:
        return P.DEFAULT_STYLE
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _DESKTOP_KEY) as key:
            ws = str(winreg.QueryValueEx(key, "WallpaperStyle")[0])
            tile = str(winreg.QueryValueEx(key, "TileWallpaper")[0])
        for name, (a, b) in P.STYLES.items():
            if a == ws and b == tile:
                return name
    except OSError:
        pass
    return P.DEFAULT_STYLE


# ---------------------------------------------------------------- 换壁纸

def _spi_set(path: str) -> bool:
    """调用 Win32 接口设置壁纸，返回是否成功。"""
    if not IS_WINDOWS:
        raise RuntimeError("当前系统不是 Windows，无法设置壁纸")
    ctypes.set_last_error(0)
    ok = _user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER,
        0,
        ctypes.c_wchar_p(path),
        SPIF_UPDATEINIFILE | SPIF_SENDCHANGE,
    )
    return bool(ok)


def _to_bmp(src: str) -> str | None:
    """把任意图片转成 BMP（老版本 Windows 只认 BMP），需要 Pillow。"""
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return None
    try:
        dst = os.path.splitext(src)[0] + ".bmp"
        with Image.open(src) as im:
            im.convert("RGB").save(dst, "BMP")
        return dst
    except Exception:
        return None


def set_wallpaper(path: str, style: str | None = None) -> str:
    """把 path 设为桌面壁纸，返回实际生效的文件路径。

    参数
    ----
    path  : 图片文件路径（jpg / png / bmp / webp 都行）
    style : 契合度中文名；None 表示沿用当前设置

    注意：Windows 会因为「路径字符串没变」而跳过刷新，所以被控端
    每次都用带 task_id 的唯一文件名保存，保证一定重新绘制桌面。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"壁纸文件不存在：{path}")
    path = os.path.abspath(path)

    if style:
        set_style(style)

    if _spi_set(path):
        return path

    # 第一次失败：可能是格式不被接受，转成 BMP 再试一次
    bmp = _to_bmp(path)
    if bmp and _spi_set(bmp):
        return bmp

    err = ctypes.get_last_error() if IS_WINDOWS else 0
    raise OSError(f"设置壁纸失败（SystemParametersInfoW 错误码 {err}）")


def current_wallpaper() -> str:
    """读取当前壁纸路径（从注册表），读不到返回空串。"""
    if not IS_WINDOWS:
        return ""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _DESKTOP_KEY) as key:
            return str(winreg.QueryValueEx(key, "Wallpaper")[0])
    except OSError:
        return ""


def self_test() -> str:
    """自检：确认接口可用，返回一句人类可读的结论。"""
    if not IS_WINDOWS:
        return "非 Windows 系统，壁纸接口不可用"
    return (
        f"壁纸接口可用 | 当前契合度：{get_style()} | "
        f"当前壁纸：{current_wallpaper() or '（未设置）'}"
    )


if __name__ == "__main__":
    print(self_test())
