# -*- coding: utf-8 -*-
"""被控端的通知渲染（BurntToast）。

为什么用 BurntToast 而不是自己拼 WinRT
--------------------------------------
Windows 的 Toast 要走 WinRT（`Windows.UI.Notifications`），Python 标准库碰不到，
要么装第三方 winrt 包（PyInstaller 打包麻烦、版本脆弱），要么自己用 ctypes 摸
COM —— 都不划算。BurntToast 是个成熟的开源 PowerShell 模块（MIT），
一行 `New-BurntToastNotification` 就能带图标/大图/按钮/声音/进度条，
所以这里**调用 PowerShell + BurntToast**：

    powershell -NoProfile -ExecutionPolicy Bypass -File toast.ps1 -Spec <json>

代价是每条通知要起一个 PowerShell（约 0.5~1.5 秒），通知不是高频操作，可以接受。

模块从哪来（按顺序找，找不到就装）
----------------------------------
1. 配置里 `toast_module_path` 指定的目录（离线/内网最稳的方式）
2. exe 旁边的 `BurntToast\\`（把模块解压进去，跟着 exe 一起拷）
3. `%LOCALAPPDATA%\\WinWallpaperPush\\modules\\BurntToast\\`（自动装的位置）
4. 系统里已安装的（用户级 / 机器级 PowerShell 模块目录）
5. 都找不到 → `Install-Module BurntToast -Scope CurrentUser`（需要能上 PSGallery）

装不上不影响壁纸推送，只是通知发不出去；被控端会把失败原因回报控制端，
`--selftest` / `--status` 里也能看到。
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time

import netutil as N

IS_WINDOWS = sys.platform == "win32"

APP_DIR_NAME = "WinWallpaperPush"
MODULE_DIR_NAME = "BurntToast"
PS_SCRIPT_NAME = "toast.ps1"
# 脚本内容变了就重写（版本号跟着改）
PS_SCRIPT_VERSION = 6

# 通知的身份（AppUserModelID）：不注册的话，通知会以「Windows PowerShell」的名义
# 弹出来 —— 用户看着懵，而且 PowerShell 发的带网址按钮的通知正是典型的钓鱼样式，
# 很容易被 IT / 安全软件盯上。
APP_ID = "WinWallpaperPush.Agent"
DEFAULT_APP_NAME = "Win 壁纸推送"
# 当前进程里生效的通知显示名（可用配置 toast_app_name 覆盖，见 set_app_display_name）
_APP_NAME: list[str] = [""]
MAX_APP_NAME_LEN = 40
ICON_NAME = "wpp.ico"
SHORTCUT_NAME = "Win 壁纸推送.lnk"
# 「通知优先级已经设置过」的标记（只写一次，之后不跟用户手动改的设置对着干）
PRIORITY_MARKER_NAME = "notification_priority.json"
# 上一次生效过的自定义通知名（改回默认时用它清理旧快捷方式）
APP_NAME_MARKER_NAME = "app_name.json"
# 给开始菜单快捷方式写 AppUserModelID 用的小脚本（BurntToast 的 New-BTShortcut
# 并不会写这个属性，实测读回来是空的 —— 不写的话快捷方式和通知身份没有关联）
AUMID_SCRIPT_NAME = "set_aumid.ps1"

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def icon_path() -> str:
    """我们自己的图标（.ico）落地位置。"""
    return os.path.join(N.local_app_data_dir(APP_DIR_NAME), ICON_NAME)


def _make_ico(path: str, size: int = 32) -> bool:
    """手写一个 .ico（32×32、32 位 BGRA）。

    为什么自己画：不想为了一张图标给项目加 Pillow 依赖，也不想塞个二进制文件进
    仓库。图案很简单：深蓝渐变底 + 白色相框 + 一个「山和太阳」的壁纸意象。
    """
    w = h = size
    px: list[tuple[int, int, int, int]] = []
    for y in range(h):
        for x in range(w):
            # 圆角矩形底
            cx, cy = x / (w - 1), y / (h - 1)
            edge = 2.5 / w
            inside = (edge <= cx <= 1 - edge) and (edge <= cy <= 1 - edge)
            if not inside:
                px.append((0, 0, 0, 0))
                continue
            # 竖直渐变：上浅蓝下深蓝
            r = int(28 + 40 * (1 - cy))
            g = int(70 + 90 * (1 - cy))
            b = int(150 + 60 * (1 - cy))
            # 相框（四周一圈白）
            border = 4 / w
            if cx < border or cx > 1 - border or cy < border or cy > 1 - border:
                px.append((245, 248, 255, 255))
                continue
            # 太阳
            if (cx - 0.72) ** 2 + (cy - 0.30) ** 2 < 0.011:
                px.append((255, 214, 102, 255))
                continue
            # 山（左低右高的折线）
            ridge = 0.72 - 0.34 * abs(cx - 0.42) / 0.42
            if cy > ridge:
                px.append((236, 242, 252, 255))
                continue
            px.append((r, g, b, 255))

    # ICO 里塞一张 BMP（BITMAPINFOHEADER + 像素 + AND 掩码），兼容性最好
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    body = bytearray()
    for y in range(h - 1, -1, -1):          # BMP 自下而上
        for x in range(w):
            r, g, b, a = px[y * w + x]
            body += bytes((b, g, r, a))
    mask_row = ((w + 31) // 32) * 4         # AND 掩码按 4 字节对齐
    body += b"\x00" * (mask_row * h)
    image = header + bytes(body)

    ico = struct.pack("<HHH", 0, 1, 1)
    ico += struct.pack("<BBBBHHII", w if w < 256 else 0, h if h < 256 else 0,
                       0, 0, 1, 32, len(image), 6 + 16)
    ico += image
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(ico)
        return True
    except OSError:
        return False


def module_cache_dir() -> str:
    """自动安装 / 解包 BurntToast 的地方（每用户，永远可写）。"""
    return os.path.join(N.local_app_data_dir(APP_DIR_NAME), "modules", MODULE_DIR_NAME)


def local_module_dir() -> str:
    """exe 旁边的 BurntToast 目录（离线部署：跟着 exe 一起拷）。

    这也是「运维要换/升级模块」的入口：放在这里的一定优先于 exe 内置的那份。
    """
    return os.path.join(N.app_dir(), MODULE_DIR_NAME)


def bundled_module_dir() -> str:
    """打进 exe 内部的那份 BurntToast 目录（没打包就返回 ""）。

    PyInstaller 单文件 exe 启动时会把 --add-data 的内容解到 sys._MEIPASS，
    整个进程存活期间它都在，子进程 powershell.exe 直接就能 Import-Module，
    所以不必再往磁盘上多拷一份 —— 客户机只拷一个 exe 就能发通知。

    体积说明：内置的是「裁剪版」模块，去掉了 20 MB 的 WinRT 投影
    Microsoft.Windows.SDK.NET.dll（那是 Toolkit 兼容提交路径才需要的，
    本工具用显式 AppId 提交，用不到），模块从 21.8 MB 降到约 1 MB。
    """
    base = getattr(sys, "_MEIPASS", "")
    if not base:
        return ""
    return os.path.join(base, MODULE_DIR_NAME)


def script_path() -> str:
    """toast.ps1 的落地位置。"""
    return os.path.join(N.local_app_data_dir(APP_DIR_NAME), PS_SCRIPT_NAME)


def _module_root(path: str) -> str:
    """把一个目录解析成「真正放着 BurntToast.psd1 的那个目录」。

    两种常见布局都要认：
      * `...\\BurntToast\\BurntToast.psd1`                     （直接拷出来的）
      * `...\\BurntToast\\1.1.0\\BurntToast.psd1`              （Save-Module 下的）
    找不到返回空串。
    """
    if not path or not os.path.isdir(path):
        return ""
    for name in ("BurntToast.psd1", "BurntToast.psm1"):
        if os.path.isfile(os.path.join(path, name)):
            return path
    try:
        for entry in sorted(os.listdir(path), reverse=True):
            sub = os.path.join(path, entry)
            if not os.path.isdir(sub):
                continue
            for name in ("BurntToast.psd1", "BurntToast.psm1"):
                if os.path.isfile(os.path.join(sub, name)):
                    return sub
    except OSError:
        pass
    return ""


def _module_ok(path: str) -> bool:
    """这个目录（或其版本子目录）里是不是一个能用的 BurntToast 模块。"""
    return bool(_module_root(path))


_seed_lock = threading.Lock()
_seeded_from_bundle = False


def _seed_cache_from_bundle(bundled: str) -> str:
    """把 exe 内置的模块释放一份到本机模块目录；返回放好的目录，失败返回 ""。

    为什么不直接用 _MEIPASS 里那份：那是 PyInstaller 的临时解包目录，
    存储感知 / CCleaner 之类的清理工具可能在程序运行期间就把它删掉，而弹通知时
    PowerShell 是**真要去读那个目录**的 —— 一旦被删就变成查不出原因的静默失败。
    释放到 %LOCALAPPDATA% 之后路径稳定，运维也能自己进目录看/替换模块。
    """
    global _seeded_from_bundle
    dst = module_cache_dir()
    with _seed_lock:
        already = _module_root(dst)          # 另一个线程可能已经释放好了
        if already:
            return already
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copytree(bundled, dst, dirs_exist_ok=True)
        except OSError:
            return ""
        found = _module_root(dst)
        if found:
            _seeded_from_bundle = True
        return found


def find_module_dir(configured: str = "") -> str:
    """找一个可用的 BurntToast 目录；没找到返回 ""（让 PowerShell 自己去找已安装的）。

    查找顺序（先找到的先用）：
      1. 调用方/配置指定的目录
      2. exe 旁边的 BurntToast\\      —— 运维要覆盖或升级模块时放这里
      3. 本机模块目录                —— 自动装的、--source 拷的，或由内置模块释放来的
      4. exe 内置的那份             —— 打包进 exe，拷一个文件就能用（发现时顺带释放一份到 3）
    """
    for cand in (configured, local_module_dir(), module_cache_dir()):
        found = _module_root(cand)
        if found:
            return found
    bundled = _module_root(bundled_module_dir())
    if not bundled:
        return ""
    return _seed_cache_from_bundle(bundled) or bundled


def module_origin(found: str) -> str:
    """把 find_module_dir 的结果翻译成一句人话，给日志/状态用。"""
    if not found:
        return ""
    bundled = bundled_module_dir()
    if bundled and os.path.normcase(found).startswith(os.path.normcase(bundled)):
        return "随 exe 内置"
    if found == local_module_dir():
        return "exe 旁边"
    if found == module_cache_dir():
        return "exe 内置 → 已释放到本机模块目录" if _seeded_from_bundle \
            else "本工具模块目录"
    return found


def _powershell() -> str:
    for name in ("powershell.exe", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def app_id_registered() -> bool:
    """我们的通知身份注册好了没有（HKCU 里的 AppUserModelId）。"""
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            rf"Software\Classes\AppUserModelId\{APP_ID}") as k:
            name = winreg.QueryValueEx(k, "DisplayName")[0]
        return bool(str(name).strip())
    except OSError:
        return False


def shortcut_path() -> str:
    """开始菜单里那个带 AppId 的快捷方式（通知的名称/图标靠它解析）。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs",
                        SHORTCUT_NAME)


def app_display_name() -> str:
    """通知上显示的名字（通知标题左边、通知中心里分组用的那个）。

    默认「Win 壁纸推送」，可以用被控端配置 `toast_app_name` 换成别的
    （比如「IT 运维通知」），见 `set_app_display_name()`。
    """
    return _APP_NAME[0] or DEFAULT_APP_NAME


def set_app_display_name(name: str) -> str:
    """设置这次的进程里通知显示成什么名字；返回生效后的名字（空 = 用默认）。"""
    clean = " ".join(str(name or "").split())[:MAX_APP_NAME_LEN]
    _APP_NAME[0] = clean
    return app_display_name()


def shortcut_path(name: str = "") -> str:
    """开始菜单里那个带 AppId 的快捷方式（通知的名称/图标靠它解析）。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs",
                        shortcut_name(name))


def shortcut_name(name: str = "") -> str:
    return f"{name or app_display_name()}.lnk"


def _app_name_marker() -> str:
    return os.path.join(N.local_app_data_dir(APP_DIR_NAME), APP_NAME_MARKER_NAME)


def _read_prev_app_name() -> str:
    """上一次生效过的自定义名字（用来清理它留下的快捷方式）。"""
    try:
        with open(_app_name_marker(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("name") or "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def _read_app_marker() -> dict:
    try:
        with open(_app_name_marker(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_app_marker(data: dict) -> None:
    try:
        path = _app_name_marker()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


def _write_prev_app_name(name: str) -> None:
    data = _read_app_marker()
    data["name"] = name
    _write_app_marker(data)


# ---------------------------------------------------------------- 快捷方式的 AUMID

# 为什么要自己写这个属性：Windows 要求"桌面应用的开始菜单快捷方式带
# System.AppUserModel.ID"，才会把这个 AppId 认成"一个真实的应用"（通知的
# 名字/图标、通知中心里的分组都靠它）。BurntToast 的 New-BTShortcut -AppId
# 实测**没有**写进去（用 Shell 属性系统读回来是空字符串），于是 Windows 给
# 快捷方式发了个自动 AUMID（Microsoft.AutoGenerated.{GUID}）—— 快捷方式和
# 我们的通知身份就此脱钩：改了 DisplayName，通知上显示的名字也不会跟着变。
_AUMID_SCRIPT = r"""# 给 .lnk 写 System.AppUserModel.ID（由被控端自动生成，不要手改）
param(
    [Parameter(Mandatory=$true)][string]$Lnk,
    [string]$AppId = '',
    [string]$Target = '',
    [string]$Name = '',
    [string]$Icon = '',
    [switch]$Create,
    [switch]$Read,
    [switch]$Clean
)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class LnkAumid {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PROPERTYKEY { public Guid fmtid; public uint pid; }

    [StructLayout(LayoutKind.Explicit)]
    public struct PROPVARIANT {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(8)] public IntPtr pointerValue;
    }

    [ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        void GetCount(out uint cProps);
        void GetAt(uint iProp, out PROPERTYKEY pkey);
        void GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
        void SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
        void Commit();
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHGetPropertyStoreFromParsingName(
        string pszPath, IntPtr zeroWorks, int flags,
        ref Guid riid, [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);

    public static void Set(string path, string appId) {
        Guid iid = new Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99");
        IPropertyStore store;
        SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 2, ref iid, out store);
        PROPERTYKEY key = new PROPERTYKEY();
        key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
        key.pid = 5;
        PROPVARIANT pv = new PROPVARIANT();
        pv.vt = 31;
        pv.pointerValue = Marshal.StringToCoTaskMemUni(appId);
        try {
            store.SetValue(ref key, ref pv);
            store.Commit();
        } finally {
            Marshal.FreeCoTaskMem(pv.pointerValue);
            Marshal.ReleaseComObject(store);
        }
    }

    public static string Get(string path) {
        // 只用它做兼容占位；读取统一走 Shell.Application（见下面），
        // 因为 IPropertyStore 读 PROPVARIANT 的封送容易踩坑（读回来总是空串，
        // 明明属性是写进去了的）。
        return "";
    }
}
"@
function Read-Aumid([string]$Path) {
    # 用 Shell 属性系统读，稳（实测 Windows 11 上可靠）
    try {
        $sh = New-Object -ComObject Shell.Application
        $dir = Split-Path -Parent $Path
        $leaf = Split-Path -Leaf $Path
        $item = $sh.Namespace($dir).ParseName($leaf)
        if ($item) { return [string]$item.ExtendedProperty('System.AppUserModel.ID') }
    } catch { }
    return ''
}

# 清理"我们自己的 AppId"留下的旧快捷方式（改名后老文件不会自己消失）。
# 判据用 AUMID 而不是文件名：凡是带这个 AppId 的快捷方式（除了当前这个）
# 都是我们建的，可以安全删掉 —— 用户自己建的东西不会被误伤。
function Remove-Stale([string]$Dir, [string]$AppId, [string]$Keep) {
    $sh = New-Object -ComObject Shell.Application
    $ns = $sh.Namespace($Dir)
    foreach ($f in $ns.Items()) {
        try {
            if ($f.Name -notlike '*.lnk') { continue }
            if ($f.Path -eq $Keep) { continue }
            $id = [string]$f.ExtendedProperty('System.AppUserModel.ID')
            if ($id -eq $AppId) {
                Remove-Item -LiteralPath $f.Path -Force -ErrorAction SilentlyContinue
                Write-Output ("REMOVED=" + $f.Path)
            }
        } catch { }
    }
}

if ($Clean -and $AppId -ne '') {
    Remove-Stale (Split-Path -Parent $Lnk) $AppId $Lnk
    exit 0
}

if ($Create) {
    # 自己建快捷方式，不用 BurntToast 的 New-BTShortcut —— 那个要 Import-Module
    # BurntToast，而客户机上模块是打进 exe、临时释放的，**不在系统模块路径里**，
    # 于是导入失败、快捷方式建不出来，连锁反应是 AppId 关联不到、改名不生效。
    # WScript.Shell 是 Windows 自带的，零依赖。
    if (Test-Path -LiteralPath $Lnk) { Remove-Item -LiteralPath $Lnk -Force -ErrorAction SilentlyContinue }
    $sh = New-Object -ComObject WScript.Shell
    $sc = $sh.CreateShortcut($Lnk)
    $sc.TargetPath = $Target
    if ($Icon) { $sc.IconLocation = $Icon }
    if ($Name) { $sc.Description = $Name }
    if ($Target) { $sc.WorkingDirectory = (Split-Path -Parent $Target) }
    $sc.Save()
    if (-not (Test-Path -LiteralPath $Lnk)) {
        Write-Output "CREATE_FAILED"
        exit 1
    }
    [LnkAumid]::Set($Lnk, $AppId)
    Write-Output ("AUMID=" + (Read-Aumid $Lnk))
    exit 0
}

if ($Read -or $AppId -eq '') {
    Write-Output ("AUMID=" + (Read-Aumid $Lnk))
    exit 0
}
[LnkAumid]::Set($Lnk, $AppId)
Write-Output ("AUMID=" + (Read-Aumid $Lnk))
"""


def aumid_script_path() -> str:
    return os.path.join(N.local_app_data_dir(APP_DIR_NAME), AUMID_SCRIPT_NAME)


def ensure_aumid_script() -> str:
    """写出（必要时）写 AUMID 的小脚本，返回路径。"""
    path = aumid_script_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = _AUMID_SCRIPT
        old = ""
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    old = f.read()
            except OSError:
                old = ""
        if old != body:
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
        return path
    except OSError:
        return ""


def shortcut_aumid(lnk: str = "") -> str:
    """读快捷方式上的 AppUserModelId（读不到返回空串）。"""
    lnk = lnk or shortcut_path()
    if not IS_WINDOWS or not os.path.isfile(lnk):
        return ""
    script = ensure_aumid_script()
    ps = _powershell()
    if not script or not ps:
        return ""
    code, out = N.run_hidden(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", script, "-Lnk", lnk, "-Read"], timeout=60)
    for line in (out or "").splitlines():
        if line.startswith("AUMID="):
            return line[len("AUMID="):].strip()
    return ""


def set_shortcut_aumid(lnk: str = "", app_id: str = "", log=None) -> tuple[bool, str]:
    """给快捷方式写上 AppUserModelId，并读回来确认。"""
    log = log or (lambda msg, level="info": None)
    lnk = lnk or shortcut_path()
    app_id = app_id or APP_ID
    if not IS_WINDOWS or not os.path.isfile(lnk):
        return False, "没有快捷方式可写"
    script = ensure_aumid_script()
    ps = _powershell()
    if not script or not ps:
        return False, "写不出/找不到 PowerShell"
    code, out = N.run_hidden(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", script, "-Lnk", lnk, "-AppId", app_id], timeout=120)
    got = ""
    for line in (out or "").splitlines():
        if line.startswith("AUMID="):
            got = line[len("AUMID="):].strip()
    if got == app_id:
        return True, f"快捷方式已带上 AppId（{app_id}）"
    log(f"给快捷方式写 AppId 没成功（读回来是「{got or '空'}」）："
        f"{(out or '').strip()[:160]}", "warn")
    return False, f"快捷方式上的 AppId 是「{got or '空'}」，不是 {app_id}"


def clean_stale_shortcuts(log=None) -> list[str]:
    """删掉「带我们 AppId 但不是当前那个」的旧快捷方式，返回删掉的路径。

    为什么按 AUMID 认而不是按名字列表：改名是用户随时能做的，名字组合无穷无尽，
    但"AUMID == WinWallpaperPush.Agent 的快捷方式"一定是我们自己建的 ——
    这样既不会漏掉历史遗留（包括手工改过名的），也不会误删用户自己的东西。
    """
    log = log or (lambda msg, level="info": None)
    if not IS_WINDOWS:
        return []
    keep = shortcut_path()
    folder = os.path.dirname(keep)
    if not os.path.isdir(folder):
        return []
    removed: list[str] = []

    # 判据一：名字历史里出现过、但已经不是当前名字的快捷方式。
    # （老版本建的文件根本没带 AUMID，所以光靠判据二认不出来）
    keep_norm = os.path.normcase(keep)
    for old_name in (_read_app_marker().get("history") or []):
        path = os.path.join(folder, f"{old_name}.lnk")
        if os.path.normcase(path) == keep_norm or not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            removed.append(path)
            log(f"已清掉改名前的旧快捷方式：{os.path.basename(path)}", "dim")
        except OSError:
            pass

    # 判据二：带我们 AppId 的快捷方式（除了当前这个）—— 收拾手工改名之类的遗留
    script = ensure_aumid_script()
    ps = _powershell()
    if script and ps:
        code, out = N.run_hidden(
            [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", script, "-Lnk", keep, "-AppId", APP_ID, "-Clean"], timeout=180)
        for line in (out or "").splitlines():
            if line.startswith("REMOVED="):
                path = line[len("REMOVED="):].strip()
                if os.path.normcase(path) not in {os.path.normcase(p) for p in removed}:
                    removed.append(path)
                    log(f"已清掉带旧身份的同名快捷方式：{os.path.basename(path)}", "dim")
    return removed


def create_shortcut(lnk: str = "", target: str = "", name: str = "",
                    icon: str = "", app_id: str = "",
                    log=None) -> tuple[bool, str]:
    """把开始菜单快捷方式建出来（并写上 AppId），**不依赖 BurntToast**。

    为什么不用 BurntToast 的 New-BTShortcut：它内部要 `Import-Module BurntToast`，
    而客户机上的模块是我们打进 exe、临时释放的，**不在系统模块路径里** ——
    导入失败 → 快捷方式建不出来 → AppId 关联不上 → "控制端下发改名"看着成功、
    屏幕上却没变化（这就是用户报的"下发不行"）。WScript.Shell 是系统自带的，
    零依赖，谁都能跑。
    """
    log = log or (lambda msg, level="info": None)
    lnk = lnk or shortcut_path()
    target = target or _self_target()
    name = name or app_display_name()
    icon = icon or icon_path()
    app_id = app_id or APP_ID
    if not IS_WINDOWS:
        return False, "只有 Windows 需要快捷方式"
    script = ensure_aumid_script()
    ps = _powershell()
    if not script or not ps:
        return False, "写不出/找不到 PowerShell"
    try:
        os.makedirs(os.path.dirname(lnk), exist_ok=True)
    except OSError:
        pass
    code, out = N.run_hidden(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", script, "-Lnk", lnk, "-AppId", app_id, "-Target", target,
         "-Name", name, "-Icon", icon, "-Create"], timeout=180)
    got = ""
    for line in (out or "").splitlines():
        if line.startswith("AUMID="):
            got = line[len("AUMID="):].strip()
    if got == app_id and os.path.isfile(lnk):
        return True, "快捷方式已建好并带上 AppId"
    log(f"建快捷方式失败：{(out or '').strip()[:200]}", "warn")
    return False, (f"快捷方式没建好（文件{'在' if os.path.isfile(lnk) else '不在'}，"
                   f"读回的 AppId 是「{got or '空'}」）")


def notify_shell_changed() -> None:
    """让资源管理器知道"应用信息变了"（改名之后刷新缓存用）。

    Windows 会把 AUMID -> 名字/图标 的解析结果缓存起来，改完注册表不一定马上
    生效；发一个 SHCNE_ASSOCCHANGED 通知是标准做法，比重启资源管理器温和得多。
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        SHCNF_FLUSH = 0x1000
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_ASSOCCHANGED, SHCNF_IDLIST | SHCNF_FLUSH, None, None)
    except Exception:
        pass


def ensure_app_id(log=None) -> tuple[bool, str]:
    """注册通知身份：图标 + HKCU AppId + 开始菜单快捷方式。幂等。

    为什么要这三样：
      * `HKCU\\Software\\Classes\\AppUserModelId\\<AppId>` 里的 DisplayName/IconUri
        —— 决定通知头上显示什么名字、什么图标（**名字可以自定义**，
        见 `set_app_display_name`）；
      * 一个带 `System.AppUserModel.ID = <AppId>` 的开始菜单快捷方式
        —— Windows 靠它把这个 AppId 解析成「一个真实的应用」；
      * 渲染进程要显式把自己标成这个 AppId（见 toast.ps1 里的
        SetCurrentProcessExplicitAppUserModelID），否则 BurntToast 用的
        CreateToastNotifier() 会带上 PowerShell 的身份。
    """
    log = log or (lambda msg, level="info": None)
    if not IS_WINDOWS:
        return False, "只有 Windows 需要注册通知身份"
    import winreg

    name = app_display_name()
    ico = icon_path()
    if not os.path.isfile(ico):
        if not _make_ico(ico):
            return False, "图标写不出去（检查 %LOCALAPPDATA% 权限）"

    # ① 注册 AppId（HKCU，不需要管理员）
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                rf"Software\Classes\AppUserModelId\{APP_ID}", 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, name)
            winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, ico)
    except OSError as e:
        return False, f"注册通知身份失败：{e}"

    # ② 开始菜单快捷方式（带 AppId；目标就指被控端自己，点开就是面板）
    lnk = shortcut_path()
    target = _self_target()
    marker = _read_app_marker()
    marker_name = str(marker.get("name") or "")
    need_create = not os.path.isfile(lnk) or marker_name != name
    if not need_create:
        # 已经存在也要确认它真的带上了 AppId：BurntToast 的 New-BTShortcut
        # 不写这个属性，老版本建出来的快捷方式都是"脱钩"的（Windows 会给它
        # 发一个 Microsoft.AutoGenerated.{GUID} 的自动身份）
        if marker.get("aumid_ok") != os.path.normcase(lnk):
            got = shortcut_aumid(lnk)
            if got != APP_ID:
                need_create = True
    aumid_ok = False
    aumid_msg = ""
    if need_create or marker.get("aumid_ok") != os.path.normcase(lnk):
        # 统一走"自己建 + 写 AppId + 读回校验"这条路（不依赖 BurntToast）
        ok_c, msg_c = create_shortcut(lnk, target, name, ico, APP_ID, log=log)
        aumid_ok, aumid_msg = set_shortcut_aumid(lnk, APP_ID, log=log) if ok_c \
            else (False, msg_c)
    else:
        # 上次已经建好并校验过了，这次不用再跑 PowerShell（启动快）
        aumid_ok, aumid_msg = True, f"快捷方式已带 AppId（未重建）：{lnk}"
    marker = _read_app_marker()
    if aumid_ok:
        marker["aumid_ok"] = os.path.normcase(lnk)
    else:
        marker.pop("aumid_ok", None)
    marker["name"] = name
    hist = [str(x) for x in (marker.get("history") or []) if str(x)]
    if name not in hist:
        hist.append(name)
    marker["history"] = hist[-20:]        # 只留最近 20 个用过的名字
    _write_app_marker(marker)
    if aumid_ok:
        log(aumid_msg, "dim")
    # ③ 改过名字的话，把老名字的快捷方式清掉（不然开始菜单里会留一个旧的）。
    #    按 AUMID + 名字历史认领，能连历史遗留一起收拾。
    if marker_name != name:
        clean_stale_shortcuts(log=log)
    notify_shell_changed()
    if not aumid_ok:
        # 别谎报成功：通知身份没关联上的话，"改通知应用名"在屏幕上是不会生效的
        return False, (f"通知身份注册不完整：{aumid_msg}；"
                       f"（快捷方式：{lnk}）—— 通知仍能弹，但改名字不会生效")
    return True, f"通知身份已就绪：{name}（{APP_ID}）"


def _self_target() -> str:
    """快捷方式指向哪：打包后指 exe，源码运行指 python（保证一定存在）。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    return sys.executable


def app_id_state() -> str:
    """一行说明，给自检/状态看。"""
    if not IS_WINDOWS:
        return "通知身份：非 Windows"
    if not app_id_registered():
        return "通知身份：未注册（通知会以「Windows PowerShell」的名义弹出）"
    ok_lnk = os.path.isfile(shortcut_path())
    name = app_display_name()
    extra = "" if name == DEFAULT_APP_NAME else "（自定义名字）"
    if ok_lnk:
        marker = _read_app_marker()
        if marker.get("aumid_ok") == os.path.normcase(shortcut_path()):
            lnk_note = "（快捷方式已带 AppId）"
        else:
            got = shortcut_aumid()
            lnk_note = ("（快捷方式已带 AppId）" if got == APP_ID
                        else f"（快捷方式的 AppId 是「{got or '空'}」—— 通知名字可能不跟着改）")
    else:
        lnk_note = "（缺开始菜单快捷方式）"
    return ("通知身份：已注册为「" + name + "」" + extra + lnk_note)


# ------------------------------------------------ 通知优先级（紧急通知）

# Windows 把「每个应用的通知设置」放在这里，子键名就是 AppUserModelID。
NOTIF_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"
# 这个 AppId 下真正存在、而且决定通知等级的开关：允许紧急通知。
# 打开之后带 scenario="urgent" 的通知会被当成「紧急/重要」处理 ——
# 能穿透专注助手（勿扰）、在通知中心里更显眼、声音也更不容易被静音。
URGENT_VALUE = "AllowUrgentNotifications"
# 老版本 Windows 10 用这个全局值记录「优先级应用」列表（本机 Win11 26200 上不存在）。
# 存在就顺手续上我们的 AppId；不存在就不硬造（格式没公开，乱写只会被忽略）。
PRIORITY_APPS_VALUE = "NOC_GLOBAL_SETTING_PRIORITY_APPS"


def _priority_marker() -> str:
    return os.path.join(N.local_app_data_dir(APP_DIR_NAME), PRIORITY_MARKER_NAME)


def allow_urgent_state() -> int | None:
    """读我们的「允许紧急通知」开关：1 / 0 / None（这台机器上还没这个值）。"""
    if not IS_WINDOWS:
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            rf"{NOTIF_SETTINGS_KEY}\{APP_ID}") as k:
            return int(winreg.QueryValueEx(k, URGENT_VALUE)[0])
    except OSError:
        return None


def priority_apps_contains() -> bool:
    """老版 Windows 的「优先级应用」列表里有我们吗（没有这个值就返回 False）。"""
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIF_SETTINGS_KEY) as k:
            raw = str(winreg.QueryValueEx(k, PRIORITY_APPS_VALUE)[0])
    except OSError:
        return False
    return APP_ID.lower() in raw.lower()


def set_notification_priority(on: bool = True,
                              log=None) -> tuple[bool, str]:
    """把本软件的通知优先级调到最高（或还原）。写注册表，不需要管理员。

    实际能做的（按本机 Win11 26200 上真实存在的值来）：
      1. `...\\Notifications\\Settings\\<AppId>\\AllowUrgentNotifications = 1`
         —— 就是「设置 → 系统 → 通知 → Win 壁纸推送 → 允许紧急通知」那个开关；
      2. 如果这台机器上存在老版的全局「优先级应用」列表，就把我们加进去。

    注意：Windows 11 里「设置优先级通知」那个列表本身是 CloudStore 管的、
    没有稳定公开的注册表值，所以「最高优先级」是通过 1 + 用 `scenario=urgent`
    发通知来实现的（控制端的「紧急」勾选框就是它）。
    """
    log = log or (lambda msg, level="info": None)
    if not IS_WINDOWS:
        return False, "只有 Windows 需要设置通知优先级"
    if not app_id_registered():
        ok_id, msg_id = ensure_app_id(log=log)
        if not ok_id:
            return False, f"通知身份没注册好，先解决这个：{msg_id}"
    import winreg

    want = 1 if on else 0
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                rf"{NOTIF_SETTINGS_KEY}\{APP_ID}", 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, URGENT_VALUE, 0, winreg.REG_DWORD, want)
    except OSError as e:
        return False, f"写注册表失败：{e}"

    extra = ""
    if on:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIF_SETTINGS_KEY, 0,
                                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
                kind, raw = winreg.QueryValueEx(k, PRIORITY_APPS_VALUE)
                text = str(raw)
                if APP_ID.lower() not in text.lower():
                    sep = "" if not text.strip() else (";" if ";" in text else ",")
                    winreg.SetValueEx(k, PRIORITY_APPS_VALUE, 0, kind,
                                      text + sep + APP_ID)
                    extra = "；并已加入系统的「优先级应用」列表"
        except OSError:
            pass                       # 这个值不存在（Win11 常态），不影响上面那条

    _write_priority_marker({"applied": bool(on), "at": time.time(),
                            URGENT_VALUE: want})
    if on:
        log("已把本软件的通知优先级调到最高（允许紧急通知）", "ok")
        return True, ("通知优先级已设为最高：允许紧急通知"
                      f"（HKCU\\{NOTIF_SETTINGS_KEY}\\{APP_ID}\\{URGENT_VALUE}=1）" + extra)
    log("已关闭本软件的通知优先级（允许紧急通知 = 0）", "warn")
    return True, f"通知优先级已关闭（{URGENT_VALUE}=0）"


def _read_priority_marker() -> dict:
    try:
        with open(_priority_marker(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_priority_marker(data: dict) -> None:
    try:
        path = _priority_marker()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


def ensure_notification_priority(log=None, force: bool = False) -> tuple[bool, str]:
    """第一次运行时把通知优先级调到最高；之后不再动（幂等）。

    为什么只做一次：这个开关在「设置 → 通知」里用户随时能关掉，每次启动都强行
    改回去等于跟用户对着干。所以用一个小标记文件记住"已经设置过"，
    要用 `set_notification_priority(False)` / `--allow-urgent off` 才能改回来。
    """
    log = log or (lambda msg, level="info": None)
    if not IS_WINDOWS:
        return False, "只有 Windows 需要设置通知优先级"
    marker = _read_priority_marker()
    if marker.get("applied") and not force:
        return True, "通知优先级之前已经设置过（不重复改动）"
    return set_notification_priority(True, log=log)


def notification_priority_state() -> str:
    """一行说明：现在这个软件的通知优先级是什么状态。

    刻意写短：这行同时显示在被控端面板的「通知优先级」那一格和 `--status` 里，
    面板那一格只有 460px 左右，太长就会被切掉半截（文字被切是排版里最难看的毛病）。
    """
    if not IS_WINDOWS:
        return "通知优先级：非 Windows"
    state = allow_urgent_state()
    if state == 1:
        return f"通知优先级：最高（{URGENT_VALUE}=1）"
    if state == 0:
        return "通知优先级：未开启 · 用 --allow-urgent on 打开"
    return "通知优先级：未设置 · 用 --allow-urgent on 打开"


def powershell_available() -> bool:
    return bool(_powershell())


def module_installed(configured: str = "") -> bool:
    """系统里 / 指定目录里有没有 BurntToast（问 PowerShell 自己）。"""
    if find_module_dir(configured):
        return True
    ps = _powershell()
    if not ps:
        return False
    code, out = N.run_hidden(
        [ps, "-NoProfile", "-NonInteractive", "-Command",
         "if (Get-Module -ListAvailable BurntToast) { 'YES' } else { 'NO' }"],
        timeout=60)
    return "YES" in out


def install_module(source: str = "", log=None, timeout: float = 240.0) -> tuple[bool, str]:
    """安装 BurntToast。

    source 给了就把那个目录（或其中的 BurntToast 目录）拷到本工具的模块目录；
    没给先用 exe 内置的那份（释放到本机模块目录），都没有才去 PSGallery 装。

    timeout 默认 240 秒：实测一次 Install-Module 就要 20~30 秒（要连 PSGallery），
    客户机不通外网时会等到它自己超时，所以不能无限期挂着。
    """
    log = log or (lambda msg, level="info": None)

    # ⓪ exe 里自带 → 释放一份到本机模块目录，不必为了装模块去连外网
    if not source:
        bundled = _module_root(bundled_module_dir())
        if bundled:
            seeded = _seed_cache_from_bundle(bundled)
            if seeded:
                log(f"已从 exe 内置的 BurntToast 释放到 {seeded}", "ok")
                return True, (f"已从 exe 内置模块释放到 {seeded}（不需要外网；"
                              f"要完整版可以用 --source 指定目录）")

    # ① 有现成的目录 → 直接拷（内网/离线最靠谱）
    if source:
        src = _module_root(source) or _module_root(os.path.join(source, MODULE_DIR_NAME))
        if not src:
            return False, f"「{source}」里没有 BurntToast.psd1/psm1，不是模块目录"
        dst = module_cache_dir()
        try:
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copytree(src, dst)
        except OSError as e:
            return False, f"复制模块失败：{e}"
        log(f"已把 BurntToast 复制到 {dst}", "ok")
        return True, f"已从 {src} 安装到 {dst}"

    # ② 从 PSGallery 装（需要能上外网）
    ps = _powershell()
    if not ps:
        return False, "找不到 powershell.exe，无法安装 BurntToast"
    cmd = (
        "$ErrorActionPreference='Stop';"
        "try {"
        "  if (-not (Get-PackageProvider -Name NuGet -ErrorAction SilentlyContinue)) {"
        "    Install-PackageProvider -Name NuGet -Force -Scope CurrentUser | Out-Null }"
        "  Set-PSRepository -Name PSGallery -InstallationPolicy Trusted -ErrorAction SilentlyContinue;"
        "  Install-Module -Name BurntToast -Scope CurrentUser -Force -AllowClobber;"
        "  if (Get-Module -ListAvailable BurntToast) { 'INSTALLED' } else { 'MISSING' }"
        "} catch { 'ERROR: ' + $_.Exception.Message }"
    )
    code, out = N.run_hidden([ps, "-NoProfile", "-NonInteractive", "-Command", cmd],
                             timeout=timeout)
    if "INSTALLED" in out:
        log("已从 PSGallery 安装 BurntToast（当前用户）", "ok")
        return True, "已用 Install-Module 安装 BurntToast（当前用户）"
    detail = out.strip().splitlines()[-1] if out.strip() else f"退出码 {code}"
    return False, (f"自动安装 BurntToast 失败：{detail}；"
                   f"可以离线部署：把 BurntToast 目录放到 exe 旁边，"
                   f"或运行 --install-toast-module --source <目录>")


MODULE_HELP = (
    "本机没有 BurntToast 通知模块。四种办法（任选一条，都不需要重装本工具）：\n"
    "  1) 用打包好的 WallpaperAgent.exe —— 模块已经内置在 exe 里，本不该缺。\n"
    "     还出现这条，说明你是用源码 python 在跑，或者 exe 是旧版\n"
    "  2) 有外网：在客户机上运行  WallpaperAgent.exe --install-toast-module\n"
    "  3) 没外网：在能上网的机器上运行  powershell -Command \"Save-Module BurntToast -Path .\\\"\n"
    "     把生成的 BurntToast 目录放到客户机 WallpaperAgent.exe 旁边（或跑\n"
    "     WallpaperAgent.exe --install-toast-module --source <那个目录>）\n"
    "  4) 本工具目录里的 prepare_toast_module.bat 就是干第 3 步的，跑一下它会\n"
    "     把模块下载到 .\\BurntToast\\（已裁剪掉 20 MB 用不到的 DLL），跟 exe 一起拷过去即可"
)

_install_lock = threading.Lock()


def ensure_modules(configured: str = "", log=None,
                   auto_install: bool = True) -> tuple[bool, str]:
    """确保 BurntToast 能用；没有就装一次。

    为什么运行时也要查一次：被控端如果只是拷过去跑（没执行过 --install），
    或者客户机上装失败过，那它一直都没有这个模块 —— 表现就是控制端发送时
    报「未能加载指定模块 BurntToast」。这里补上，并给出可操作的说明。
    """
    log = log or (lambda msg, level="info": None)
    if find_module_dir(configured) or module_installed(configured):
        return True, "BurntToast 已就绪"
    if not auto_install:
        return False, MODULE_HELP
    with _install_lock:                      # 多条通知同时来，只装一次
        if find_module_dir(configured) or module_installed(configured):
            return True, "BurntToast 已就绪"
        log("本机没有 BurntToast 通知模块，正在自动安装（首次会比较慢）…", "warn")
        ok, msg = install_module()
        if ok:
            return True, f"{msg}（自动安装）"
        return False, f"{msg}\n{MODULE_HELP}"


def module_error_hint(err: str) -> str:
    """把 PowerShell 的模块加载错误换成一句能照着做的说明。"""
    text = err or ""
    if ("BurntToast" in text and ("未能加载" in text or "not find" in text.lower()
                                  or "找不到" in text or "CommandNotFound" in text
                                  or "not recognized" in text.lower())):
        return MODULE_HELP
    return text


_PS_SCRIPT = r"""# Win 壁纸推送 —— 通知渲染（由被控端自动生成，不要手改）
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File toast.ps1 -Spec <规格.json> -Result <结果.json> [-ModuleDir <目录>] [-AppId <身份>] [-DryRun]
#
# 为什么不用 New-BurntToastNotification 一步到位：
#   它内部走的是 Microsoft.Toolkit 的 CreateToastNotifier()（无参重载），通知
#   身份取的是**当前进程** —— 结果是通知以「Windows PowerShell」的名义弹出，
#   而且 Toolkit 还会把 AppId 的注册改写成 powershell。
#   所以这里拆成两步：用 BurntToast 的构建器拼内容（New-BTText / New-BTImage /
#   New-BTButton…），再用**带 AppId 的** CreateToastNotifier 提交 —— 通知就显示
#   成我们自己注册的名字和图标了。
#
# -DryRun：只把最终 XML 写进结果文件、**不真的弹**（自测用它来核对
#   duration / scenario / loop 这些属性，免得测试时满屏弹窗、铃声循环）。
param(
    [Parameter(Mandatory=$true)][string]$Spec,
    [Parameter(Mandatory=$true)][string]$Result,
    [string]$ModuleDir = '',
    [string]$AppId = '',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
function Write-Result($obj) {
    $json = $obj | ConvertTo-Json -Compress -Depth 6
    [System.IO.File]::WriteAllText($Result, $json, (New-Object System.Text.UTF8Encoding($false)))
}
try {
    if ($ModuleDir -and (Test-Path (Join-Path $ModuleDir 'BurntToast.psd1'))) {
        Import-Module (Join-Path $ModuleDir 'BurntToast.psd1') -Force -ErrorAction Stop
    } else {
        Import-Module BurntToast -Force -ErrorAction Stop
    }

    $s = [System.IO.File]::ReadAllText($Spec, [System.Text.Encoding]::UTF8) | ConvertFrom-Json

    # ---------------- 内容主体
    $children = @()
    foreach ($line in $s.text) { $children += (New-BTText -Text $line) }
    if ($s.progress -and $s.progress.status) {
        $bar = @{ Status = $s.progress.status }
        if ($s.progress.title) { $bar.Title = $s.progress.title }
        if ([double]$s.progress.value -lt 0) { $bar.Indeterminate = $true }
        else { $bar.Value = [double]$s.progress.value }
        $children += (New-BTProgressBar @bar)
    }

    $bindArgs = @{ Children = $children }
    if ($s.app_logo)   { $bindArgs.AppLogoOverride = (New-BTImage -Source $s.app_logo -AppLogoOverride) }
    if ($s.hero_image) { $bindArgs.HeroImage = (New-BTImage -Source $s.hero_image -HeroImage) }
    if ($s.attribution){ $bindArgs.Attribution = $s.attribution }

    $contentArgs = @{ Visual = (New-BTVisual -BindingGeneric (New-BTBinding @bindArgs)) }

    # ---------------- 声音 / 静音
    # 「一直显示到用户处理」= 闹钟场景，声音必须循环（Windows 靠它把横幅留在屏幕上）
    $alarm = ($s.duration -eq 'until_dismissed')
    if ($s.silent -and -not $alarm) {
        $contentArgs.Audio = (New-BTAudio -Silent)
    } elseif ($s.sound) {
        $plain = @('Default', 'IM', 'Mail', 'Reminder', 'SMS')
        if ($plain -contains $s.sound) { $uri = "ms-winsoundevent:Notification.$($s.sound)" }
        else { $uri = "ms-winsoundevent:Notification.Looping.$($s.sound)" }
        if ($alarm) { $contentArgs.Audio = (New-BTAudio -Source $uri -Loop) }
        else        { $contentArgs.Audio = (New-BTAudio -Source $uri) }
    }

    # ---------------- 按钮（最多 5 个，被控端已经校验过只允许 http/https）
    $btns = @()
    foreach ($b in $s.buttons) {
        if ($b.activation_type -eq 'system') {
            if ($b.arguments -eq 'snooze') { $btns += (New-BTButton -Snooze) }
            else { $btns += (New-BTButton -Dismiss) }
        } else {
            $btns += (New-BTButton -Content $b.content -Arguments $b.arguments)
        }
    }
    if ($btns.Count -gt 0 -and $s.snooze) { $contentArgs.Actions = (New-BTAction -Buttons $btns -SnoozeAndDismiss) }
    elseif ($btns.Count -gt 0)            { $contentArgs.Actions = (New-BTAction -Buttons $btns) }
    elseif ($s.snooze)                    { $contentArgs.Actions = (New-BTAction -SnoozeAndDismiss) }

    if ($s.header -and $s.header.title) {
        $contentArgs.Header = (New-BTHeader -Id $s.header.id -Title $s.header.title)
    }

    $content = New-BTContent @contentArgs

    # ---------------- 提交（带我们自己的 AppId）
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($content.GetContent())

    # BurntToast 1.1.0 + Toolkit 7.1.0 的坑：New-BTText 会把每行文字**包进大括号**
    # （字面量的大括号还会翻倍：'{already}' 变成 '{{already}}'），Windows 把 {} 原样
    # 显示出来 —— 用户看到的就是"标题/正文/第三行 两边多了 {}"。
    # 这里按顺序把我们的文字原样写回前 N 个 text 节点（跳过 attribution 那个）。
    try {
        $textNodes = $xml.GetElementsByTagName('text')
        $idx = 0
        foreach ($node in $textNodes) {
            if ($node.GetAttribute('placement')) { continue }
            if ($idx -ge $s.text.Count) { break }
            $node.InnerText = [string]$s.text[$idx]
            $idx++
        }
    } catch { }

    # 「紧急」= 给 toast 加 scenario="urgent"，这样能穿透专注助手。
    # （Toolkit 的 ToastScenario 枚举里没有 Urgent，BurntToast 自己也是直接改 XML 的。）
    if ($s.urgent) {
        try { $xml.GetElementsByTagName('toast')[0].SetAttribute('scenario', 'urgent') } catch { }
    }

    # ---------------- 横幅停留时长
    # Windows 的 toast 只有短（约 5 秒）/ 长（约 25 秒）两档；「一直显示到用户处理」
    # 是闹钟场景（scenario="alarm" + 循环音频）—— 那种通知不会自动消失，
    # 必须用户点掉/稍后提醒（见上面的 Audio 处理）。
    $toastEl = $xml.GetElementsByTagName('toast')[0]
    if ($s.duration -eq 'long' -or $s.duration -eq 'until_dismissed') {
        try { $toastEl.SetAttribute('duration', 'long') } catch { }
    }
    if ($s.duration -eq 'until_dismissed') {
        try { $toastEl.SetAttribute('scenario', 'alarm') } catch { }
    }

    if ($DryRun) {
        Write-Result @{ ok = $true; dry_run = $true; xml = $xml.GetXml() }
        exit 0
    }

    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null

    if ($AppId) {
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId)
    } else {
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier()
    }

    $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
    if ($s.unique_id) {
        $toast.Tag = $s.unique_id
        $toast.Group = $s.unique_id
    }
    if ($s.suppress_popup) { $toast.SuppressPopup = $true }
    if ($s.expire_minutes -and [int]$s.expire_minutes -gt 0) {
        $toast.ExpirationTime = [DateTimeOffset]::Now.AddMinutes([int]$s.expire_minutes)
    }

    $notifier.Show($toast)
    Write-Result @{ ok = $true; appid = $AppId }
    exit 0
} catch {
    $msg = $_.Exception.Message
    if ($_.Exception -is [System.Management.Automation.CommandNotFoundException]) {
        $msg = 'BurntToast 模块不可用：' + $msg
    }
    Write-Result @{ ok = $false; err = $msg }
    exit 1
}
"""


def ensure_script() -> str:
    """把 toast.ps1 写到磁盘（内容有变化才重写），返回路径。"""
    path = script_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        need = True
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                old = f.read()
            if old == _PS_SCRIPT:
                need = False
        if need:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(_PS_SCRIPT)
            os.replace(tmp, path)
    except OSError:
        return ""
    return path


def render(spec: dict, module_dir: str = "", timeout: float = 30.0,
           dry_run: bool = False) -> tuple[bool, str] | tuple[bool, str, str]:
    """按规格弹一条通知。返回 (成功, 说明)。

    图片路径由调用方在 spec 里给成绝对路径（本函数只负责调用 PowerShell）。
    dry_run=True 时只拼出最终 XML 并返回 (True, 说明, xml) —— 不真的弹，
    自测用它核对 duration / scenario / loop 这些属性。
    """
    if sys.platform != "win32":
        return (False, "只有 Windows 支持 Toast 通知", "") if dry_run \
            else (False, "只有 Windows 支持 Toast 通知")
    ps = _powershell()
    if not ps:
        return (False, "找不到 powershell.exe", "") if dry_run else (False, "找不到 powershell.exe")
    script = ensure_script()
    if not script:
        msg = "toast.ps1 写不出去（检查 %LOCALAPPDATA% 权限）"
        return (False, msg, "") if dry_run else (False, msg)

    # 关键：必须把「实际找到的模块目录」显式交给 PowerShell。
    # 只传配置值是不够的 —— exe 旁边那份离线模块、exe 内置的那份、自动装的缓存，
    # 都不在 PowerShell 默认的模块搜索路径里，不指定就会报
    # 「未能加载指定模块 BurntToast」，哪怕 Python 这边明明找得到。
    module_dir = module_dir or find_module_dir()

    tmpdir = tempfile.mkdtemp(prefix="wpp_toast_")
    spec_path = os.path.join(tmpdir, "spec.json")
    result_path = os.path.join(tmpdir, "result.json")
    try:
        with open(spec_path, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False)
        args = [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", script, "-Spec", spec_path, "-Result", result_path,
                "-AppId", APP_ID]
        if module_dir:
            args += ["-ModuleDir", module_dir]
        if dry_run:
            args += ["-DryRun"]
        proc = subprocess.run(args, capture_output=True, timeout=timeout,
                              creationflags=_NO_WINDOW)
        data = {}
        try:
            with open(result_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            data = {}
        if data.get("ok"):
            if dry_run:
                return True, "已生成 XML（没有真的弹）", str(data.get("xml") or "")
            return True, "已弹出通知"
        err = data.get("err") or ""
        if not err:
            tail = (proc.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
            err = tail[-1] if tail else f"PowerShell 退出码 {proc.returncode}"
        hint = module_error_hint(err)
        return (False, hint, "") if dry_run else (False, hint)
    except subprocess.TimeoutExpired:
        msg = f"渲染超时（>{timeout:.0f} 秒）"
        return (False, msg, "") if dry_run else (False, msg)
    except Exception as e:
        msg = f"渲染失败：{e}"
        return (False, msg, "") if dry_run else (False, msg)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def self_test(configured: str = "") -> str:
    """一行状态，给 --selftest / --status / 界面用。"""
    if not IS_WINDOWS:
        return "通知：非 Windows，不支持"
    if not powershell_available():
        return "通知：找不到 powershell.exe，无法使用"
    found = find_module_dir(configured)
    if found:
        where = module_origin(found)
        state = f"通知：BurntToast 就绪（{where}）"
    elif module_installed():
        state = "通知：BurntToast 已安装（系统 PowerShell 模块）"
    else:
        return ("通知：缺少 BurntToast 模块 —— 运行 --install-toast-module 安装，"
                "或把 BurntToast 目录放到 exe 旁边")
    if not app_id_registered():
        state += "；通知身份未注册（会以「Windows PowerShell」的名义弹出）"
    return state
