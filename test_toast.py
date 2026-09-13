#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知（Toast）功能自测

覆盖：
    1. 规格校验：正常规格通过；越界/危险规格被拒
       （远程脚本块、非 http 按钮、路径穿越、超量按钮、错声音……）
    2. BurntToast 探测（模块在哪、能不能用）
    3. 真实渲染：本机弹一条，用 Get-BTHistory 确认它进了通知中心
    4. 端到端：控制端广播 → 被控端拉图片 → 弹通知 → 回执 ok
    5. 被控端不盲信网络：伪造一条带危险按钮的通知，必须被拒绝并回报原因

运行： python test_toast.py
退出码 0 = 全过。会真的弹 1~2 条通知（本机），不会发给局域网其它机器。
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import zlib

import protocol as P
import toast as toastmod
import toastspec as TS

RESULTS: list[tuple[bool, str]] = []
TEST_PORT = 39571
REPLY_PORT = 39573
TCP_PORT = 39572


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


def make_png(path: str, w: int = 64, h: int = 64, rgb=(60, 130, 220)) -> None:
    """手写一张 PNG（不依赖 Pillow）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def history_has(unique_id: str) -> tuple[bool, str]:
    """确认通知真的进了通知中心，而且是**以我们自己的 AppId** 进的。

    注意：不能再像以前那样用 BurntToast 的 `Get-BTHistory` —— 那个查的是
    PowerShell（compat 层）那份历史；我们现在用显式的 AppId 提交，历史也归到
    我们自己名下，所以要按 AppId 查 WinRT 的历史。

    PowerShell 默认按控制台代码页输出，管道里读出来会乱码，所以先切 UTF-8。
    """
    ps = toastmod._powershell()
    if not ps:
        return False, "没有 powershell"
    app_id = toastmod.APP_ID
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] | Out-Null;"
        f"$h = [Windows.UI.Notifications.ToastNotificationManager]::History.GetHistory('{app_id}');"
        f"$f = @($h | Where-Object {{ $_.Tag -eq '{unique_id}' }});"
        "if ($f.Count -gt 0) { $c = $f[0].Content;"
        "  $txt = ($c.GetElementsByTagName('text') | ForEach-Object { $_.InnerText }) -join '|';"
        "  $act = @($c.GetElementsByTagName('action')).Count;"
        "  $scn = $c.DocumentElement.GetAttribute('scenario');"
        "  'FOUND|' + $txt + '|actions=' + $act + '|scenario=' + $scn }"
        " else { 'NONE' }"
    )
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, env=env,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    out = (proc.stdout or "").strip().replace("\r", "").replace("\n", "")
    return out.startswith("FOUND"), out or (proc.stderr or "").strip()


# ---------------------------------------------------------------- 1. 规格校验

def test_spec() -> None:
    print("\n[1] 通知规格校验")
    ok, spec, err = TS.normalize({
        "text": ["标题", "正文", "第三行"],
        "sound": "Alarm2", "urgent": True, "snooze": True,
        "attribution": "署名", "hero_image": "hero.png", "app_logo": "logo.png",
        "header": {"id": "1", "title": "更新"},
        "progress": {"title": "进度", "status": "40%", "value": 0.4},
        "buttons": [{"content": "打开", "arguments": "https://example.com"}],
        "unique_id": "abc", "expire_minutes": 60,
    })
    check(ok, "完整规格通过校验", TS.summarize(spec) if ok else err)
    check(spec.get("sound") == "Alarm2" and spec.get("urgent") is True,
          "声音 / 紧急等字段被保留")
    check(spec.get("text") == ["标题", "正文", "第三行"], "三行文字都保留")
    check("progress" in spec and spec["progress"]["value"] == 0.4, "进度条被保留")

    ok2, spec2, _ = TS.normalize({"text": ["只有标题"]})
    check(ok2 and "progress" not in spec2 and "header" not in spec2,
          "没填的进度条 / 分组不会带到被控端（空 progress 会让 PowerShell 报错）")

    ok3, spec3, _ = TS.normalize({"text": ["a", "b", "c", "d", "e"]})
    check(ok3 and len(spec3["text"]) == 3, "文字最多 3 行（多了截掉）")

    cases = [
        ({"text": ["x"], "activated_action": "calc.exe"}, "脚本块 ActivatedAction"),
        ({"text": ["x"], "dismissed_action": "calc.exe"}, "脚本块 DismissedAction"),
        ({"text": ["x"], "buttons": [{"content": "点", "arguments": "cmd.exe /c calc"}]},
         "按钮地址不是 http/https"),
        ({"text": ["x"], "buttons": [{"content": "点", "arguments": "javascript:alert(1)"}]},
         "按钮地址 javascript:"),
        ({"text": ["x"], "buttons": [{"content": "点", "arguments": "https://a.com",
                                      "activation_type": "system"}]},
         "系统按钮参数不在白名单"),
        ({"text": []}, "没有任何文字"),
        ({"text": ["x"], "sound": "不存在的声音"}, "声音取值非法"),
        ({"text": ["x"], "app_logo": "../../evil.png"}, "图片名路径穿越"),
        ({"text": ["x"], "app_logo": "payload.exe"}, "图片扩展名不对"),
        ({"text": ["x"], "expire_minutes": 999999}, "过期时间超限"),
        ({"text": ["x"], "buttons": [{"content": "b", "arguments": "https://a.com"}] * 6},
         "按钮超过 5 个"),
        ({"text": ["x"], "header": {"id": "a b", "title": "t"}}, "分组编号非法"),
        ({"text": ["x"], "progress": {"status": "s", "value": 3}}, "进度值越界"),
        ({"text": ["x"], "progress": {"title": "维护进度", "value": -1}},
         "进度条只写标题、没写状态文字"),
        ({"text": ["x"], "progress": {"title": "维护进度", "value": 0.5}},
         "固定进度没写状态文字"),
    ]
    bad = []
    for data, label in cases:
        okc, _s, _e = TS.normalize(data)
        if okc:
            bad.append(label)
    check(not bad, f"危险 / 越界规格全部被拒（{len(cases)} 个用例）",
          "、".join(bad) if bad else "含远程脚本、危险按钮、路径穿越等")

    okc, _s, errc = TS.normalize({"text": ["x"], "buttons": [
        {"content": "关闭", "arguments": "dismiss", "activation_type": "system"}]})
    check(okc, "系统按钮 dismiss 允许", errc)

    okc, _s, errc = TS.normalize({"text": ["x"], "hero_image": "屏幕截图 2026.png"})
    check(okc, "中文截图文件名可以用", errc)

    # 以前进度条没写状态文字时会偷偷补一句「处理中」，用户看到一行自己没写过的字，
    # 完全不知道哪来的。现在必须明确报错，绝不凭空造内容。
    okc, sp, _e = TS.normalize({"text": ["x"],
                                "progress": {"status": "正在下发 40%", "value": 0.4}})
    check(okc and sp.get("progress", {}).get("status") == "正在下发 40%",
          "进度条里的文字就是我们给的，不会被替换成别的")
    okc2, _s2, err2 = TS.normalize({"text": ["x"], "progress": {"title": "维护进度"}})
    check(not okc2 and "状态文字" in err2 and "处理中" not in err2,
          "没写状态文字时明确报错，而不是塞一句「处理中」", err2)


# ---------------------------------------------------------------- 2. 组件探测

def test_module() -> bool:
    print("\n[2] BurntToast 组件")
    if sys.platform != "win32":
        skip("BurntToast 探测", "非 Windows")
        return False
    line = toastmod.self_test()
    check(toastmod.powershell_available(), "能找到 powershell.exe")
    available = toastmod.module_installed()
    check(available, "BurntToast 模块可用", line[:90])
    if not available:
        skip("真实弹窗测试", "本机没有 BurntToast（先跑 --install-toast-module）")
        return False

    print("\n[2b] 通知署名（AppId 注册）")
    ok, msg = toastmod.ensure_app_id(log=lambda m, l="info": print("      ", m))
    check(ok, "注册通知身份成功", msg)
    check(toastmod.app_id_registered(), "HKCU 里能查到我们的 AppId", toastmod.APP_ID)
    ico = toastmod.icon_path()
    check(os.path.isfile(ico), "生成了自己的图标", f"{os.path.getsize(ico)} 字节" if os.path.isfile(ico) else "（缺）")
    check(os.path.isfile(toastmod.shortcut_path()),
          "开始菜单里有带 AppId 的快捷方式", toastmod.shortcut_path())
    # 关键：注册信息不能被别人（比如 Toolkit 的 compat 层）改回去
    ps = toastmod._powershell()
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        f"(Get-ItemProperty 'HKCU:\\Software\\Classes\\AppUserModelId\\{toastmod.APP_ID}').DisplayName"
    )
    proc = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    shown = (proc.stdout or "").strip()
    check(shown == toastmod.app_display_name(),
          "注册里的显示名是我们的名字（不是 powershell）", shown or "（读不到）")
    # 回归：快捷方式必须真的带上 AppUserModelId。
    # 以前这里没写进去（BurntToast 的 New-BTShortcut 不做这件事），Windows 就给
    # 快捷方式发了个 Microsoft.AutoGenerated.{GUID} 的自动身份 —— 我们的 AppId
    # 压根不在 shell 的应用表里，于是"改通知应用名"改了也白改（用户实测没效果）。
    aumid = toastmod.shortcut_aumid()
    check(aumid == toastmod.APP_ID,
          "开始菜单快捷方式上带着我们的 AppUserModelId（改名才会生效）",
          f"读到 {aumid!r}，期望 {toastmod.APP_ID!r}")

    # 关键回归：**客户机上的模块不在系统模块路径里**（我们把它打进 exe、临时释放），
    # 老代码用 BurntToast 的 New-BTShortcut 建快捷方式 -> Import-Module 失败 ->
    # 快捷方式建不出来 -> AppId 关联不上 -> "控制端下发改名"看着成功、屏幕上没变化。
    # 这里把 PSModulePath 指到空目录来复现"客户机"，要求照样能建好并带上 AppId。
    old_mp = os.environ.get("PSModulePath")
    lnk = toastmod.shortcut_path()
    had_lnk = os.path.isfile(lnk)
    marker_before = toastmod._read_app_marker()
    empty_mod = os.path.join(tempfile.gettempdir(), "wpp_no_modules_test")
    os.makedirs(empty_mod, exist_ok=True)
    try:
        os.environ["PSModulePath"] = empty_mod
        if had_lnk:
            os.remove(lnk)
        toastmod.set_app_display_name("客户机模拟名")
        ok_c, msg_c = toastmod.ensure_app_id()
        check(ok_c, "没有系统 BurntToast 模块时也能注册好通知身份（客户机场景）", msg_c)
        # 注意：名字变了，快捷方式路径也跟着变，得重新取一次
        made = toastmod.shortcut_path()
        check(os.path.isfile(made), "快捷方式照样建出来了（不再依赖 BurntToast）", made)
        check(toastmod.shortcut_aumid(made) == toastmod.APP_ID,
              "快捷方式上带着我们的 AppId（改名才会在屏幕上生效）",
              repr(toastmod.shortcut_aumid(made)))
    finally:
        if old_mp is None:
            os.environ.pop("PSModulePath", None)
        else:
            os.environ["PSModulePath"] = old_mp
        # 还原成默认名字 + 原来的状态
        toastmod.set_app_display_name("")
        toastmod.ensure_app_id()
        toastmod._write_app_marker(marker_before)
    check(toastmod.shortcut_aumid() == toastmod.APP_ID,
          "还原后快捷方式仍然是好的", repr(toastmod.shortcut_aumid()))

    # 改名留下的旧快捷方式要能自动清掉（不然开始菜单里越攒越多）
    folder = os.path.dirname(toastmod.shortcut_path())
    keep = os.path.basename(toastmod.shortcut_path())
    fake = ["wpp旧名甲.lnk", "wpp旧名乙.lnk"]
    marker_before = None
    try:
        for n in fake:
            with open(os.path.join(folder, n), "wb") as f:
                f.write(b"stub")
        marker_before = toastmod._read_app_marker()
        m = dict(marker_before)
        m["history"] = ["wpp旧名甲", "wpp旧名乙"]
        toastmod._write_app_marker(m)
        removed = toastmod.clean_stale_shortcuts()
        left = [n for n in fake if os.path.isfile(os.path.join(folder, n))]
        check(not left, "改名留下的旧快捷方式会被自动清理（当前那个留着）",
              "残留：" + "、".join(left) if left else f"清掉 {len(removed)} 个，{keep} 保留")
        check(os.path.isfile(os.path.join(folder, keep)), "当前快捷方式没被误删")
    finally:
        for n in fake:
            try:
                os.remove(os.path.join(folder, n))
            except OSError:
                pass
        if marker_before is not None:
            toastmod._write_app_marker(marker_before)
    return True


def test_text_braces() -> None:
    """通知文字不能被包上大括号（用户实测：标题/正文/第三行两边出现 {}）。

    根因：BurntToast 1.1.0 + Toolkit 7.1.0 的 `New-BTText` 会把文字包进 `{}`，
    字面量的大括号还会翻倍（`{already}` -> `{{already}}`），Windows 原样显示。
    我们的 toast.ps1 现在会把文字按原文写回 XML，这里用 dry-run 核对。
    """
    print("\n[2i] 通知文字不带大括号")
    import re as _re

    cases = [
        (["标题", "正文", "第三行"], "普通三行"),
        (["只有一行"], "单行"),
        (["带{大括号}的标题", "正文 {f(x)}"], "用户自己写的大括号要原样保留"),
    ]
    for lines, label in cases:
        okn, clean, err = TS.normalize({"text": lines, "unique_id": "wpp-brace-check",
                                        "expire_minutes": 1})
        if not okn:
            check(False, f"{label}：规格合法", err)
            continue
        res = toastmod.render(clean, dry_run=True)
        okr, msg, xml = res if len(res) == 3 else (res[0], res[1], "")
        if not okr:
            check(False, f"{label}：能生成 XML", msg)
            continue
        got = _re.findall(r"<text[^>]*>(.*?)</text>", xml)
        want = [x.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                for x in lines]
        check(got == want, f"{label}：文字就是原文", f"实际 {got}")
        check(not any("{" in g for g in got) or any("{" in w for w in lines),
              f"{label}：没有被额外包上大括号", f"实际 {got}")

    # 署名（attribution）不受影响
    okn, clean, _e = TS.normalize({"text": ["标题"], "attribution": "运维组",
                                   "unique_id": "wpp-brace-check"})
    _r = toastmod.render(clean, dry_run=True)
    xml = _r[2] if len(_r) == 3 else ""
    att = _re.search(r'<text placement="attribution"[^>]*>(.*?)</text>', xml)
    check(bool(att) and att.group(1) == "运维组", "署名不受影响（还是原文）",
          att.group(1) if att else "（没找到）")


# ---------------------------------------------------------------- 2h. 停留时长与署名

def test_duration_and_attribution() -> None:
    """停留时长（短/长/一直显示）与署名：规格校验 + 真正拼出来的 XML 属性。

    用 `render(dry_run=True)`：只生成 XML、**不真的弹**，所以自测不会满屏弹窗、
    更不会响循环铃声。
    """
    print("\n[2h] 停留时长 / 署名")
    cases = [
        ({"duration": "short"}, "short", None),
        ({"duration": "long"}, "long", 'duration="long"'),
        ({"duration": "长"}, "long", 'duration="long"'),
        ({"duration": "until_dismissed", "sound": "Alarm2"},
         "until_dismissed", 'scenario="alarm"'),
    ]
    for extra, want, attr in cases:
        spec = {"text": ["停留时长自测"], "unique_id": "wpp-dur-test"}
        spec.update(extra)
        ok, clean, err = TS.normalize(spec)
        if not ok:
            check(False, f"规格 {extra} 应当合法", err)
            continue
        check(clean.get("duration") == want, f"规格 duration = {want}", str(clean.get("duration")))
        res = toastmod.render(clean, dry_run=True)
        ok2, msg, xml = res if len(res) == 3 else (res[0], res[1], "")
        if not ok2:
            check(False, f"{want}：能生成 XML", msg)
            continue
        if attr:
            check(attr in xml, f"{want} 的 XML 里带了 {attr}",
                  (xml.splitlines()[0] if xml else ""))
        else:
            check('duration="long"' not in xml, "short 不加 duration 属性",
                  xml.splitlines()[0] if xml else "")
    # 「一直显示」要循环音频
    ok, clean, _e = TS.normalize({"text": ["x"], "duration": "until_dismissed",
                                  "sound": "Alarm3"})
    if ok:
        _r = toastmod.render(clean, dry_run=True)
        xml = _r[2] if len(_r) == 3 else ""
        check('loop="true"' in xml, "「一直显示」用的是循环音频（loop=true）",
              [l for l in xml.splitlines() if "audio" in l][:1] or "（没找到 audio）")
    # 冲突与非法值
    bad = [
        ({"text": ["x"], "duration": "until_dismissed", "urgent": True, "sound": "Alarm2"},
         "「紧急」+「一直显示」冲突"),
        ({"text": ["x"], "duration": "until_dismissed", "silent": True},
         "「一直显示」+「静音」冲突"),
        ({"text": ["x"], "duration": "until_dismissed", "sound": "Default"},
         "「一直显示」配了非循环音"),
        ({"text": ["x"], "duration": "一小时"}, "乱填的停留时长"),
    ]
    missed = []
    for data, label in bad:
        okc, _s, errc = TS.normalize(data)
        if okc:
            missed.append(label)
        else:
            check(True, f"{label} → 被拒（并说明原因）", errc[:70])
    check(not missed, "该拒的都拒了", "、".join(missed) if missed else f"{len(bad)} 个用例")

    # 署名（通知底部那行小字）：能自定义、能显示在 XML 里
    ok, clean, err = TS.normalize({"text": ["署名测试"], "attribution": "IT 运维组"})
    check(ok and clean.get("attribution") == "IT 运维组", "署名可以自定义", err or "")
    if ok:
        _r = toastmod.render(clean, dry_run=True)
        xml = _r[2] if len(_r) == 3 else ""
        check("IT 运维组" in xml, "署名出现在生成的 XML 里")
    # 通知顶部显示的那个名字（AppId 显示名）也能自定义
    old = toastmod.set_app_display_name("IT 运维通知")
    check(old == "IT 运维通知", "能把通知显示名改成自定义的", old)
    check(toastmod.shortcut_name() == "IT 运维通知.lnk",
          "开始菜单快捷方式跟着改名", toastmod.shortcut_name())
    check("IT 运维通知" in toastmod.app_id_state(),
          "状态行显示的是自定义名字", toastmod.app_id_state()[:60])
    toastmod.set_app_display_name("")      # 还原
    check(toastmod.app_display_name() == toastmod.DEFAULT_APP_NAME,
          "清空后又回到默认名字", toastmod.app_display_name())


# ---------------------------------------------------------------- 3. 真实渲染

def test_missing_module() -> None:
    """客户机上没有 BurntToast 时：必须给一句能照着做的说明，而不是干巴巴的模块错误。

    这一段用 PSModulePath 把 PowerShell 的模块搜索路径指到空目录来模拟
    「这台机器没装模块」—— 不动本机真实环境。
    """
    print("\n[2c] 客户机没装 BurntToast 时的表现")
    empty = os.path.join(tempfile.gettempdir(), "wpp_empty_modules")
    os.makedirs(empty, exist_ok=True)
    old_env = os.environ.get("PSModulePath")
    old_find = toastmod.find_module_dir
    os.environ["PSModulePath"] = empty
    # 本机 exe 旁边就有一份离线模块，所以还要把它也屏蔽掉，才是「客户机什么都没有」
    toastmod.find_module_dir = lambda configured="": ""      # type: ignore[assignment]
    try:
        check(not toastmod.module_installed(),
              "把模块搜索路径清空后，探测到「没装模块」")
        ok, msg = toastmod.ensure_modules(auto_install=False)
        check(not ok, "ensure_modules 报告不可用")
        check("--install-toast-module" in msg and "Save-Module" in msg,
              "给出的说明里包含可照做的两条命令", msg.splitlines()[0])
        check("prepare_toast_module.bat" in msg, "并且提到了离线打包脚本")
        hint = toastmod.module_error_hint(
            "未能加载指定模块“BurntToast”，因为在任何目录模块中都没找到有效模块文件。")
        check("--install-toast-module" in hint,
              "PowerShell 那句模块错误会被换成可操作的说明（就是用户遇到的那条）",
              hint.splitlines()[0])
    finally:
        toastmod.find_module_dir = old_find               # type: ignore[assignment]
        if old_env is None:
            os.environ.pop("PSModulePath", None)
        else:
            os.environ["PSModulePath"] = old_env
    check(toastmod.module_installed() or toastmod.find_module_dir() != "",
          "恢复环境后模块又能被找到（测试没有破坏本机状态）")


def test_offline_dir() -> None:
    """离线部署：把 BurntToast 目录放在 exe 旁边（含版本化目录）要能认出来。"""
    print("\n[2d] 离线模块目录识别")
    with tempfile.TemporaryDirectory(prefix="wpp_off_") as tmp:
        # 版本化布局：BurntToast\1.1.0\BurntToast.psd1（Save-Module 下来就是这样）
        ver = os.path.join(tmp, "BurntToast", "1.1.0")
        os.makedirs(ver, exist_ok=True)
        with open(os.path.join(ver, "BurntToast.psd1"), "w", encoding="utf-8") as f:
            f.write("# stub\n")
        check(toastmod._module_root(os.path.join(tmp, "BurntToast")) == ver,
              "能认出 BurntToast\\1.1.0\\BurntToast.psd1 这种版本化目录")
        flat = os.path.join(tmp, "flat")
        os.makedirs(flat, exist_ok=True)
        with open(os.path.join(flat, "BurntToast.psm1"), "w", encoding="utf-8") as f:
            f.write("# stub\n")
        check(toastmod._module_root(flat) == flat, "扁平布局（直接放 psd1/psm1）也能认")
        check(toastmod._module_root(os.path.join(tmp, "nope")) == "",
              "空目录 / 不存在的目录返回空")
    real = toastmod.find_module_dir()
    check(bool(real), "本机能找到模块（exe 旁边 / 自动装的位置 / 系统已装）", real or "（没有）")


def test_bundled_module() -> None:
    """模块打进 exe：_MEIPASS 里的那份要能找出来，且优先级正确。

    另外核对「裁剪版模块」为什么安全：生成的 toast.ps1 只走显式 AppId 提交，
    完全不碰 Microsoft.Toolkit 的兼容提交路径 —— 被删掉的那 20 MB DLL 正是
    只有那条路径才加载的。
    """
    print("\n[2e] exe 内置模块（_MEIPASS）")
    old_meipass = getattr(sys, "_MEIPASS", None)
    old_local = toastmod.local_module_dir
    old_cache = toastmod.module_cache_dir
    try:
        with tempfile.TemporaryDirectory(prefix="wpp_bundle_") as tmp:
            # 模拟 PyInstaller 单文件解包出来的 _MEIPASS\BurntToast
            packed = os.path.join(tmp, "_MEI123456", "BurntToast")
            os.makedirs(os.path.join(packed, "lib"), exist_ok=True)
            with open(os.path.join(packed, "BurntToast.psd1"), "w", encoding="utf-8") as f:
                f.write("# stub\n")

            nowhere = os.path.join(tmp, "nowhere")
            cache = os.path.join(nowhere, "modules", "BurntToast")
            toastmod.local_module_dir = lambda: os.path.join(nowhere, "BurntToast")   # type: ignore[assignment]
            toastmod.module_cache_dir = lambda: cache            # type: ignore[assignment]
            sys._MEIPASS = os.path.join(tmp, "_MEI123456")        # type: ignore[attr-defined]

            check(toastmod.bundled_module_dir() == packed,
                  "打包后能算出内置模块目录（sys._MEIPASS\\BurntToast）",
                  toastmod.bundled_module_dir())
            check(toastmod.find_module_dir() == cache,
                  "只有内置模块时，先释放一份到本机模块目录再用它",
                  toastmod.find_module_dir())
            check(os.path.isfile(os.path.join(cache, "BurntToast.psd1")),
                  "释放出来的目录里确实有模块文件")
            check(toastmod.module_origin(cache) == "exe 内置 → 已释放到本机模块目录",
                  "状态里说清楚「这份是从 exe 内置的模块释放来的」",
                  toastmod.module_origin(cache))

            # 本机模块目录建不了（比如被同名文件挡住）→ 退回直接用 _MEIPASS 里那份，
            # 而不是报"没有模块"
            blocker = os.path.join(tmp, "blocker")
            with open(blocker, "w", encoding="utf-8") as f:
                f.write("x")
            toastmod.module_cache_dir = lambda: os.path.join(blocker, "modules", "BurntToast")  # type: ignore[assignment]
            toastmod._seeded_from_bundle = False                 # type: ignore[attr-defined]
            check(toastmod.find_module_dir() == packed,
                  "释放失败时仍然直接用 _MEIPASS 里那份（不会误报缺模块）",
                  toastmod.find_module_dir())
            check(toastmod.module_origin(packed) == "随 exe 内置",
                  "这时候来源标注为「随 exe 内置」")
            toastmod.module_cache_dir = lambda: cache            # type: ignore[assignment]

            # 运维在 exe 旁边放一份要能盖过内置的（升级模块不用重打包）
            beside = os.path.join(tmp, "beside", "BurntToast")
            os.makedirs(beside, exist_ok=True)
            with open(os.path.join(beside, "BurntToast.psd1"), "w", encoding="utf-8") as f:
                f.write("# stub\n")
            toastmod.local_module_dir = lambda: beside                  # type: ignore[assignment]
            check(toastmod.find_module_dir() == beside,
                  "exe 旁边的模块优先于内置的（可单独升级模块）")
            check(toastmod.module_origin(beside) == "exe 旁边", "来源标注为「exe 旁边」")

            # 配置里指定的最优先
            configured = os.path.join(tmp, "configured")
            os.makedirs(configured, exist_ok=True)
            with open(os.path.join(configured, "BurntToast.psd1"), "w", encoding="utf-8") as f:
                f.write("# stub\n")
            check(toastmod.find_module_dir(configured) == configured,
                  "配置指定的目录优先级最高")
    finally:
        toastmod.local_module_dir = old_local          # type: ignore[assignment]
        toastmod.module_cache_dir = old_cache          # type: ignore[assignment]
        toastmod._seeded_from_bundle = False           # type: ignore[attr-defined]
        if old_meipass is None:
            try:
                delattr(sys, "_MEIPASS")
            except AttributeError:
                pass
        else:
            sys._MEIPASS = old_meipass                 # type: ignore[attr-defined]

    check(toastmod.bundled_module_dir() == "",
          "源码运行时没有 _MEIPASS，内置目录为空（不误报）")

    # 裁剪版模块的安全性依据：脚本里没有兼容提交路径
    script = toastmod.ensure_script()
    if script:
        with open(script, "r", encoding="utf-8") as f:
            code = f.read()
        check("ToastNotificationManagerCompat" not in code,
              "生成的脚本不使用 Toolkit 兼容提交路径（所以能删掉 20 MB 的 WinRT 投影 DLL）")
        check("CreateToastNotifier" in code and "AppId" in code,
              "通知是用显式 AppId 提交的（署名才是「Win 壁纸推送」）")
    else:
        skip("裁剪版模块依据", "toast.ps1 没写出来")


def test_render_passes_module_dir() -> None:
    """render 必须把「实际找到的模块目录」显式传给 PowerShell，不能只传配置值。

    回归用：以前是 render(spec, configured)，configured 默认空串，于是
    「exe 旁边 / exe 内置 / 自动装的缓存」这三种找到的模块全都没被用上，
    PowerShell 只能去默认搜索路径里找 → 报「未能加载指定模块 BurntToast」。
    """
    print("\n[2f] render 会把找到的模块目录交给 PowerShell")
    real_run = toastmod.subprocess.run
    calls: list = []

    class _Result:
        returncode = 1
        stdout = b""
        stderr = b""

    def fake_run(args, **kw):
        calls.append(list(args))
        return _Result()

    old_local = toastmod.local_module_dir
    old_cache = toastmod.module_cache_dir
    old_meipass = getattr(sys, "_MEIPASS", None)
    try:
        with tempfile.TemporaryDirectory(prefix="wpp_mdir_") as tmp:
            packed = os.path.join(tmp, "_MEI9", "BurntToast")
            os.makedirs(packed, exist_ok=True)
            with open(os.path.join(packed, "BurntToast.psd1"), "w", encoding="utf-8") as f:
                f.write("# stub\n")
            none = os.path.join(tmp, "none")
            toastmod.local_module_dir = lambda: os.path.join(none, "BurntToast")   # type: ignore[assignment]
            toastmod.module_cache_dir = lambda: os.path.join(none, "modules", "BurntToast")  # type: ignore[assignment]
            sys._MEIPASS = os.path.join(tmp, "_MEI9")     # type: ignore[attr-defined]
            toastmod.subprocess.run = fake_run            # type: ignore[assignment]

            ok, _msg = toastmod.render({"text": ["x"], "sound": "Default"},
                                      timeout=5)
            check(not ok, "（用假的 subprocess）渲染按预期返回失败")
            args = calls[-1] if calls else []
            check("-ModuleDir" in args, "命令行里确实带上了 -ModuleDir",
                  " ".join(args[-4:]))
            if "-ModuleDir" in args:
                got = args[args.index("-ModuleDir") + 1]
                cache = os.path.join(none, "modules", "BurntToast")
                check(os.path.normcase(got) == os.path.normcase(cache),
                      "传出去的是从 exe 内置模块释放出来的稳定路径（不是临时解包目录）", got)
                check(os.path.isfile(os.path.join(got, "BurntToast.psd1")),
                      "那个路径下真的有模块文件")
    finally:
        toastmod.subprocess.run = real_run               # type: ignore[assignment]
        toastmod.local_module_dir = old_local            # type: ignore[assignment]
        toastmod.module_cache_dir = old_cache            # type: ignore[assignment]
        if old_meipass is None:
            try:
                delattr(sys, "_MEIPASS")
            except AttributeError:
                pass
        else:
            sys._MEIPASS = old_meipass                   # type: ignore[attr-defined]


def test_notification_priority() -> None:
    """通知优先级：第一次运行写「允许紧急通知」，之后不重复改；能关掉。

    会真的读写本机 HKCU（`...\\Notifications\\Settings\\WinWallpaperPush.Agent`），
    结束后把原来的值/标记文件原样还原 —— 跟 test_agent_control 一样，
    测完不留痕迹。
    """
    print("\n[2g] 通知优先级（允许紧急通知）")
    if sys.platform != "win32":
        skip("通知优先级", "非 Windows")
        return
    import winreg

    key_path = rf"{toastmod.NOTIF_SETTINGS_KEY}\{toastmod.APP_ID}"
    marker = toastmod._priority_marker()
    # ---- 快照
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            old_val = winreg.QueryValueEx(k, toastmod.URGENT_VALUE)[0]
    except OSError:
        old_val = None
    old_marker = None
    if os.path.isfile(marker):
        with open(marker, "rb") as f:
            old_marker = f.read()

    def read_val():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
                return winreg.QueryValueEx(k, toastmod.URGENT_VALUE)[0]
        except OSError:
            return None

    try:
        ok, msg = toastmod.set_notification_priority(True)
        check(ok, "能把通知优先级设为最高", msg)
        check(read_val() == 1,
              f"注册表里 {toastmod.URGENT_VALUE} 写成了 1（DWORD）")
        check(toastmod.allow_urgent_state() == 1, "读回来也是 1")
        state = toastmod.notification_priority_state()
        check("最高" in state, "状态行显示「最高」", state)
        check(os.path.isfile(marker), "写下了「已经设置过」的标记文件")

        ok2, msg2 = toastmod.ensure_notification_priority()
        check(ok2 and "已经设置过" in msg2,
              "第二次运行时不再重复改动（不跟用户手动关掉的设置对着干）", msg2)

        ok3, msg3 = toastmod.set_notification_priority(False)
        check(ok3 and read_val() == 0, "能关掉（--allow-urgent off）", msg3)
        state2 = toastmod.notification_priority_state()
        check("未开启" in state2, "关掉后状态行如实说明", state2)

        ok4, _msg4 = toastmod.ensure_notification_priority(force=True)
        check(ok4 and read_val() == 1, "force 时会重新打开", str(read_val()))
    finally:
        # ---- 还原
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                    winreg.KEY_SET_VALUE) as k:
                if old_val is None:
                    try:
                        winreg.DeleteValue(k, toastmod.URGENT_VALUE)
                    except OSError:
                        pass
                else:
                    winreg.SetValueEx(k, toastmod.URGENT_VALUE, 0,
                                      winreg.REG_DWORD, old_val)
        except OSError:
            pass
        try:
            if old_marker is None:
                if os.path.isfile(marker):
                    os.remove(marker)
            else:
                with open(marker, "wb") as f:
                    f.write(old_marker)
        except OSError:
            pass
    check(read_val() == old_val, "测试后注册表已还原成原样",
          f"现在是 {read_val()}，原来是 {old_val}")


def test_render(available: bool) -> None:
    print("\n[3] 真实弹一条并核对通知中心")
    if not available:
        skip("真实渲染", "BurntToast 不可用")
        return
    uid = "wpp-test-toast-render"
    ok, spec, err = TS.normalize({
        "text": ["通知自测", f"主机 {os.environ.get('COMPUTERNAME', '?')}",
                 time.strftime("%H:%M:%S")],
        "sound": "Default", "attribution": "Win 壁纸推送", "unique_id": uid,
        "expire_minutes": 10,
    })
    if not check(ok, "自测规格合法", err):
        return
    ok2, msg = toastmod.render(spec)
    check(ok2, "调用 BurntToast 渲染成功", msg)
    if not ok2:
        return
    time.sleep(2)
    found, detail = history_has(uid)
    check(found, "通知中心里能查到这条通知（按我们自己的 AppId）", detail[:90])
    check("通知自测" in detail, "内容正确（标题在里面）", detail[:90])
    check("scenario=" in detail, "带上了 scenario 字段（紧急通知靠它穿透专注助手）", detail[:90])


# ---------------------------------------------------------------- 4/5. 端到端

def test_end_to_end(tmp: str) -> None:
    print("\n[4] 端到端：控制端广播 → 被控端拉图 → 弹通知 → 回执")
    import winipc
    from agent import AgentCore
    from controller import ControllerCore

    if winipc.instance_running():
        skip("端到端通知", "本机已有被控端在运行，先 --stop 再跑本测试")
        return

    png = os.path.join(tmp, "hero.png")
    make_png(png)
    with open(png, "rb") as f:
        png_bytes = f.read()

    agent = AgentCore({
        "udp_port": TEST_PORT, "retry": 1, "keep": 1, "hello_interval": 0,
        "wallpaper_dir": os.path.join(tmp, "wp"),
        "log_file": os.path.join(tmp, "agent.log"),
        "toast_min_interval": 0,
    }, log=lambda m, l="info": print("      [agent]", m))
    ctrl = ControllerCore({
        "udp_port": TEST_PORT, "tcp_port": TCP_PORT, "reply_port": REPLY_PORT,
        "sweep": False, "auto_scan": 0,
    }, log=lambda m, l="info": print("      [ctrl ]", m))
    try:
        # 被控端先起来：同一台机器上两个 socket 都绑着 38571，回环单播只会投给
        # 先绑定的那个；生产环境里控制端和被控端在不同机器上，不存在这个问题。
        agent.start()
        time.sleep(0.5)
        ctrl.start()
        time.sleep(0.4)

        uid = "wpp-test-e2e"
        task = ctrl.push_toast({
            "text": ["端到端通知", "带大图和按钮的那条", "第三行"],
            "sound": "Reminder", "urgent": True, "attribution": "Win 壁纸推送",
            "hero_image": "hero.png", "unique_id": uid,
            "buttons": [{"content": "打开项目", "arguments": "https://example.com/wpp"}],
            "progress": {"status": "下发中 70%", "value": 0.7},
        }, assets={"hero.png": png_bytes}, local_only=True)

        deadline = time.time() + 25
        acks: dict = {}
        while time.time() < deadline:
            time.sleep(0.25)
            with ctrl._dev_lock:
                acks = dict(ctrl._acks.get(task, {}))
            if acks:
                break
        check(bool(acks), "收到被控端回执", json.dumps(
            {k: v[0] for k, v in acks.items()}, ensure_ascii=False))
        good = [v for v in acks.values() if v[0]]
        bad = [v for v in acks.values() if not v[0]]
        check(bool(good) and not bad, "回执是成功",
              (bad[0][1] if bad else "被控端报告已弹出通知"))

        # 图片真的被拉过去了
        asset_dir = os.path.join(
            os.path.dirname(agent.wallpaper_dir), "..", "toast_assets")
        from agent import APP_DIR_NAME
        candidate = os.path.join(os.environ.get("LOCALAPPDATA", ""), APP_DIR_NAME,
                                 "toast_assets", task, "hero.png")
        check(os.path.isfile(candidate), "通知里的大图被下发到被控端",
              candidate if os.path.isfile(candidate) else "（没找到）")
        if os.path.isfile(candidate):
            with open(candidate, "rb") as f:
                check(f.read() == png_bytes, "图片内容和控制端的一致（sha256 校验过）")

        time.sleep(2)
        found, detail = history_has(uid)
        check(found, "通知中心里能查到这条端到端通知", detail[:100])
        check("端到端通知" in detail and "带大图和按钮的那条" in detail,
              "标题和正文都对", detail[:100])
        check("actions=1" in detail, "按钮也带上了", detail[:100])
        check("scenario=urgent" in detail, "紧急标记生效（scenario=urgent）", detail[:100])

        print("\n[5] 被控端不盲信网络：伪造一条危险通知")
        # 绕过 push_toast 的校验，直接广播一个危险规格，看被控端拦不拦
        bad_spec = {"text": ["恶意通知"], "buttons": [
            {"content": "点我", "arguments": "cmd.exe /c calc"}]}
        bad_task = "fedcba987654"
        ctrl._broadcast(P.make(P.MSG_TOAST, task=bad_task, spec=bad_spec, assets=[],
                               tcp_port=TCP_PORT, reply_port=REPLY_PORT,
                               ts=time.time()), targets=["127.0.0.1"])
        deadline = time.time() + 15
        acks2: dict = {}
        while time.time() < deadline:
            time.sleep(0.25)
            with ctrl._dev_lock:
                acks2 = dict(ctrl._acks.get(bad_task, {}))
            if acks2:
                break
        check(bool(acks2), "被控端对危险通知有回执（说明它确实处理了）",
              json.dumps({k: v[1][:40] for k, v in acks2.items()}, ensure_ascii=False))
        refused = any((not v[0]) and ("按钮" in v[1] or "规格" in v[1])
                      for v in acks2.values())
        check(refused, "危险按钮的通知被拒绝，并回报了原因")
    finally:
        ctrl.stop()
        agent.stop()
        time.sleep(0.3)


def main() -> int:
    print("=" * 68)
    print("  Win 壁纸推送 —— 通知（Toast）功能自测")
    print("=" * 68)
    test_spec()
    available = test_module() or False
    test_missing_module()
    test_offline_dir()
    test_bundled_module()
    test_render_passes_module_dir()
    test_notification_priority()
    test_duration_and_attribution()
    test_text_braces()
    test_render(bool(available))
    with tempfile.TemporaryDirectory(prefix="wpp_toast_") as tmp:
        test_end_to_end(tmp)

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
