# -*- coding: utf-8 -*-
"""控制端的「发送通知」编辑器 —— 现在是主窗口里的**一个页面**，不是弹窗。

以前它是个 Toplevel 浮窗：点「📢 发送通知…」→ 弹出来 → 填完关掉，过程中主界面
和设备列表全被挡住。现在直接嵌进主窗口的「通知」页（`build_editor()` 返回一个
Frame），和「壁纸」「设置」并排，右侧设备列表始终看得见。

    通知页（内嵌）──────────────────────────────────┐
    │ 内容：标题 / 正文 / 预设                      │
    │ 提醒方式：声音 · 静音 · 紧急 · 稍后提醒       │
    │ 按钮（最多 5 个，只能打开网址）               │
    │ 预览（示意）                                  │
    │ ▸ 高级选项（第三行 / 图片 / 署名 / 分组 / …） │
    │ [📢 发送通知]  □只发本机（测试用）  提示行    │
    └───────────────────────────────────────────┘

点「高级选项」才展开第三行文字、图标/大图、署名、分组、进度条、唯一标识、
过期时间、只进通知中心 —— 这些字段都在，只是不占地方。

字段与 BurntToast 的 `New-BurntToastNotification` 参数一一对应，详见 toastspec.py。
故意没做的：`ActivatedAction` / `DismissedAction`（PowerShell 脚本块，从网络接收
等于让别人在你机器上执行命令）、`DataBinding` / `Column` / `CustomTimestamp`。
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import netutil as N
import toastspec as TS
import ui

MAX_UI_BUTTONS = TS.MAX_BUTTONS

PRESETS = [
    ("普通通知", {
        "text": ["通知", ""],
        "sound": "Default", "urgent": False, "snooze": False,
    }),
    ("维护公告", {
        "text": ["系统维护通知", "今晚 22:00 起进行网络割接，预计 30 分钟"],
        "sound": "Reminder", "urgent": True, "snooze": True,
        "duration": TS.DURATION_LONG, "expire_minutes": 1440,
    }),
    ("紧急提醒", {
        "text": ["紧急通知", "请立即保存工作并断开网络连接"],
        "sound": "Alarm2", "urgent": True, "snooze": False,
        "duration": TS.DURATION_LONG,
    }),
    ("到期提醒", {
        "text": ["密码即将到期", "请在 3 天内修改域账号密码"],
        "sound": "IM", "urgent": False, "snooze": True,
        "expire_minutes": 2880,
    }),
]


def _card(parent, title: str = "", pad=(0, 6)):
    box = ttk.Frame(parent, style="Card.TFrame", padding=10)
    box.pack(fill="x", pady=pad)
    if title:
        ttk.Label(box, text=title, style="Card.TLabel",
                  font=ui.pick_font(10, True)).pack(anchor="w", pady=(0, 5))
    return box


def _row(parent):
    r = ttk.Frame(parent, style="Card.TFrame")
    r.pack(fill="x", pady=2)
    return r


def build_editor(parent: tk.Misc, core, cfg: dict, cfg_path: str) -> tk.Frame:
    """把「发送通知」编辑器建成一个**普通页面**，返回这个页面（Frame）。

    调用方（控制端）把它塞进主窗口的页签里就行 —— 不再有弹窗，
    设备列表和日志在编辑通知时始终可见。
    """
    # 下面沿用 win 这个名字指代"这个页面"，widget 的父子关系一眼能看懂
    win = ttk.Frame(parent)
    # 发送区钉在页面底部（先 pack，占住底部这条），内容再长也不用滚到底才够得着
    pin = ttk.Frame(win, padding=(0, 6, 0, 0))
    pin.pack(side="bottom", fill="x")
    scroll = ui.ScrollFrame(win)
    scroll.pack(fill="both", expand=True)
    body = scroll.inner

    saved = dict(cfg.get("last_toast") or {})
    if not saved:
        saved = dict(TS.empty_spec())

    state = {
        "assets": {},          # 资源名 -> 字节
        "preview": {},         # 资源名 -> PhotoImage（防被 GC）
        "advanced": False,
        "busy": False,
    }

    def s_text(i: int) -> str:
        lines = list(saved.get("text") or [])
        return lines[i] if i < len(lines) else ""

    # ---------------- 变量
    v_title = tk.StringVar(value=s_text(0))
    v_body = tk.StringVar(value=s_text(1))
    v_body3 = tk.StringVar(value=s_text(2))
    v_sound = tk.StringVar(value=saved.get("sound") or TS.DEFAULT_SOUND)
    v_silent = tk.BooleanVar(value=bool(saved.get("silent")))
    v_urgent = tk.BooleanVar(value=bool(saved.get("urgent")))
    v_snooze = tk.BooleanVar(value=bool(saved.get("snooze")))
    v_suppress = tk.BooleanVar(value=bool(saved.get("suppress_popup")))
    v_attr = tk.StringVar(value=saved.get("attribution") or "")
    v_logo = tk.StringVar(value=saved.get("app_logo") or "")
    v_hero = tk.StringVar(value=saved.get("hero_image") or "")
    v_hid = tk.StringVar(value=(saved.get("header") or {}).get("id") or "")
    v_htitle = tk.StringVar(value=(saved.get("header") or {}).get("title") or "")
    v_ptitle = tk.StringVar(value=(saved.get("progress") or {}).get("title") or "")
    v_pstatus = tk.StringVar(value=(saved.get("progress") or {}).get("status") or "")
    v_pvalue = tk.DoubleVar(value=float((saved.get("progress") or {}).get("value", -1)))
    v_pindet = tk.BooleanVar(value=float((saved.get("progress") or {}).get("value", -1)) < 0)
    v_uid = tk.StringVar(value=saved.get("unique_id") or "")
    v_expire = tk.IntVar(value=int(saved.get("expire_minutes") or 0))
    v_local = tk.BooleanVar(value=False)
    v_hint = tk.StringVar(value="")
    # 横幅停留时长：下拉里显示人话，值还是 toastspec 的英文标识
    _dur0 = str(saved.get("duration") or TS.DURATION_SHORT)
    if _dur0 not in TS.DURATIONS:
        _dur0 = TS.DURATION_SHORT
    v_dur_label = tk.StringVar(value=TS.DURATION_LABELS[_dur0])
    v_duration = tk.StringVar(value=_dur0)

    def _sync_duration(*_a):
        """下拉换成英文标识；选「一直显示」时顺手把声音/紧急调成能配合的组合。"""
        label = v_dur_label.get()
        value = TS.DURATION_SHORT
        for k, v in TS.DURATION_LABELS.items():
            if v == label:
                value = k
        v_duration.set(value)
        if value == TS.DURATION_UNTIL:
            changed = []
            if v_sound.get() not in TS.LOOPING_SOUNDS:
                v_sound.set(TS.DEFAULT_LOOPING_SOUND)
                changed.append(f"声音改成 {TS.DEFAULT_LOOPING_SOUND}（循环响铃）")
            if v_silent.get():
                v_silent.set(False)
                changed.append("取消静音")
            if v_urgent.get():
                v_urgent.set(False)
                changed.append("取消「紧急」（Windows 的场景只能有一个）")
            v_hint.set("「一直显示到用户处理」= 循环响铃、必须用户点掉；已自动：" +
                       ("、".join(changed) if changed else "无需调整"))
        refresh_preview()

    # ================= 内容
    box = _card(body, "内容")
    r = _row(box)
    ttk.Label(r, text="标题", style="Card.TLabel", foreground=ui.FG_DIM,
              width=6).pack(side="left")
    ttk.Entry(r, textvariable=v_title, width=52).pack(side="left", fill="x", expand=True)
    ttk.Label(box, text="预设：", style="Card.TLabel",
              foreground=ui.FG_DIM).pack(anchor="w", pady=(6, 2))
    prow = ttk.Frame(box, style="Card.TFrame")
    prow.pack(fill="x")
    for name, preset in PRESETS:
        ttk.Button(prow, text=name, width=9,
                   command=lambda p=preset: apply_preset(p)).pack(side="left", padx=(0, 6))
    r = _row(box)
    ttk.Label(r, text="正文", style="Card.TLabel", foreground=ui.FG_DIM,
              width=6).pack(side="left")
    ttk.Entry(r, textvariable=v_body, width=52).pack(side="left", fill="x", expand=True)

    # ================= 提醒方式
    box = _card(body, "提醒方式")
    r = _row(box)
    ttk.Label(r, text="声音", style="Card.TLabel", foreground=ui.FG_DIM,
              width=6).pack(side="left")
    ttk.Combobox(r, textvariable=v_sound, values=list(TS.SOUNDS),
                 state="readonly", width=11).pack(side="left")
    ui.checkbutton(r, "静音", v_silent).pack(side="left", padx=(10, 0))
    cb_urgent = ui.checkbutton(r, "紧急", v_urgent)
    cb_urgent.pack(side="left", padx=(6, 0))
    ui.tooltip(cb_urgent, "紧急通知：能被 Windows 当成重要通知处理 ——\n"
                          "可穿透专注助手（勿扰），在通知中心里排在前面。\n"
                          "（前提：被控端允许「紧急通知」，安装时已自动打开）")
    cb_snooze = ui.checkbutton(r, "稍后提醒+关闭", v_snooze)
    cb_snooze.pack(side="left", padx=(6, 0))
    ui.tooltip(cb_snooze, "用系统自带的那两个按钮（稍后提醒 / 关闭），\n"
                          "不需要自己填网址")

    # 停留时长：Windows 只给这几档（短≈5 秒 / 长≈25 秒 / 一直显示到用户处理）
    r = _row(box)
    ttk.Label(r, text="停留", style="Card.TLabel", foreground=ui.FG_DIM,
              width=6).pack(side="left")
    dur_box = ttk.Combobox(r, textvariable=v_dur_label,
                           values=[TS.DURATION_LABELS[k] for k in TS.DURATIONS],
                           state="readonly", width=28)
    dur_box.pack(side="left")
    ui.tooltip(dur_box, "横幅在屏幕上停多久（Windows 只给这三档）：\n"
                        "短 ≈ 5 秒；长 ≈ 25 秒；\n"
                        "一直显示到用户处理 = 循环响铃、必须点掉（像闹钟）。\n"
                        "提示：这是「停多久」，不是「留多久」——\n"
                        "通知中心里保留多久看高级选项里的「过期」。")
    v_dur_label.trace_add("write", _sync_duration)

    # ================= 按钮
    box = _card(body, f"按钮（可选，最多 {MAX_UI_BUTTONS} 个，点了只会打开网址）")
    btn_rows = ttk.Frame(box, style="Card.TFrame")
    btn_rows.pack(fill="x")
    buttons: list[dict] = [
        {"content": tk.StringVar(value=b.get("content", "")),
         "arguments": tk.StringVar(value=b.get("arguments", "")),
         "activation_type": str(b.get("activation_type") or TS.ACTIVATION_PROTOCOL)}
        for b in (saved.get("buttons") or [])
    ]

    def redraw_buttons():
        for child in btn_rows.winfo_children():
            child.destroy()
        for idx, item in enumerate(buttons):
            r = _row(btn_rows)
            ttk.Label(r, text=f"{idx + 1}", style="Card.TLabel",
                      foreground=ui.FG_DIM, width=2).pack(side="left")
            ttk.Entry(r, textvariable=item["content"], width=12).pack(side="left")
            ttk.Entry(r, textvariable=item["arguments"]).pack(
                side="left", fill="x", expand=True, padx=4)
            if item["activation_type"] == TS.ACTIVATION_SYSTEM:
                kind = ttk.Combobox(r, values=["稍后提醒", "关闭"], width=9,
                                    state="readonly")
                kind.set("稍后提醒" if item["arguments"].get() == "snooze" else "关闭")
                kind.pack(side="left")
            else:
                ttk.Label(r, text="https://…", style="Card.TLabel",
                          foreground=ui.FG_DIM, width=9).pack(side="left")
            ttk.Button(r, text="删", width=3,
                       command=lambda i=idx: remove_button(i)).pack(side="left", padx=4)
        add_btn.configure(state="normal" if len(buttons) < MAX_UI_BUTTONS else "disabled")

    def add_button():
        if len(buttons) >= MAX_UI_BUTTONS:
            return
        buttons.append({"content": tk.StringVar(value=""),
                        "arguments": tk.StringVar(value=""),
                        "activation_type": TS.ACTIVATION_PROTOCOL})
        redraw_buttons()
        refresh_preview()

    def remove_button(idx: int):
        if 0 <= idx < len(buttons):
            buttons.pop(idx)
            redraw_buttons()
            refresh_preview()

    add_btn = ttk.Button(box, text="＋ 添加按钮", command=add_button)
    add_btn.pack(anchor="w", pady=(4, 0))
    redraw_buttons()

    # ================= 预览
    box = _card(body, "预览（示意，实际样式由 Windows 决定）")
    prev = tk.Label(box, bg=ui.PANEL_2, fg=ui.FG, justify="left", anchor="nw",
                    font=ui.pick_font(9), width=58, height=6, padx=10, pady=6)
    prev.pack(fill="x")

    # ================= 高级选项（默认收起）
    adv_btn = ttk.Button(body, text="▸ 高级选项（第三行文字 / 图片 / 署名 / 分组 / 进度条 / 标识 / 过期）")
    adv_btn.pack(anchor="w", pady=(2, 0))
    adv = ttk.Frame(body)
    adv_holder: dict[str, ttk.Frame] = {}
    img_labels: dict[str, tk.Label] = {}

    def build_advanced():
        """高级区懒创建（不点不占地方）。

        这里**必须是单列、能自适应宽度的排版**：通知页和右侧设备列表挤在一个窗口里，
        页宽只有 ~470px，而以前这里是"两列网格"（需要 ~790px）—— 结果右半边整片
        被推到可视区之外，用户看到的就是「进度 / 进度标题 / 标识 / 过期 被挡住了」。
        现在每行都是"固定宽度的字段名 + 会伸展的输入框"，页宽多少都不会溢出；
        一行里控件多了就拆成两行（见下面进度条那三行）。
        """
        box = _card(adv, "高级选项")

        def line(label: str, width: int = 8):
            """一行：左边固定宽度的字段名，右边放会伸展的控件。"""
            r = ttk.Frame(box, style="Card.TFrame")
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=label, style="Card.TLabel", foreground=ui.FG_DIM,
                      width=width).pack(side="left")
            return r

        def hint(parent, text: str):
            ttk.Label(parent, text=text, style="Card.TLabel",
                      foreground=ui.FG_DIM).pack(side="left", padx=(6, 0))

        r = line("第三行")
        ttk.Entry(r, textvariable=v_body3, width=20).pack(
            side="left", fill="x", expand=True)
        r = line("署名")
        ttk.Entry(r, textvariable=v_attr, width=20).pack(
            side="left", fill="x", expand=True)
        r = line("分组")
        ttk.Entry(r, textvariable=v_hid, width=6).pack(side="left")
        ttk.Entry(r, textvariable=v_htitle, width=12).pack(
            side="left", fill="x", expand=True, padx=(6, 0))
        hint(line(""), "编号 + 标题（同类通知归到一组）")
        for label, var, key in (("应用图标", v_logo, "app_logo"),
                                ("大图", v_hero, "hero_image")):
            r = line(label)
            ttk.Entry(r, textvariable=var, width=12).pack(
                side="left", fill="x", expand=True)
            ttk.Button(r, text="浏览…", width=7,
                       command=lambda v=var, k=key: pick_image(v, k)).pack(
                side="left", padx=4)
            ttk.Button(r, text="清", width=3,
                       command=lambda v=var: v.set("")).pack(side="left")
            lb = ttk.Label(r, text="", style="Card.TLabel", foreground=ui.FG_DIM)
            lb.pack(side="left", padx=(6, 0))
            img_labels[key] = lb

        # ---- 进度条：三个字段分三行，谁也不挤谁
        r = line("进度")
        ttk.Entry(r, textvariable=v_pstatus, width=12).pack(
            side="left", fill="x", expand=True)
        hint(r, "状态文字")
        ui.checkbutton(r, "不确定", v_pindet).pack(side="left", padx=(8, 0))
        r = line("")
        ttk.Entry(r, textvariable=v_ptitle, width=12).pack(
            side="left", fill="x", expand=True)
        hint(r, "进度标题（可选）")
        r = line("")
        ttk.Scale(r, from_=0.0, to=1.0, variable=v_pvalue).pack(
            side="left", fill="x", expand=True)
        ttk.Label(r, textvariable=v_pvalue, style="Card.TLabel",
                  foreground=ui.FG_DIM, width=5).pack(side="left", padx=6)
        hint(line(""), "状态文字必填；两个都空 = 不显示进度条")

        r = line("标识")
        ttk.Entry(r, textvariable=v_uid, width=12).pack(
            side="left", fill="x", expand=True)
        hint(r, "留空=任务号")
        r = line("过期")
        ttk.Spinbox(r, from_=0, to=10080, textvariable=v_expire, width=7).pack(side="left")
        hint(r, "分钟后消失（0=不过期）")
        r = line("")
        ui.checkbutton(r, "只进通知中心（不弹横幅）", v_suppress).pack(side="left")

    def toggle_advanced():
        if state["advanced"]:
            adv.pack_forget()
            adv_btn.configure(
                text="▸ 高级选项（第三行文字 / 图片 / 署名 / 分组 / 进度条 / 标识 / 过期）")
            state["advanced"] = False
        else:
            if not adv_holder:
                build_advanced()
                adv_holder["built"] = adv
            # adv 的父容器是 body（页面内容容器），所以这里不能写 before=bottom，
            # 那会跨父容器 pack 并抛 TclError —— 顺手 pack 到 body 末尾即可，
            # 位置正好在「高级选项」按钮下面。
            adv.pack(fill="x")
            adv_btn.configure(text="▾ 收起高级选项")
            state["advanced"] = True
        win.update_idletasks()

    adv_btn.configure(command=toggle_advanced)

    # ================= 页脚（固定在底部）：发送
    row = ttk.Frame(pin)
    row.pack(fill="x")
    send_btn = ttk.Button(row, text="📢  发送通知", style="Accent.TButton")
    send_btn.pack(side="left", ipadx=12, ipady=2)
    ui.checkbutton(row, "只发本机（测试用）", v_local).pack(side="left", padx=10)
    ui.tooltip(send_btn, "发给所有在线被控端；快捷键 Ctrl+Enter")
    ttk.Label(pin, textvariable=v_hint, style="TLabel",
              foreground=ui.ERR, wraplength=760, justify="left").pack(anchor="w", pady=(6, 0))

    # ---------------- 交互
    def apply_preset(preset: dict):
        lines = list(preset.get("text") or [])
        v_title.set(lines[0] if lines else "")
        v_body.set(lines[1] if len(lines) > 1 else "")
        v_body3.set(lines[2] if len(lines) > 2 else "")
        v_sound.set(preset.get("sound") or TS.DEFAULT_SOUND)
        v_silent.set(bool(preset.get("silent")))
        v_urgent.set(bool(preset.get("urgent")))
        v_snooze.set(bool(preset.get("snooze")))
        v_suppress.set(bool(preset.get("suppress_popup")))
        v_expire.set(int(preset.get("expire_minutes") or 0))
        v_attr.set(preset.get("attribution") or "")
        _dur = str(preset.get("duration") or TS.DURATION_SHORT)
        v_dur_label.set(TS.DURATION_LABELS.get(_dur, TS.DURATION_LABELS[TS.DURATION_SHORT]))
        v_hint.set("已套用预设，可直接改内容或按「发送」")
        refresh_preview()

    def pick_image(var: tk.StringVar, key: str):
        path = filedialog.askopenfilename(
            title=f"选择{'图标' if key == 'app_logo' else '大图'}",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.gif"), ("所有文件", "*.*")],
            parent=win.winfo_toplevel())
        if not path:
            return
        name = os.path.basename(path)
        ok, err = TS.check_asset(name)
        if not ok:
            v_hint.set(f"这张图不能用：{err}")
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            v_hint.set(f"读取图片失败：{e}")
            return
        if len(data) > TS.MAX_ASSET_LEN:
            v_hint.set(f"图片太大（{len(data) // 1024} KB > {TS.MAX_ASSET_LEN // 1024} KB）")
            return
        state["assets"][name] = data
        var.set(name)
        v_hint.set("")
        try:
            from controller import make_thumbnail

            photo, _info = make_thumbnail(path, 88, 40)
        except Exception:
            photo = None
        state["preview"][key] = photo
        lb = img_labels.get(key)
        if lb is not None:
            if photo:
                lb.configure(image=photo, text="", width=photo.width(), height=photo.height())
            else:
                lb.configure(image="", text=name[:10])

    def current_spec() -> dict:
        out: dict = {
            "text": [v_title.get(), v_body.get(), v_body3.get()],
            "attribution": v_attr.get(),
            "sound": v_sound.get(),
            "silent": bool(v_silent.get()),
            "urgent": bool(v_urgent.get()),
            "snooze": bool(v_snooze.get()),
            "suppress_popup": bool(v_suppress.get()),
            "unique_id": v_uid.get(),
            "expire_minutes": int(v_expire.get() or 0),
            "duration": v_duration.get(),
            "buttons": [
                {"content": b["content"].get().strip(),
                 "arguments": (b["arguments"].get().strip()
                               if b["activation_type"] == TS.ACTIVATION_PROTOCOL
                               else ("snooze" if "稍后" in b["arguments"].get() else "dismiss")),
                 "activation_type": b["activation_type"]}
                for b in buttons
            ],
        }
        if v_logo.get().strip():
            out["app_logo"] = v_logo.get().strip()
        if v_hero.get().strip():
            out["hero_image"] = v_hero.get().strip()
        if v_hid.get().strip() or v_htitle.get().strip():
            out["header"] = {"id": v_hid.get().strip(), "title": v_htitle.get().strip()}
        if not v_pindet.get():
            if v_pstatus.get().strip() or v_ptitle.get().strip():
                out["progress"] = {"title": v_ptitle.get(), "status": v_pstatus.get(),
                                   "value": float(v_pvalue.get())}
        elif v_pstatus.get().strip():
            out["progress"] = {"title": v_ptitle.get(), "status": v_pstatus.get(),
                               "value": -1.0}
        return out

    def refresh_preview(*_a):
        s = current_spec()
        lines = [x.strip() for x in s["text"] if x.strip()]
        w = 54
        out = ["┌" + "─" * w + "┐"]
        title = lines[0] if lines else "（标题）"
        out.append("│ 🖼 " + title[:w - 4].ljust(w - 3) + "│")
        for line in lines[1:3]:
            out.append("│    " + line[:w - 4].ljust(w - 3) + "│")
        if s.get("hero_image"):
            out.append("│    [ 大图 ]".ljust(w + 2) + "│")
        prog = s.get("progress")
        if prog:
            val = prog.get("value", -1)
            bar = "▓" * 10 if val < 0 else "▓" * int(round(val * 10)) + "░" * (
                10 - int(round(val * 10)))
            out.append("│    " + (prog.get("status") or "")[:w - 6].ljust(w - 5) + "│")
            out.append("│    " + bar + (" 不确定" if val < 0 else f" {int(val * 100)}%")
                       + " " * 3 + "│")
        for b in s["buttons"]:
            if b["content"]:
                out.append("│    " + ("[ " + b["content"] + " ]").ljust(w - 5) + "│")
        foot = []
        if s.get("attribution"):
            foot.append(s["attribution"])
        if s.get("header", {}).get("title"):
            foot.append("分组:" + s["header"]["title"])
        if s.get("urgent"):
            foot.append("紧急")
        if s.get("silent"):
            foot.append("静音")
        elif s.get("sound"):
            foot.append("声音:" + s["sound"])
        if s.get("suppress_popup"):
            foot.append("不弹横幅")
        if s.get("duration") == TS.DURATION_LONG:
            foot.append("停留:长")
        elif s.get("duration") == TS.DURATION_UNTIL:
            foot.append("停留:直到处理")
        if s.get("expire_minutes"):
            foot.append(f"{s['expire_minutes']}分钟后消失")
        if foot:
            out.append("│ " + " · ".join(foot)[:w - 1].ljust(w - 1) + "│")
        out.append("└" + "─" * w + "┘")
        prev.configure(text="\n".join(out))

    for var in (v_title, v_body, v_body3, v_sound, v_silent, v_urgent, v_snooze,
                v_suppress, v_attr, v_logo, v_hero, v_hid, v_htitle, v_pstatus,
                v_pindet, v_uid, v_expire):
        var.trace_add("write", refresh_preview)
    refresh_preview()

    def do_send():
        if state["busy"]:
            return
        ok, clean, err = TS.normalize(current_spec())
        if not ok:
            v_hint.set("还不能发送：" + err)
            return
        wanted = TS.asset_names(clean)
        assets = {n: state["assets"][n] for n in wanted if n in state["assets"]}
        missing = [n for n in wanted if n not in assets]
        state["busy"] = True
        send_btn.configure(state="disabled", text="发送中…")
        cfg["last_toast"] = clean
        N.save_json(cfg_path, cfg)
        v_hint.set("已发送，结果看主界面的设备列表…" if not missing
                   else "已发送；但这些图片没有选本地文件，通知里不会带："
                        + "、".join(missing))

        def work():
            try:
                task = core.push_toast(clean, assets, local_only=bool(v_local.get()))
                where = "本机" if v_local.get() else "局域网"
                v_hint.set(f"已向{where}广播通知 · 任务号 {task}")
            except Exception as e:
                v_hint.set(f"发送失败：{e}")
            finally:
                state["busy"] = False
                try:
                    send_btn.configure(state="normal", text="📢  发送")
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()

    send_btn.configure(command=do_send)
    # 快捷键挂在主窗口上：编辑器已经是页面了，没有自己的窗口
    try:
        win.winfo_toplevel().bind("<Control-Return>", lambda _e: do_send())
    except Exception:
        pass
    return win
