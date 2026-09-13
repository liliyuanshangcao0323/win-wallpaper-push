#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载 WiX v3.14 工具链（candle.exe / light.exe / 扩展库）到指定目录。

为什么从 NuGet 下载
-------------------
GitHub Releases 的实际文件由 objects.githubusercontent.com 下发。很多企业网络
允许访问 github.com 本身，却把这个 CDN 域名挡掉，表现是「元数据能取、文件下载
超时」。NuGet 是微软官方包仓库，做 Windows 部署的环境基本都会放行。

同一台受限网络机器上的实测结果：

    GitHub Releases        ->  超时（objects.githubusercontent.com 不可达）
    Windows PowerShell     ->  连不上
    系统自带 curl.exe       ->  连不上
    NuGet api.nuget.org    ->  正常，41 MB 直接下完
    PyPI                   ->  正常

.nupkg 本身就是个 zip，解压即用。

所以这里 **只走 NuGet 一个来源**。再加一个 GitHub 后备并不能提高成功率
（那个场景基本不存在），反而会在两条路都失败时多等两轮超时，让用户更晚
看到真正有用的报错。—— 内网有 NuGet 镜像的，用 --url 指过去即可。

为什么用 Python 而不是 PowerShell
---------------------------------
Python 是本项目的硬依赖（打包 exe 就要用），所以用它不引入任何新东西；
解压也直接用标准库 zipfile 完成，不需要 Expand-Archive / tar / curl。

为什么选 WiX v3.14
------------------
它的命令行工具直接跑在 Windows 自带的 .NET Framework 4.x 上。
WiX v4/v5/v6 需要 .NET SDK（约 200 MB），在很多机器上装不上。

用法：
    python get_wix.py --dest .tools/wix3
    python get_wix.py --dest .tools/wix3 --url https://内网镜像/wix.3.14.1.nupkg
"""

from __future__ import annotations

import argparse
import os
import shutil
import ssl
import sys
import time
import urllib.request
import zipfile

# 固定版本，不跟随最新：构建要可重复，浮动版本会某天突然编不过
NUGET_URL = (
    "https://api.nuget.org/v3-flatcontainer/wix/3.14.1/wix.3.14.1.nupkg"
)

MIN_SIZE = 10 * 1024 * 1024      # 正常约 41 MB，太小肯定是被网关拦截了
TIMEOUT = 60
ATTEMPTS_PER_ROUTE = 2

REQUIRED = (
    "candle.exe",
    "light.exe",
    "WixFirewallExtension.dll",
    "WixUtilExtension.dll",
    "WixUIExtension.dll",
)


def wininet_proxy() -> str | None:
    """读取 WinINET（IE 选项）里配置的代理服务器地址。

    即使 ProxyEnable=0 也照样返回：在受限网络里那条记录往往是唯一的出口，
    而且只在直连失败之后才会用到。
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            server = str(winreg.QueryValueEx(k, "ProxyServer")[0])
    except Exception:
        return None

    server = server.strip()
    if not server:
        return None
    if "://" not in server:
        server = "http://" + server
    return server


def candidate_routes() -> list[tuple[str, dict]]:
    """返回依次尝试的下载路线：[(说明, ProxyHandler 参数), ...]。

    这里才是值得保留的冗余 —— 同一个来源，换不同的出口去连。
    """
    routes: list[tuple[str, dict]] = []
    seen: set[str] = set()

    def add(name: str, proxy: dict) -> None:
        key = repr(sorted(proxy.items()))
        if key not in seen:
            seen.add(key)
            routes.append((name, proxy))

    add("直连", {})                     # ① 直连

    env = {}                            # ② 环境变量里显式配置的代理
    for scheme, names in (("http", ("HTTP_PROXY", "http_proxy")),
                          ("https", ("HTTPS_PROXY", "https_proxy"))):
        for n in names:
            v = os.environ.get(n)
            if v:
                env[scheme] = v
                break
    if env:
        add("环境变量代理", env)

    srv = wininet_proxy()               # ③ 系统（WinINET）里配置的代理
    if srv:
        add(f"系统代理 {srv}", {"http": srv, "https": srv})

    return routes


def download(url: str, dst: str, proxy: dict) -> None:
    """下载 url 到 dst。proxy 传 {} 表示强制直连。"""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(proxy),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    opener.addheaders = [("User-Agent", "WinWallpaperPush-build")]

    with opener.open(url, timeout=TIMEOUT) as resp:
        expected = int(resp.headers.get("Content-Length") or 0)
        got = 0
        with open(dst, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
        if expected and got != expected:
            raise IOError(f"下载不完整：收到 {got} / 期望 {expected} 字节")


def fetch(url: str, dst: str) -> bool:
    """按候选路线依次尝试下载，成功返回 True。"""
    for name, proxy in candidate_routes():
        for attempt in range(1, ATTEMPTS_PER_ROUTE + 1):
            try:
                print(f"    路线「{name}」第 {attempt} 次...", flush=True)
                if os.path.exists(dst):
                    os.remove(dst)
                download(url, dst, proxy)

                size = os.path.getsize(dst) if os.path.exists(dst) else 0
                if size < MIN_SIZE:
                    raise IOError(f"文件只有 {size:,} 字节，明显不对（可能被网关拦截）")
                print(f"      成功，{size:,} 字节")
                return True
            except Exception as e:
                print(f"      失败：{e}", flush=True)
                if attempt < ATTEMPTS_PER_ROUTE:
                    time.sleep(1.5)
    return False


def extract_tools(zip_path: str, dest: str) -> None:
    """解压并把工具摊平到 dest。

    NuGet 包里的工具在 tools/ 下，所以先整包解到临时目录，找到装着
    candle.exe 的那一层，再把它的内容搬过去（而不是写死 tools/ 这个路径）。
    """
    staging = dest + ".staging"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(staging)

    holder = None
    for root, _dirs, files in os.walk(staging):
        if "candle.exe" in [f.lower() for f in files]:
            holder = root
            break
    if holder is None:
        raise RuntimeError("解压后没有找到 candle.exe，下载内容可能不完整")

    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    for item in os.listdir(holder):
        shutil.move(os.path.join(holder, item), os.path.join(dest, item))

    shutil.rmtree(staging, ignore_errors=True)


def manual_instructions(url: str, dest: str) -> None:
    print()
    print("=" * 70)
    print("  无法自动下载 WiX 工具链（网络被限制）")
    print()
    print("  手工处理办法：")
    print()
    print("    1. 在一台能上网的机器上用浏览器打开：")
    print(f"         {url}")
    print("       （或打开 https://www.nuget.org/packages/WiX/3.14.1 下载）")
    print()
    print("    2. 下载得到的 .nupkg 就是个普通 zip，解压它")
    print()
    print("    3. 找到含 candle.exe 的那一层（在 tools/ 目录里），")
    print("       把其中全部文件放到：")
    print(f"         {dest}")
    print()
    print("    4. 重新运行 build_msi.bat，它会跳过下载直接编译")
    print()
    print("  提示：内网有 NuGet 镜像的话，也可以直接指过去：")
    print(f"         python installer\\get_wix.py --dest .tools\\wix3 --url <镜像地址>")
    print("=" * 70)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="下载 WiX v3.14 工具链（来源：NuGet）")
    ap.add_argument("--dest", required=True, help="解压目标目录")
    ap.add_argument("--url", default=NUGET_URL,
                    help="下载地址，默认走 api.nuget.org；可指向内网镜像")
    args = ap.parse_args(argv)

    dest = os.path.abspath(args.dest)
    candle = os.path.join(dest, "candle.exe")

    if os.path.isfile(candle):
        print(f"WiX 工具链已存在：{dest}")
        return 0

    print("下载 WiX 工具链（约 41 MB）...")
    print(f"  {args.url}")

    tmp = os.path.join(
        os.environ.get("TEMP") or os.path.expanduser("~"),
        f"wix314-{os.getpid()}.nupkg",
    )

    ok = False
    try:
        ok = fetch(args.url, tmp)
        if ok:
            print("  解压中...")
            extract_tools(tmp, dest)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass

    if not ok:
        manual_instructions(args.url, dest)
        return 1

    missing = [n for n in REQUIRED if not os.path.isfile(os.path.join(dest, n))]
    if missing:
        print(f"缺少必要文件：{', '.join(missing)}")
        manual_instructions(args.url, dest)
        return 1

    print(f"WiX 工具链就绪：{dest}")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass
    sys.exit(main())
