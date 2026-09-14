#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面冒烟测试

真正把控制端和被控端的窗口创建出来、跑几帧事件循环再销毁，
用来在没有人工点按钮的情况下，发现界面代码里的拼写/属性/布局错误。

运行： python test_gui_smoke.py
窗口会一闪而过，属正常现象。
"""

from __future__ import annotations

import os
import sys
import time
import traceback

FRAMES = 12
FRAME_GAP = 0.05

# 布局检查用：记录每个被测窗口「内容实际需要多大」和「窗口实际多大」，
# 内容比窗口大就说明有控件被裁掉了（多加了几个控件最容易踩这个坑）。
LAYOUT: list[tuple[str, int, int, int, int]] = []


def check_layout(win, name: str) -> tuple[bool, str]:
    """内容比窗口大就说明有控件被裁掉了。

    窗口尺寸本身受屏幕高度限制（屏幕太小只能退让），所以比较基准也要
    跟着屏幕走，否则小屏机器上会误报。

    被控端面板现在受密码保护：自动化环境下没人输密码，窗口就是 withdraw 状态，
    这时 winfo_width() 拿不到真实尺寸 —— 改读我们设定过的 geometry。
    """
    win.update_idletasks()
    try:
        # 子窗口（Toplevel）的尺寸变化要跑一次完整事件循环才会生效：
        # 假 mainloop 只对主窗口调 update()，这里给子窗口补一次。
        win.update()
    except Exception:
        pass
    req_w, req_h = win.winfo_reqwidth(), win.winfo_reqheight()
    act_w, act_h = win.winfo_width(), win.winfo_height()
    if not win.winfo_ismapped():
        # 被控端面板现在受密码保护：自动化环境下没人输密码，窗口就一直是
        # withdraw 状态，而没映射过的窗口读不出真实尺寸（永远 200x200）。
        # 测试自己把它显示一次再量 —— 闸门该拦的已经拦过了（见
        # test_agent_control 的 --show 用例），这里只是量布局。
        try:
            win.deiconify()
            win.update_idletasks()
            win.update()
        except Exception:
            pass
        act_w, act_h = win.winfo_width(), win.winfo_height()
    room_w = max(640, win.winfo_screenwidth() - 60)
    room_h = max(480, win.winfo_screenheight() - 90)
    need_w, need_h = min(req_w, room_w), min(req_h, room_h)
    LAYOUT.append((name, req_w, req_h, act_w, act_h))
    ok = act_w >= need_w and act_h >= need_h
    return ok, (f"{name}：内容需要 {req_w}×{req_h}，窗口 {act_w}×{act_h}"
                + ("" if ok else "  ← 有控件会被裁掉"))


def exercise_widgets(win, safe_texts: tuple[str, ...] = (
        "重新检测", "扫描", "运行日志", "清空")) -> list[str]:
    """按一遍界面上「点了不会弹对话框、不会真发东西」的按钮，看回调里有没有拼写错误。

    会弹文件选择框的（浏览…）、真会广播出去的（推送 / 发送通知）一律不点，
    否则测试要么卡在对话框上，要么真的往局域网发东西。
    """
    out: list[str] = []
    targets: list[object] = []

    def walk(w):
        for child in w.winfo_children():
            cls = child.winfo_class()
            if cls in ("Checkbutton", "TCheckbutton"):
                targets.append(child)
            elif cls in ("Button", "TButton"):
                try:
                    text = str(child.cget("text"))
                except Exception:
                    text = ""
                if any(s in text for s in safe_texts):
                    targets.append(child)
            walk(child)

    walk(win)
    if not targets:
        return ["[SKIP] 没有找到可安全点击的控件"]
    for widget in targets:
        try:
            name = str(widget.cget("text"))
        except Exception:
            name = widget.winfo_class()
        try:
            widget.invoke()
            win.update_idletasks()
            out.append(f"[PASS] 点击「{name}」没有报错")
        except Exception as e:
            out.append(f"[FAIL] 点击「{name}」抛异常：{e!r}")
    return out


EDITOR_SAFE_TEXTS = ("高级选项", "添加按钮", "普通通知", "维护公告", "紧急提醒",
                     "到期提醒", "删")


def find_notebooks(win) -> list:
    """找出界面里的页签容器（控制端的「壁纸 / 通知 / 设置」）。"""
    out: list = []

    def walk(w):
        for ch in w.winfo_children():
            if ch.winfo_class() == "TNotebook":
                out.append(ch)
            walk(ch)

    walk(win)
    return out


def find_scrollframes(win) -> list:
    """找出界面里的滚动容器（ui.ScrollFrame）。"""
    out: list = []

    def walk(w):
        for ch in w.winfo_children():
            if hasattr(ch, "inner") and hasattr(ch, "canvas"):
                out.append(ch)
            walk(ch)

    walk(win)
    return out


def count_toplevels(win) -> int:
    """数一下当前有几个子窗口（Toplevel）——用来断言"编辑通知不再弹窗"。"""
    n = 0

    def walk(w):
        nonlocal n
        for ch in w.winfo_children():
            if ch.winfo_class() == "Toplevel":
                n += 1
            walk(ch)

    walk(win)
    return n


def _safe_text(w) -> str:
    try:
        return str(w.cget("text"))
    except Exception:
        return ""


def _value(w) -> str:
    """取输入框 / 下拉框里的当前值。

    为什么不能直接用 _safe_text：TEntry / TCombobox 没有 -text 选项，
    cget("text") 永远返回空串 —— 用它找"私钥路径是不是填好了"会永远找不到。
    """
    try:
        if w.winfo_class() in ("TEntry", "Entry", "TCombobox", "Combobox"):
            return str(w.get())
    except Exception:
        pass
    return _safe_text(w)


def is_log_text(w) -> bool:
    """这个 Text 是不是「日志面板」。

    日志面板一律是只读的（ui.LogPane 建出来就是 state=disabled），
    而命令输入框之类的 Text 是可编辑的 —— 靠这一点区分。
    以前这里写的是"只要有一个可见的 Text 就算日志展开了"，
    后来「远程命令」页加了**可见的命令输入框**，那条判断就误报了。
    """
    try:
        return w.winfo_class() == "Text" and str(w.cget("state")) == "disabled"
    except Exception:
        return False


def find_widgets(win, cls_names: tuple[str, ...]) -> list:
    out: list = []

    def walk(w):
        for ch in w.winfo_children():
            if ch.winfo_class() in cls_names:
                out.append(ch)
            walk(ch)

    walk(win)
    return out


def clipped_texts(win) -> list[str]:
    """找出「文字被容器切掉」的控件。

    排版重做最容易出的问题不是报错，而是某个标签比它分到的位置还宽 ——
    界面上看着就是文字被硬生生切掉一截。这里逐个量：已经布局过的控件，
    实际宽度比所需宽度还小就是被切了（留 2px 容差）。
    """
    bad: list[str] = []
    for w in find_widgets(win, ("TLabel", "Label", "TButton", "Button", "TCheckbutton",
                                "Checkbutton", "TEntry", "Entry", "TCombobox")):
        try:
            if not w.winfo_ismapped() or w.winfo_width() <= 1:
                continue
            need = w.winfo_reqwidth()
            got = w.winfo_width()
            if need > got + 2:
                try:
                    text = str(w.cget("text"))[:24]
                except Exception:
                    text = w.winfo_class()
                bad.append(f"{w.winfo_class()}「{text}」需要 {need}px，只有 {got}px")
        except Exception:
            continue
    return bad


def snapshot_autostart() -> tuple[str, object]:
    """备份 HKCU 的开机自启项。

    被控端界面上那个「登录时自动运行」复选框是真的会写注册表的 ——
    冒烟测试会点它，所以跑完必须把用户原来的状态放回去。
    返回 (值名, 原值)；原值为 None 表示「本来就没有这一项」。
    """
    try:
        import winreg

        import agent
    except Exception:
        return "", None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, agent._RUN_KEY) as k:
            return agent.RUN_VALUE_NAME, winreg.QueryValueEx(k, agent.RUN_VALUE_NAME)[0]
    except FileNotFoundError:
        return agent.RUN_VALUE_NAME, None
    except Exception:
        return "", None


def restore_autostart(saved: tuple[str, object]) -> None:
    name, value = saved
    if not name:
        return
    try:
        import winreg

        import agent

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, agent._RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            if value is None:
                try:
                    winreg.DeleteValue(k, name)
                except OSError:
                    pass
            else:
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
    except Exception as e:
        print(f"  [WARN] 恢复开机自启项失败：{e}")


def snapshot_priority() -> tuple[object, bytes | None]:
    """备份「通知优先级」相关的东西。

    被控端面板上那个「设为最高（允许紧急通知）」复选框也是真写注册表的
    （HKCU\\...\\Notifications\\Settings\\<AppId>\\AllowUrgentNotifications），
    外加一个"已经设置过"的标记文件 —— 冒烟测试会点它，跑完都要放回去。
    返回 (原值或 None, 标记文件内容或 None)。
    """
    import os

    orig: object = None
    try:
        import winreg

        import toast as toastmod

        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                rf"{toastmod.NOTIF_SETTINGS_KEY}\{toastmod.APP_ID}") as k:
            orig = winreg.QueryValueEx(k, toastmod.URGENT_VALUE)[0]
    except Exception:
        orig = None
    marker_bytes: bytes | None = None
    try:
        import toast as toastmod

        path = toastmod._priority_marker()
        if os.path.isfile(path):
            with open(path, "rb") as f:
                marker_bytes = f.read()
    except Exception:
        marker_bytes = None
    return orig, marker_bytes


def restore_priority(saved: tuple[object, bytes | None]) -> None:
    import os

    orig, marker_bytes = saved
    try:
        import winreg

        import toast as toastmod

        with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                rf"{toastmod.NOTIF_SETTINGS_KEY}\{toastmod.APP_ID}", 0,
                winreg.KEY_SET_VALUE) as k:
            if orig is None:
                try:
                    winreg.DeleteValue(k, toastmod.URGENT_VALUE)
                except OSError:
                    pass
            else:
                winreg.SetValueEx(k, toastmod.URGENT_VALUE, 0,
                                  winreg.REG_DWORD, orig)
    except Exception as e:
        print(f"  [WARN] 恢复通知优先级失败：{e}")
    try:
        import toast as toastmod

        path = toastmod._priority_marker()
        if marker_bytes is None:
            if os.path.isfile(path):
                os.remove(path)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(marker_bytes)
    except Exception as e:
        print(f"  [WARN] 恢复通知优先级标记失败：{e}")


def snapshot_configs() -> dict[str, bytes | None]:
    """测试会真的点复选框，会把配置写坏（比如把「自动重扫」关掉）。

    先备份两个配置文件，跑完原样放回去，别动用户的设置。
    """
    import os

    snap: dict[str, bytes | None] = {}
    for name in ("controller_config.json", "agent_config.json"):
        try:
            with open(name, "rb") as f:
                snap[name] = f.read()
        except OSError:
            snap[name] = None
    return snap


def restore_configs(snap: dict[str, bytes | None]) -> None:
    import os

    for name, data in snap.items():
        try:
            if data is None:
                if os.path.isfile(name):
                    os.remove(name)
            else:
                with open(name, "wb") as f:
                    f.write(data)
        except OSError:
            print(f"  [WARN] 恢复 {name} 失败")


def main() -> int:
    # 被控端面板要密码：自动化场景下没人去点那个弹窗，设 2 秒超时（超时=拒绝），
    # 这样冒烟测试不会挂在密码框上。它只影响等待时长，不能绕过密码。
    os.environ.setdefault("WPP_PROMPT_TIMEOUT", "2")
    try:
        import tkinter as tk
    except Exception as e:
        print(f"[SKIP] 当前环境没有可用的 tkinter：{e}")
        return 0

    # 把 mainloop 换成「跑几帧就销毁」，这样窗口不会真的停在那儿等人操作
    real_mainloop = tk.Tk.mainloop
    frames_run = {"n": 0}
    windows: list[tk.Tk] = []

    def fake_mainloop(self):
        try:
            windows.append(self)
            for _ in range(FRAMES):
                self.update_idletasks()
                self.update()
                frames_run["n"] += 1
                time.sleep(FRAME_GAP)
            # 布局自检：内容比窗口大 = 有控件被裁掉
            ok, detail = check_layout(self, self.title() or "未命名窗口")
            print(f"  [{'PASS' if ok else 'FAIL'}] 布局完整（没被裁掉）   ({detail})")
            if not ok:
                layout_failures.append(detail)
            # 先记下"刚建出来时"的状态：下面要按一堆按钮，其中「运行日志」
            # 会把日志展开，之后再查就查不出"默认是不是收起"了。
            log_open_at_start = any(
                is_log_text(x) and x.winfo_ismapped()
                for x in find_widgets(self, ("Text",)))

            # 回调自检：把所有复选框和「重新检测网卡」按一遍，
            # 免得这些新加的按钮里藏着拼写错误，要点下去才炸
            for line in exercise_widgets(self):
                print("  " + line)
                if line.startswith("[FAIL]"):
                    layout_failures.append(line)

            # 页签自检：控制端现在是「壁纸 / 通知 / 远程命令 / 设置」四个页面，
            # 一个都不能是空的、点不开的，而且内容必须看得到
            # （要么整页放得下，要么能滚动到 —— 不允许有控件被裁掉）。
            books = find_notebooks(self)
            for nb in books:
                tabs = list(nb.tabs())
                names = [nb.tab(t, "text").strip() for t in tabs]
                print(f"  [PASS] 找到页签 {len(tabs)} 个：" + "、".join(names))
                if len(tabs) < 2:
                    layout_failures.append("页签数量不对（控制端应该有壁纸/通知/远程命令/设置四个页）")
                for need in ("壁纸", "通知", "远程命令", "设置"):
                    if need not in names:
                        layout_failures.append(f"少了「{need}」页签（现有：{'、'.join(names)}）")
                before_popups = count_toplevels(self)
                for idx, t in enumerate(tabs):
                    try:
                        nb.select(t)
                        self.update_idletasks()
                        self.update()
                    except Exception as e:
                        layout_failures.append(f"切到第 {idx + 1} 个页签失败：{e!r}")
                        continue
                    page = self.nametowidget(t)
                    kids = len(page.winfo_children())
                    print(f"  [{'PASS' if kids else 'FAIL'}] 页签「"
                          f"{nb.tab(t, 'text').strip()}」有内容（{kids} 个直接子控件）")
                    if not kids:
                        layout_failures.append(f"页签「{nb.tab(t, 'text').strip()}」是空的")
                    # 页里安全的东西（预设 / 高级选项 / 加按钮）也点一遍
                    for line in exercise_widgets(page, EDITOR_SAFE_TEXTS):
                        print("  " + line)
                        if line.startswith("[FAIL]"):
                            layout_failures.append(line)
                # 用户的硬要求：发送通知不能再弹窗，必须在页里就地编辑
                after_popups = count_toplevels(self)
                ok_popup = after_popups <= before_popups
                print(f"  [{'PASS' if ok_popup else 'FAIL'}] 切换页签 / 展开高级选项"
                      f"都不弹窗（子窗口 {before_popups} → {after_popups}）")
                if not ok_popup:
                    layout_failures.append("通知编辑仍然弹出了子窗口")

            # 滚动容器自检：内容要么放得下，要么滚得到（scrollregion 覆盖全部内容），
            # 而且**不能横向溢出** —— 页签内容比窗口窄的时候，横向溢出等于把右边的
            # 控件整片推到看不见的地方（用户反馈过的「进度/进度标题/标识/过期被挡住」）。
            for sf in find_scrollframes(self):
                try:
                    sf.update_idletasks()
                    sf.update()
                    bbox = sf.canvas.bbox("all")
                    need = sf.inner.winfo_reqheight()
                    ok_sf = bool(bbox) and bbox[3] >= need - 2
                    print(f"  [{'PASS' if ok_sf else 'FAIL'}] 可滚动页面内容完整"
                          f"（内容 {need}px，可滚区域到 {bbox[3] if bbox else '?'}px）")
                    if not ok_sf:
                        layout_failures.append("滚动页面里有内容滚不到（会被裁掉）")

                    cv_w = sf.canvas.winfo_width()
                    cv_x = sf.canvas.winfo_rootx()
                    over: list[str] = []

                    def walk_inner(w):
                        for ch in w.winfo_children():
                            try:
                                if ch.winfo_ismapped() and ch.winfo_width() > 1:
                                    right = ch.winfo_rootx() - cv_x + ch.winfo_width()
                                    if right > cv_w + 2:
                                        try:
                                            txt = str(ch.cget("text"))[:16]
                                        except Exception:
                                            txt = ""
                                        over.append(f"{ch.winfo_class()}「{txt}」右边到 {right}px"
                                                    f"（页面只有 {cv_w}px）")
                            except Exception:
                                pass
                            walk_inner(ch)

                    walk_inner(sf.inner)
                    ok_over = not over
                    print(f"  [{'PASS' if ok_over else 'FAIL'}] 页面内容没有横向溢出"
                          + ("" if ok_over else f"（{len(over)} 个控件跑到右边外面）"))
                    if not ok_over:
                        layout_failures.extend(f"横向溢出：{o}" for o in over[:6])
                except Exception as e:
                    layout_failures.append(f"滚动容器自检失败：{e!r}")

            # 「发送通知」必须一直够得着：高级区展开后内容有两屏高，
            # 发送按钮要钉在页脚，不能跟着内容滚出去。
            # （要先切回「通知」页 —— 上面的页签循环停在最后一页，那页没映射。）
            if books:
                nb_pin = books[0]
                for t in nb_pin.tabs():
                    if nb_pin.tab(t, "text").strip() == "通知":
                        nb_pin.select(t)
                self.update_idletasks()
                self.update()
            senders = [b for b in find_widgets(self, ("TButton", "Button"))
                       if "发送通知" in _safe_text(b)]
            if senders:
                b = senders[0]
                win_top = self.winfo_rooty()
                win_bottom = win_top + self.winfo_height()
                by = b.winfo_rooty()
                ok_pin = b.winfo_ismapped() and win_top <= by <= win_bottom
                print(f"  [{'PASS' if ok_pin else 'FAIL'}] 「发送通知」钉在可见区域"
                      f"（按钮 y={by}，窗口 {win_top}~{win_bottom}）")
                if not ok_pin:
                    layout_failures.append("发送按钮被内容挤出可见区域")

            # ================= 「远程命令」页自检
            # 这一页是新加的：在几十台机器上跑 SSH 命令，界面里填错一个字段
            # 就可能"一条都没发出去"，所以把关键控件都点名检查一遍。
            if books:
                nb_ssh = books[0]
                for t in nb_ssh.tabs():
                    if nb_ssh.tab(t, "text").strip() == "远程命令":
                        nb_ssh.select(t)
                self.update_idletasks()
                self.update()

                # ① 命令框里要有内容（默认 hostname），而且是可编辑的多行框
                cmds = ""
                for w in find_widgets(self, ("Text",)):
                    try:
                        if not (w.winfo_ismapped() and w.winfo_width() > 80):
                            continue
                        body = w.get("1.0", "end").strip()
                    except Exception:
                        continue
                    if body and "hostname" in body.lower():
                        cmds = body
                        break
                ok_cmd = bool(cmds)
                print(f"  [{'PASS' if ok_cmd else 'FAIL'}] 远程命令页有命令输入框且预填了命令"
                      f"（内容：{cmds.splitlines()[0] if cmds else '空'}）")
                if not ok_cmd:
                    layout_failures.append("远程命令页的命令输入框没预填/找不到")

                # ② 私钥路径默认值要能看见（就是用户给的原始命令里的那个路径）
                keys = [e for e in find_widgets(self, ("TEntry", "Entry"))
                        if "id_ed25519" in _value(e)]
                print(f"  [{'PASS' if keys else 'FAIL'}] 私钥 -i 默认值在界面上"
                      f"（{_value(keys[0])[:52] if keys else '没找到'}）")
                if not keys:
                    layout_failures.append("远程命令页没有私钥路径输入框/没有默认值")

                # ③ 结果表：每台一行（IP / 状态 / 退出码 / 远端回显）
                ok_tree2 = False
                for tr in find_widgets(self, ("Treeview",)):
                    try:
                        if list(tr.cget("columns")) == ["ip", "state", "code", "evidence"]:
                            ok_tree2 = True
                    except Exception:
                        pass
                print(f"  [{'PASS' if ok_tree2 else 'FAIL'}] 远程命令页有每台设备的结果表")
                if not ok_tree2:
                    layout_failures.append("远程命令页缺少结果表（IP/状态/退出码/回显）")

                # ④ 动作按钮要钉在可见区域（和「发送通知」同一个道理）
                for label in ("发送到设备", "停止", "生成批处理", "导出结果"):
                    btns = [b for b in find_widgets(self, ("TButton", "Button"))
                            if label in _safe_text(b)]
                    if not btns:
                        layout_failures.append(f"远程命令页缺少「{label}」按钮")
                        print(f"  [FAIL] 远程命令页缺少「{label}」按钮")
                        continue
                    b = btns[0]
                    win_top = self.winfo_rooty()
                    win_bottom = win_top + self.winfo_height()
                    by = b.winfo_rooty()
                    ok_b = b.winfo_ismapped() and win_top <= by <= win_bottom
                    print(f"  [{'PASS' if ok_b else 'FAIL'}] 「{label}」钉在可见区域"
                          f"（y={by}，窗口 {win_top}~{win_bottom}）")
                    if not ok_b:
                        layout_failures.append(f"远程命令页「{label}」被挤出可见区域")

                # ⑤ 执行方式下拉：两种方式都能选（会话式 / 逐条独立）
                modes = [c for c in find_widgets(self, ("TCombobox",))
                         if "会话式" in _value(c)]
                ok_mode = bool(modes) and len(modes[0].cget("values")) == 2
                print(f"  [{'PASS' if ok_mode else 'FAIL'}] 执行方式下拉有 2 个选项"
                      f"（{'/'.join(modes[0].cget('values'))[:44] if modes else '没找到'}）")
                if not ok_mode:
                    layout_failures.append("远程命令页的执行方式下拉不对")

            # 控制端专属的排版指标：设备列表是主体、日志默认收起、文字不被切
            if books:
                trees = find_widgets(self, ("Treeview",))
                if not trees:
                    layout_failures.append("找不到设备列表（Treeview）")
                else:
                    t = max(trees, key=lambda w: w.winfo_width() * w.winfo_height())
                    tw, th = t.winfo_width(), t.winfo_height()
                    ok_tree = tw >= 360 and th >= 240
                    print(f"  [{'PASS' if ok_tree else 'FAIL'}] 设备列表是主体"
                          f"（{tw}×{th}，要求 ≥360×240）")
                    if not ok_tree:
                        layout_failures.append(f"设备列表太小：{tw}×{th}")

                panes = find_widgets(self, ("TPanedwindow", "Panedwindow"))
                print(f"  [{'PASS' if panes else 'FAIL'}] 左右分栏可拖动（找到 {len(panes)} 个）")
                if not panes:
                    layout_failures.append("没有找到可拖动的分栏")

                # 日志默认收起：刚建出来时不该有可见的日志文本框
                print(f"  [{'PASS' if not log_open_at_start else 'FAIL'}] 运行日志默认收起"
                      f"（刚打开时可见文本框：{'有' if log_open_at_start else '无'}）")
                if log_open_at_start:
                    layout_failures.append("日志默认没有收起")

                # 对齐：同一列里的标签左边缘要在同一条竖线上
                labels = find_widgets(self, ("TLabel",))
                xs = {}
                for lb in labels:
                    try:
                        txt = str(lb.cget("text"))
                    except Exception:
                        continue
                    if txt in ("图片", "契合度"):
                        xs[txt] = lb.winfo_rootx()
                if len(xs) == 2:
                    same = abs(xs["图片"] - xs["契合度"]) <= 1
                    print(f"  [{'PASS' if same else 'FAIL'}] 壁纸页字段名左对齐"
                          f"（图片 x={xs['图片']}，契合度 x={xs['契合度']}）")
                    if not same:
                        layout_failures.append("壁纸页字段名没有左对齐")

            cut = clipped_texts(self)
            ok_cut = not cut
            print(f"  [{'PASS' if ok_cut else 'FAIL'}] 没有文字被切掉"
                  + ("" if ok_cut else f"（{len(cut)} 处）"))
            if not ok_cut:
                layout_failures.extend(f"文字被切：{c}" for c in cut[:6])

            # 子窗口（如果还有）也量一遍布局
            for child in self.winfo_children():
                if child.winfo_class() in ("Toplevel",) and child.winfo_exists():
                    for line in exercise_widgets(child, EDITOR_SAFE_TEXTS):
                        print("  " + line)
                        if line.startswith("[FAIL]"):
                            layout_failures.append(line)
                    ok2, detail2 = check_layout(child, child.title() or "子窗口")
                    print(f"  [{'PASS' if ok2 else 'FAIL'}] 子窗口布局完整   ({detail2})")
                    if not ok2:
                        layout_failures.append(detail2)
            # 走真实的「点右上角关闭」路径，顺便验证关闭清理逻辑
            cmd = self.protocol("WM_DELETE_WINDOW")
            if cmd:
                self.tk.call(cmd)
        finally:
            try:
                self.destroy()
            except Exception:
                pass

    tk.Tk.mainloop = fake_mainloop

    failures = []
    layout_failures: list[str] = []
    config_snap = snapshot_configs()
    autostart_snap = snapshot_autostart()
    priority_snap = snapshot_priority()

    # ---------------- 被控端界面
    print("=" * 62)
    print("  [1/2] 被控端界面冒烟测试")
    print("=" * 62)
    frames_run["n"] = 0
    windows.clear()
    try:
        import agent

        # --force：本机可能真的有一个被控端在后台跑（自启项），不加这个参数
        # 主程序会走「单实例」分支直接退出，窗口根本不会创建 —— 那样冒烟测试
        # 就是「跑了个寂寞」，什么都不验证。
        rc = agent.main(["--force"])
        print(f"  被控端 run_gui 正常返回，退出码 {rc}，跑了 {frames_run['n']} 帧")
        if rc != 0:
            failures.append(f"被控端返回了非 0 退出码：{rc}")
        if frames_run["n"] <= 0:
            failures.append("被控端界面没有真的创建出来（一帧都没跑）")
    except Exception:
        traceback.print_exc()
        failures.append("被控端界面抛出异常")
    finally:
        tk.Tk.mainloop = fake_mainloop

    # ---------------- 控制端界面
    print()
    print("=" * 62)
    print("  [2/2] 控制端界面冒烟测试")
    print("=" * 62)
    frames_run["n"] = 0
    windows.clear()
    try:
        import controller

        rc = controller.main([])
        print(f"  控制端 run_gui 正常返回，退出码 {rc}，跑了 {frames_run['n']} 帧")
        if rc != 0:
            failures.append(f"控制端返回了非 0 退出码：{rc}")
        if frames_run["n"] <= 0:
            failures.append("控制端界面没有真的创建出来（一帧都没跑）")
    except Exception:
        traceback.print_exc()
        failures.append("控制端界面抛出异常")

    tk.Tk.mainloop = real_mainloop

    restore_configs(config_snap)
    restore_autostart(autostart_snap)
    restore_priority(priority_snap)
    failures.extend(layout_failures)

    print()
    print("=" * 62)
    if failures:
        print("  界面冒烟测试失败：")
        for f in failures:
            print("   -", f)
        print("=" * 62)
        return 1
    print("  界面冒烟测试全部通过 ✅")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass
    sys.exit(main())
