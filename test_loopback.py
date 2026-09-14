#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端回环自测

不联网、不开界面，在**同一个进程**里同时跑起控制端和被控端，验证整条链路：

    广播公告 → TCP 回连下载 → sha256 校验 → 调用 Win32 接口换壁纸 → 回报控制端

运行：
    python test_loopback.py

注意：测试会真的更换一次桌面壁纸，**结束后自动恢复成测试前的壁纸和契合度**。
退出码 0 = 全部通过，1 = 有失败项。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

import protocol as P
import wallpaper

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, desc: str, detail: str = "") -> bool:
    RESULTS.append((ok, desc))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {desc}"
    if detail:
        line += f"   ({detail})"
    print(line, flush=True)
    return ok


def make_test_image(path: str) -> None:
    """生成一张容易辨认的测试壁纸（没有 Pillow 就退化成手写 BMP）。"""
    try:
        from PIL import Image, ImageDraw

        w, h = 1920, 1080
        im = Image.new("RGB", (w, h))
        d = ImageDraw.Draw(im)
        for y in range(h):
            t = y / h
            d.line(
                [(0, y), (w, y)],
                fill=(int(24 + 40 * t), int(70 + 110 * t), int(170 - 40 * t)),
            )
        d.rectangle([50, 50, w - 50, h - 50], outline=(255, 255, 255), width=8)
        d.rectangle([80, 80, w - 80, 300], fill=(255, 255, 255))
        d.text((120, 150), "WIN WALLPAPER PUSH  --  LOOPBACK TEST OK",
               fill=(20, 40, 90))
        im.save(path, "JPEG", quality=90)
        return
    except Exception:
        pass

    # 兜底：手写一张 2x2 的 24 位 BMP，不依赖任何第三方库
    import struct

    w = h = 2
    row = b"\x00\x00" + b"\x11\x22\xcc" * w      # BGR，行按 4 字节对齐
    pixels = row * h
    header = b"BM" + struct.pack("<IHHI", 14 + 40 + len(pixels), 0, 0, 14 + 40)
    dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    with open(path, "wb") as f:
        f.write(header + dib + pixels)


def main() -> int:
    print("=" * 66)
    print("  Win 壁纸推送 —— 端到端回环自测")
    print("=" * 66)

    print("\n[0] 环境检查")
    check(wallpaper.IS_WINDOWS, "运行在 Windows 上", sys.platform)
    if not wallpaper.IS_WINDOWS:
        print("\n非 Windows 环境，无法继续测试。")
        return 1
    check(True, "壁纸接口可用", wallpaper.self_test())

    original_wall = wallpaper.current_wallpaper()
    original_style = wallpaper.get_style()
    print(f"      测试前壁纸：{original_wall or '（无）'}")
    print(f"      测试前契合度：{original_style}")

    tmpdir = tempfile.mkdtemp(prefix="wpp_test_")
    img_path = os.path.join(tmpdir, "test_wallpaper.jpg")
    wall_dir = os.path.join(tmpdir, "applied")

    # 延迟导入，确保上面的环境检查先跑
    from agent import AgentCore
    from controller import ControllerCore

    agent_logs: list[str] = []
    ctrl_logs: list[str] = []

    agent_cfg = {
        "udp_port": P.UDP_PORT,
        "style": "填充",
        "keep": 5,
        "wallpaper_dir": wall_dir,
        "retry": 3,
    }
    ctrl_cfg = {
        "udp_port": P.UDP_PORT,
        "tcp_port": P.TCP_PORT,
        "reply_port": P.REPLY_PORT,
        "style": "填充",
        "targets": [],
    }

    agent = AgentCore(agent_cfg, log=lambda m, l="info": agent_logs.append(f"[{l}] {m}"))
    ctrl = ControllerCore(ctrl_cfg, log=lambda m, l="info": ctrl_logs.append(f"[{l}] {m}"))

    exit_code = 1
    try:
        print("\n[1] 生成测试图片")
        make_test_image(img_path)
        size = os.path.getsize(img_path)
        check(size > 0, "测试图片已生成", f"{size // 1024} KB")

        print("\n[2] 启动被控端")
        agent.start()
        time.sleep(0.4)
        check(agent._sock is not None, f"被控端已监听 UDP {P.UDP_PORT}")

        print("\n[3] 启动控制端")
        ctrl.start()
        time.sleep(0.3)
        check(ctrl._sock is not None, f"控制端已监听广播口 {P.UDP_PORT}")
        check(ctrl._reply_sock is not None,
              f"控制端已监听回执口 {P.REPLY_PORT}",
              "同机测试时确认能收到的关键")
        check(ctrl._srv is not None, f"控制端已监听文件传输口 TCP {P.TCP_PORT}")

        print("\n[4] 广播扫描在线设备")
        ctrl.scan()
        deadline = time.time() + 6
        while time.time() < deadline and not ctrl.online_devices():
            time.sleep(0.2)
        online = ctrl.online_devices()
        detail = "；".join(f"{d.get('ip')} / {d.get('host')}" for _k, d in online) or "（无）"
        # 不能断言"只有 1 台"：这是真的在扫局域网，别人机器上的被控端也会被发现
        # （改进探测范围之后实测就多扫到过一台，那条断言因此挂过一次）
        import socket as _sock

        mine = [d for _k, d in online if d.get("host") == _sock.gethostname()]
        check(bool(mine), "扫描到本机被控端（多网卡按计算机名合并）",
              f"共 {len(online)} 台：{detail}")

        print("\n[5] 广播推送壁纸")
        task = ctrl.push(img_path, "填充")
        check(bool(task), "公告已广播", f"任务号 {task}")

        print("\n[6] 等待被控端下载并应用")
        deadline = time.time() + 25
        acked = False
        while time.time() < deadline:
            with ctrl._dev_lock:
                acks = dict(ctrl._acks.get(task, {}))
            if acks:
                acked = True
                break
            time.sleep(0.2)

        check(acked, "收到被控端执行回执")
        with ctrl._dev_lock:
            acks = dict(ctrl._acks.get(task, {}))
        for _key, (ok, err, _ts, aip) in sorted(acks.items()):
            check(ok, f"被控端 {aip} 报告应用成功", err or "无错误")

        print("\n[7] 校验壁纸真的被换掉了")
        time.sleep(0.8)
        now_wall = wallpaper.current_wallpaper()
        check(bool(now_wall), "注册表 Wallpaper 已更新", now_wall)
        check(os.path.normcase(now_wall) != os.path.normcase(original_wall or ""),
              "壁纸路径已发生变化")
        check(os.path.isfile(now_wall), "新壁纸文件确实存在于磁盘上")
        check(wallpaper.get_style() == "填充", "契合度已写入注册表",
              f"当前为「{wallpaper.get_style()}」")
        check(agent.applied_count == 1, "被控端计数 +1",
              f"applied_count={agent.applied_count}")

        print("\n[8] 校验 sha256 完整性")
        import hashlib

        with open(now_wall, "rb") as f:
            got = hashlib.sha256(f.read()).hexdigest()
        with open(img_path, "rb") as f:
            want = hashlib.sha256(f.read()).hexdigest()
        check(got == want, "落地文件与原图 sha256 完全一致")

        print("\n[9] 重复广播去重")
        before = agent.applied_count
        ctrl.push(img_path, "填充")          # 换了 task，应该再来一次
        time.sleep(2.5)
        check(agent.applied_count == before + 1,
              "新任务的广播会再次被处理（去重只针对同一个 task）",
              f"{before} -> {agent.applied_count}")

        print("\n[9b] 远程改被控端「通知上显示的应用名」")
        # 这条会真的改 HKCU 里的 AppId 显示名和开始菜单快捷方式，测完必须还原
        import protocol as PP
        import toast as toastmod

        def read_display() -> str:
            import winreg

            try:
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        rf"Software\Classes\AppUserModelId\{toastmod.APP_ID}") as k:
                    return str(winreg.QueryValueEx(k, "DisplayName")[0])
            except OSError:
                return ""

        orig_display = read_display()
        try:
            # 名字校验（被控端会独立再校验一遍，不信任网络来的内容）
            okv, cleanv = PP.normalize_app_name("  多余   空格  ")
            check(okv and cleanv == "多余 空格", "名字里的多余空白会被压成一个", repr(cleanv))
            for raw, label in (("x" * 50, "太长"), ("坏\x00名字", "控制字符")):
                okb, msg = PP.normalize_app_name(raw)
                check(not okb, f"非法名字被拒（{label}）", msg)

            task2 = ctrl.push_app_name("回环测试专用名")
            deadline = time.time() + 10
            acks2: dict = {}
            while time.time() < deadline:
                with ctrl._dev_lock:
                    acks2 = dict(ctrl._acks.get(task2, {}))
                if acks2:
                    break
                time.sleep(0.2)
            check(bool(acks2), "改名指令收到回执",
                  "；".join(f"{v[3]}:{v[1]}" for v in acks2.values()) or "（无）")
            check(all(v[0] for v in acks2.values()) if acks2 else False,
                  "被控端报告改名成功",
                  "；".join(v[1] for v in acks2.values())[:80])
            check(read_display() == "回环测试专用名",
                  "注册表里的显示名真的变了（通知顶部显示的就是它）", read_display())
            check(toastmod.app_display_name() == "回环测试专用名",
                  "被控端进程内也认这个名字")

            # 关掉开关就该被拒
            agent.cfg["allow_remote_app_name"] = False
            task3 = ctrl.push_app_name("不该生效的名字")
            deadline = time.time() + 10
            acks3: dict = {}
            while time.time() < deadline:
                with ctrl._dev_lock:
                    acks3 = dict(ctrl._acks.get(task3, {}))
                if acks3:
                    break
                time.sleep(0.2)
            check(any((not v[0]) and "禁止" in v[1] for v in acks3.values()),
                  "配置里禁止远程改名时会被拒绝，并说明原因",
                  "；".join(v[1] for v in acks3.values())[:80] or "（没有回执）")
            check(read_display() == "回环测试专用名", "被拒绝时不会改动注册表")
            agent.cfg["allow_remote_app_name"] = True

            # 空名字 = 恢复默认
            task4 = ctrl.push_app_name("")
            deadline = time.time() + 10
            acks4: dict = {}
            while time.time() < deadline:
                with ctrl._dev_lock:
                    acks4 = dict(ctrl._acks.get(task4, {}))
                if acks4:
                    break
                time.sleep(0.2)
            check(bool(acks4) and all(v[0] for v in acks4.values()),
                  "空名字 = 恢复默认")
            check(read_display() == orig_display, "恢复成测试前那个名字", read_display())
        finally:
            try:
                if read_display() != orig_display:
                    toastmod.set_app_display_name(
                        "" if orig_display == toastmod.DEFAULT_APP_NAME else orig_display)
                    toastmod.ensure_app_id()
            except Exception:
                pass

        exit_code = 0

    except Exception as e:
        import traceback

        traceback.print_exc()
        check(False, "测试过程抛出异常", str(e))
        exit_code = 1

    finally:
        print("\n[10] 清理并恢复原壁纸")
        try:
            ctrl.stop()
            agent.stop()
        except Exception:
            pass
        time.sleep(0.3)

        if original_wall and os.path.isfile(original_wall):
            try:
                wallpaper.set_wallpaper(original_wall, original_style)
                restored = wallpaper.current_wallpaper()
                same = os.path.normcase(restored) == os.path.normcase(original_wall)
                check(same, "已恢复原壁纸", restored)
            except Exception as e:
                check(False, "恢复原壁纸失败", str(e))
        else:
            print("      （测试前没有可恢复的壁纸，跳过）")

        import shutil

        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

        if exit_code != 0:
            print("\n--- 控制端日志 ---")
            for line in ctrl_logs[-30:]:
                print("   ", line)
            print("--- 被控端日志 ---")
            for line in agent_logs[-30:]:
                print("   ", line)

    passed = sum(1 for ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("\n" + "=" * 66)
    print(f"  结果：{passed} 项通过，{failed} 项失败")
    print("=" * 66)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass
    sys.exit(main())
