# -*- coding: utf-8 -*-
"""控制端的「远程命令」页 —— 用 SSH 在每台设备上执行命令。

和「壁纸」「通知」两页并排放在主窗口里（不是弹窗），右边那列设备列表在
执行过程中照样看得见。

    远程命令页（内嵌）────────────────────────────────────────┐
    │ 目标：○在线设备(48) ○全部已发现(52) ○自定义网段/IP      │
    │       自定义: [10.127.112.1-56        ] [取右侧选中][扫描]│
    │       解析提示：56 个地址（10.127.112.1 … 10.127.112.56）│
    │ 登录设置：私钥 -i [C:\\User\\.ssh\\id_ed25519] [浏览…]    │
    │           用户名 [Lonovo] 附加参数 [-o ConnectTimeout=10] │
    │           ☑首次连接自动接受主机密钥  并发[4] 超时[25]秒   │
    │           ssh 客户端：C:\\…\\OpenSSH\\ssh.exe（或红字警告）│
    │ 命令：预设[▾] [填入][追加]   方式[会话式 ▾]              │
    │       ┌ 多行文本框（一行一条命令）# 注释 ─────────────┐  │
    │ 结果：IP | 状态 | 退出码 | 远端回显（双击看完整回显）  │  │
    │ [▶ 发送到设备] [■ 停止] [生成批处理…] [导出结果…]  提示行│
    └────────────────────────────────────────────────────────┘

几条刻意的设计：
  * 目标默认就是「控制端已经发现的在线设备」—— 不用再手填网段（这是把远程
    命令做进控制端、而不是单独再做一个小工具的原因）。
  * 执行前一定弹确认框，把「实际执行的完整命令 + 台数」原样列出来；
    干的是"在几十台机器上以管理员身份跑命令"的事，不该点一下就出去。
  * 结果表里放的是**远端回显**（证据），双击能看到完整回显，
    因为"成功"这两个字本身给不了任何信息。
"""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import netutil as N
import sshcmd as SC
import ui

MONO = "Consolas"


def _mode_key(label: str) -> str:
    for k, v in SC.MODE_LABELS.items():
        if v == label:
            return k
    return SC.MODE_SESSION


def build_panel(parent: tk.Misc, core, cfg: dict, cfg_path: str,
                log=None, get_selection=None) -> tk.Frame:
    """把「远程命令」建成一个普通页面（Frame），塞进控制端的页签里即可。

    log            —— 控制端的日志回调 (msg, level)，执行过程会写进主界面日志
    get_selection  —— 取右边设备列表里当前选中的 IP（列表是控制端建的）
    """
    def say(msg: str, level: str = "info") -> None:
        if log:
            try:
                log(msg, level)
            except Exception:
                pass

    win = ttk.Frame(parent)
    # 底部动作条先 pack 占住底部：内容再长也不用滚到底才够得着按钮
    pin = ttk.Frame(win, padding=(0, 8, 0, 0))
    pin.pack(side="bottom", fill="x")
    scroll = ui.ScrollFrame(win)
    scroll.pack(fill="both", expand=True)
    body = scroll.inner

    saved = SC.normalize_cfg(dict(cfg.get("ssh") or {}))
    cfg["ssh"] = saved                      # 顺手把默认值写回配置
    state: dict = {"busy": False, "results": {}, "stop": threading.Event(),
                   "hint": "", "last_counts": ""}
    q: "queue.Queue[tuple[str, object]]" = queue.Queue()

    v_target_mode = tk.StringVar(value=saved["target_mode"])
    v_targets = tk.StringVar(value=saved["targets"])
    v_key = tk.StringVar(value=saved["key"])
    v_user = tk.StringVar(value=saved["user"])
    v_extra = tk.StringVar(value=saved["extra"])
    v_accept = tk.BooleanVar(value=bool(saved["accept_new_hostkey"]))
    v_workers = tk.StringVar(value=str(saved["workers"]))
    v_timeout = tk.StringVar(value=f"{float(saved['timeout']):g}")
    v_yes_wait = tk.StringVar(value=f"{float(saved['yes_wait']):g}")
    v_between = tk.StringVar(value=f"{float(saved['wait_between']):g}")
    v_mode = tk.StringVar(value=SC.MODE_LABELS.get(saved["mode"], SC.MODE_LABELS[SC.MODE_SESSION]))
    v_preset = tk.StringVar(value="")
    v_hint = tk.StringVar(value="")
    v_stat = tk.StringVar(value="还没有执行过")

    def card(title: str, pad=(0, 8)):
        box = ttk.Frame(body, style="Card.TFrame", padding=12)
        box.pack(fill="x", pady=pad)
        ttk.Label(box, text=title, style="Card.TLabel",
                  font=ui.pick_font(10, True)).pack(anchor="w", pady=(0, 6))
        return box

    def row(master):
        r = ttk.Frame(master, style="Card.TFrame")
        r.pack(fill="x", pady=2)
        return r

    # ================================================== ① 目标
    tgt_card = card("目标（发给哪些设备）")
    tgt_radio = row(tgt_card)
    radios: list[tk.Radiobutton] = []
    for key in (SC.TARGET_ONLINE, SC.TARGET_ALL, SC.TARGET_SPEC):
        rb = tk.Radiobutton(tgt_radio, text=SC.TARGET_LABELS[key], value=key,
                            variable=v_target_mode, command=lambda: refresh_hint(),
                            bg=ui.PANEL, fg=ui.FG, activebackground=ui.PANEL,
                            activeforeground=ui.ACCENT, selectcolor=ui.PANEL_2,
                            highlightthickness=0, bd=0, anchor="w",
                            font=ui.pick_font(9), cursor="hand2")
        rb.pack(side="left", padx=(0, 14))
        radios.append(rb)

    tgt_row = row(tgt_card)
    ttk.Label(tgt_row, text="自定义", style="Card.TLabel",
              foreground=ui.FG_DIM).pack(side="left", padx=(0, 6))
    tgt_entry = ttk.Entry(tgt_row, textvariable=v_targets)
    tgt_entry.pack(side="left", fill="x", expand=True)
    ui.tooltip(tgt_entry,
               "只在选「自定义网段/IP」时生效，四种写法都认（空格或逗号分隔多个）：\n"
               "  10.127.112.0/24              整个网段\n"
               "  10.127.112.1-10.127.112.60   地址范围\n"
               "  10.127.112.1-56              范围简写（前缀相同）\n"
               "  10.127.112.98                单台机器")
    pick_btn = ttk.Button(tgt_row, text="取右侧选中", width=10,
                          command=lambda: take_selection())
    pick_btn.pack(side="left", padx=(6, 0))
    ui.tooltip(pick_btn, "把右边「局域网设备」列表里选中的机器填进自定义框")
    scan_btn = ttk.Button(tgt_row, text="扫描", width=6, command=lambda: do_scan())
    scan_btn.pack(side="left", padx=(6, 0))
    ui.tooltip(scan_btn, "重新搜索局域网里的被控端（和右边那个 🔍 扫描 一样）")

    tgt_hint = ttk.Label(tgt_card, text="", style="Card.TLabel",
                         foreground=ui.FG_DIM, wraplength=620, justify="left")
    tgt_hint.pack(anchor="w", pady=(6, 0))

    # ================================================== ② 登录设置
    login_card = card("登录设置（就是 ssh -i 私钥 用户名@IP 这条命令的参数）")
    key_row = row(login_card)
    ttk.Label(key_row, text="私钥 -i", style="Card.TLabel",
              foreground=ui.FG_DIM, width=7).pack(side="left")
    key_entry = ttk.Entry(key_row, textvariable=v_key)
    key_entry.pack(side="left", fill="x", expand=True)
    ui.tooltip(key_entry, "ssh -i 后面那个私钥文件。\n"
                          "在哪台机器上运行控制端，哪台机器上就要有这把私钥。")
    ttk.Button(key_row, text="浏览…", width=7,
               command=lambda: pick_key()).pack(side="left", padx=(6, 0))

    user_row = row(login_card)
    ttk.Label(user_row, text="用户名", style="Card.TLabel",
              foreground=ui.FG_DIM, width=7).pack(side="left")
    ttk.Entry(user_row, textvariable=v_user, width=16).pack(side="left")
    ttk.Label(user_row, text="附加参数", style="Card.TLabel",
              foreground=ui.FG_DIM).pack(side="left", padx=(12, 6))
    extra_entry = ttk.Entry(user_row, textvariable=v_extra)
    extra_entry.pack(side="left", fill="x", expand=True)
    ui.tooltip(extra_entry, "默认 -o ConnectTimeout=10：连不通的机器 10 秒收手。\n"
                            "不加的话，每台不通的机器要等约两分钟。清空即完全照原始命令。")

    opt_row = row(login_card)
    cb_accept = ui.checkbutton(opt_row, "首次连接自动接受主机密钥", v_accept)
    cb_accept.pack(side="left")
    ui.tooltip(cb_accept,
               "等于替你在第一次连接时敲那个 yes（-o StrictHostKeyChecking=accept-new）。\n"
               "关掉的话程序仍然会替人回答 yes（和 UWF 工具一样），\n"
               "但主机密钥变过（重装系统）时会明确报错，不会闷着连。")
    for label, var, tip, width in (
        ("并发", v_workers, "同时连几台。默认 4；56 台串行会等很久，太大又容易被安全软件盯上。", 4),
        ("单条超时(秒)", v_timeout, "一条命令最多等多久，到点判超时并继续下一台。", 5),
        ("首次连接等待(秒)", v_yes_wait, "首次连接 / 远端 shell 起来最多等多久。", 5),
        ("命令间隔(秒)", v_between, "两条命令之间额外等多久（0 = 只靠远端回显判断，推荐）。", 5),
    ):
        ttk.Label(opt_row, text=label, style="Card.TLabel",
                  foreground=ui.FG_DIM).pack(side="left", padx=(12, 4))
        ent = ttk.Entry(opt_row, textvariable=var, width=width)
        ent.pack(side="left")
        ui.tooltip(ent, tip)

    ssh_hint = ttk.Label(login_card, text="", style="Card.TLabel",
                         wraplength=620, justify="left")
    ssh_hint.pack(anchor="w", pady=(6, 0))

    # ================================================== ③ 命令
    cmd_card = card("命令（一行一条，按顺序执行）")
    cmd_top = row(cmd_card)
    ttk.Label(cmd_top, text="常用命令", style="Card.TLabel",
              foreground=ui.FG_DIM).pack(side="left", padx=(0, 6))
    preset_box = ttk.Combobox(cmd_top, textvariable=v_preset, state="readonly",
                              width=26, values=[label for label, _c in SC.PRESETS])
    preset_box.pack(side="left")
    ui.tooltip(preset_box, "选一条常用命令，再点「填入」或「追加」")
    ttk.Button(cmd_top, text="填入", width=6,
               command=lambda: use_preset(False)).pack(side="left", padx=(6, 0))
    ttk.Button(cmd_top, text="追加", width=6,
               command=lambda: use_preset(True)).pack(side="left", padx=(4, 0))
    ttk.Label(cmd_top, text="执行方式", style="Card.TLabel",
              foreground=ui.FG_DIM).pack(side="left", padx=(16, 6))
    mode_box = ttk.Combobox(cmd_top, textvariable=v_mode, state="readonly", width=30,
                            values=[SC.MODE_LABELS[k] for k in
                                    (SC.MODE_SESSION, SC.MODE_ONESHOT)])
    mode_box.pack(side="left")
    ui.tooltip(mode_box,
               "会话式：登录一次，在同一个会话里逐条发命令，靠远端回显判断成败\n"
               "（判断「这条跑完了」用的是回显标记，不是死等固定秒数）。\n"
               "逐条独立：每条命令单独一次连接，能拿到真实退出码（0 = 成功），\n"
               "但慢一些、各条命令之间不共享状态；一条失败就跳过这台剩下的命令。")

    cmd_wrap = ttk.Frame(cmd_card, style="Card.TFrame")
    cmd_wrap.pack(fill="x", pady=(6, 0))
    cmd_text = tk.Text(cmd_wrap, height=5, wrap="none", font=(MONO, 9),
                       bg=ui.PANEL_2, fg=ui.FG, insertbackground=ui.FG,
                       relief="flat", highlightthickness=0, padx=8, pady=6)
    cmd_sb = ttk.Scrollbar(cmd_wrap, orient="vertical", command=cmd_text.yview)
    cmd_hsb = ttk.Scrollbar(cmd_wrap, orient="horizontal", command=cmd_text.xview)
    cmd_text.configure(yscrollcommand=cmd_sb.set, xscrollcommand=cmd_hsb.set)
    cmd_text.grid(row=0, column=0, sticky="nsew")
    cmd_sb.grid(row=0, column=1, sticky="ns")
    cmd_hsb.grid(row=1, column=0, sticky="ew")
    cmd_wrap.columnconfigure(0, weight=1)
    cmd_text.insert("1.0", saved["commands"] or "")
    ui.tooltip(cmd_text, "一行一条命令，例如：\n"
                         "    hostname\n"
                         "    uwfmgr filter disable\n"
                         "# 或 :: 开头的行是注释，空行忽略")

    # ================================================== ④ 结果
    res_card = ttk.Frame(body, style="Card.TFrame", padding=12)
    res_card.pack(fill="both", expand=True, pady=(0, 8))
    res_head = ttk.Frame(res_card, style="Card.TFrame")
    res_head.pack(fill="x")
    ttk.Label(res_head, text="执行结果（双击某一行看完整远端回显）",
              style="Card.TLabel", font=ui.pick_font(10, True)).pack(side="left")
    ttk.Label(res_head, textvariable=v_stat, style="Card.TLabel",
              foreground=ui.FG_DIM).pack(side="right")

    tree_wrap = ttk.Frame(res_card, style="Card.TFrame")
    tree_wrap.pack(fill="both", expand=True, pady=(6, 0))
    cols = ("ip", "state", "code", "evidence")
    tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", height=8)
    for c, t, w, stretch in (("ip", "IP 地址", 110, False),
                             ("state", "状态", 90, False),
                             ("code", "退出码", 60, False),
                             ("evidence", "远端回显（证据）", 330, True)):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="w", stretch=stretch)
    vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    tree_wrap.rowconfigure(0, weight=1)
    tree_wrap.columnconfigure(0, weight=1)
    tree.tag_configure(SC.STATE_OK, foreground=ui.OK)
    tree.tag_configure(SC.STATE_SENT, foreground=ui.WARN)
    tree.tag_configure(SC.STATE_PART, foreground=ui.WARN)
    tree.tag_configure(SC.STATE_FAIL, foreground=ui.ERR)
    tree.bind("<Double-1>", lambda _e: show_detail())

    # ================================================== 底部动作条
    run_btn = ttk.Button(pin, text="▶  发送到设备", style="Accent.TButton",
                         command=lambda: do_run())
    run_btn.pack(side="left", ipadx=8, ipady=3)
    ui.tooltip(run_btn, "按当前目标执行上面的命令（执行前会弹确认框列出完整命令）")
    stop_btn = ttk.Button(pin, text="■  停止", command=lambda: do_stop(),
                          state="disabled")
    stop_btn.pack(side="left", padx=(8, 0))
    ui.tooltip(stop_btn, "已经开始的机器会收尾，不再派新的机器")
    bat_btn = ttk.Button(pin, text="生成批处理…", command=lambda: export_bat())
    bat_btn.pack(side="left", padx=(8, 0))
    ui.tooltip(bat_btn, "生成一个不依赖本软件的 .bat（用 echo y | ssh 自动应答 yes），"
                        "换台机器也能跑")
    exp_btn = ttk.Button(pin, text="导出结果…", command=lambda: export_log())
    exp_btn.pack(side="left", padx=(8, 0))
    ttk.Label(pin, textvariable=v_hint, style="TLabel",
              foreground=ui.FG_DIM).pack(side="left", padx=(12, 0))

    # ================================================== 逻辑
    def collect() -> dict:
        """把界面上的值收集成配置（顺便把越界值夹回安全范围）。"""
        return SC.normalize_cfg({
            "key": v_key.get().strip(),
            "user": v_user.get().strip(),
            "extra": v_extra.get().strip(),
            "accept_new_hostkey": bool(v_accept.get()),
            "mode": _mode_key(v_mode.get()),
            "workers": v_workers.get(),
            "timeout": v_timeout.get(),
            "yes_wait": v_yes_wait.get(),
            "wait_between": v_between.get(),
            "target_mode": v_target_mode.get(),
            "targets": v_targets.get(),
            "commands": cmd_text.get("1.0", "end").strip(),
        })

    def save() -> dict:
        s = collect()
        cfg["ssh"] = s
        try:
            N.save_json(cfg_path, cfg)
        except Exception as e:
            v_hint.set(f"设置没保存成功：{e}")
        return s

    def device_lists() -> tuple[list[str], list[str]]:
        """从控制端的设备表里取 (在线, 全部)。"""
        window = float(getattr(core, "online_window", 90.0))
        now = time.time()
        try:
            with core._dev_lock:
                devices = dict(core.devices)
        except Exception:
            devices = {}
        online, every = [], []
        for key, d in devices.items():
            ip = str(d.get("ip") or key)
            every.append(ip)
            if now - float(d.get("last", 0) or 0) <= window:
                online.append(ip)
        return SC.clean_ips(online), SC.clean_ips(every)

    def resolve() -> tuple[list[str], list[str]]:
        mode = v_target_mode.get()
        online, every = device_lists()
        if mode == SC.TARGET_ONLINE:
            return online, []
        if mode == SC.TARGET_ALL:
            return every, []
        return SC.spec_ips(v_targets.get())

    def refresh_hint(*_a) -> None:
        """把当前目标解析成人话，顺手更新本机 ssh / 私钥的状态。"""
        mode = v_target_mode.get()
        online, every = device_lists()
        if mode == SC.TARGET_ONLINE:
            ips, notes = online, []
            text = f"将发给**当前在线**的 {len(ips)} 台设备"
        elif mode == SC.TARGET_ALL:
            ips, notes = every, []
            text = f"将发给发现过的全部 {len(ips)} 台设备（含已离线的）"
        else:
            ips, notes = SC.spec_ips(v_targets.get())
            text = (f"解析出 {len(ips)} 个地址"
                    + (f"（{ips[0]} … {ips[-1]}）" if ips else ""))
        if not ips and mode != SC.TARGET_SPEC:
            text += "　—— 还没扫描到设备？点「扫描」或在右边点 🔍 扫描"
        bad = [n for n in notes if "忽略" in n]
        tgt_hint.configure(text=text + ("；" + "；".join(notes) if notes else ""),
                           foreground=ui.ERR if bad else ui.FG_DIM)
        state["last_counts"] = f"{len(online)}/{len(every)}"

        ok_ssh, info_ssh = SC.ssh_available(collect())
        ok_key, info_key = SC.key_state(v_key.get())
        if not ok_ssh:
            ssh_hint.configure(text="⚠ " + info_ssh, foreground=ui.ERR)
        elif not ok_key:
            ssh_hint.configure(
                text=f"ssh 客户端：{info_ssh}　·　⚠ {info_key}", foreground=ui.WARN)
        else:
            ssh_hint.configure(
                text=f"ssh 客户端：{info_ssh}　·　私钥已找到　·　"
                     f"实际执行的命令：{SC.display_command(collect(), ips[0] if ips else '10.127.112.98')}",
                foreground=ui.FG_DIM)

        # 单选按钮上带上台数，一眼能看出"发得出去几台"
        try:
            radios[0].configure(text=f"{SC.TARGET_LABELS[SC.TARGET_ONLINE]}（{len(online)}）")
            radios[1].configure(text=f"{SC.TARGET_LABELS[SC.TARGET_ALL]}（{len(every)}）")
        except Exception:
            pass

    def pick_key() -> None:
        p = filedialog.askopenfilename(
            title="选择 SSH 私钥文件",
            initialdir=os.path.dirname(v_key.get() or "") or None,
            filetypes=[("所有文件", "*.*")])
        if p:
            v_key.set(p)
            refresh_hint()

    def take_selection() -> None:
        """把右侧设备列表里选中的机器填进自定义框。"""
        ips: list[str] = []
        if callable(get_selection):
            try:
                ips = SC.clean_ips(get_selection() or [])
            except Exception:
                ips = []
        if not ips:
            v_hint.set("右边设备列表里先选中几台机器")
            return
        old = SC.spec_ips(v_targets.get())[0]
        merged = SC.clean_ips(list(old) + ips)
        v_targets.set(" ".join(merged))
        v_target_mode.set(SC.TARGET_SPEC)
        v_hint.set(f"已把选中的 {len(ips)} 台加进自定义目标（共 {len(merged)} 台）")
        refresh_hint()

    def use_preset(append: bool) -> None:
        label = v_preset.get()
        for lb, cmd in SC.PRESETS:
            if lb == label:
                cur = cmd_text.get("1.0", "end").strip()
                if append and cur:
                    cmd_text.insert("end", "\n" + cmd + "\n")
                else:
                    cmd_text.delete("1.0", "end")
                    cmd_text.insert("1.0", cmd + "\n")
                v_hint.set(f"已{'追加' if append else '填入'}预设：{lb}")
                return
        v_hint.set("先选一条常用命令")

    def do_scan() -> None:
        v_hint.set("正在扫描局域网设备…")
        threading.Thread(target=lambda: core.scan(deep=None), daemon=True).start()

    def set_busy(busy: bool) -> None:
        state["busy"] = busy
        run_btn.configure(state="disabled" if busy else "normal",
                          text="执行中…" if busy else "▶  发送到设备")
        stop_btn.configure(state="normal" if busy else "disabled")
        mode_box.configure(state="disabled" if busy else "readonly")

    def do_stop() -> None:
        if state["busy"]:
            state["stop"].set()
            v_hint.set("正在停止：已开始的机器会收尾，不再派新的")
            say("远程命令：已请求停止", "warn")

    def do_run() -> None:
        if state["busy"]:
            return
        s = save()
        cmds = SC.parse_commands(s["commands"])
        if not cmds:
            messagebox.showwarning("没有命令", "先在上面填要执行的命令（一行一条）。")
            return
        ips, _notes = resolve()
        if not ips:
            messagebox.showwarning(
                "没有目标设备",
                "当前目标里一台设备都没有。\n\n"
                "·「在线设备」来自控制端的设备表 —— 先点「扫描」；\n"
                "· 或者选「自定义网段/IP」自己填，例如 10.127.112.1-56。")
            return
        ok_ssh, info_ssh = SC.ssh_available(s)
        if not ok_ssh:
            messagebox.showerror("没有 ssh 客户端", info_ssh)
            return
        ok_key, info_key = SC.key_state(s["key"])
        if not ok_key and not messagebox.askyesno(
                "私钥不存在",
                f"{info_key}\n\n仍要继续执行吗？\n"
                f"（如果控制端是在别的机器上运行、私钥在那台机器上，可以忽略这个提示）"):
            return
        if not messagebox.askyesno(
                "确认执行",
                SC.plan_text(ips, s, cmds)
                + "\n\n这些命令会在远端机器上真正执行（权限取决于私钥对应的账号）。\n"
                  "确定继续？"):
            return

        state["stop"] = threading.Event()
        state["results"] = {}
        state["total"] = len(ips)
        tree.delete(*tree.get_children())
        v_stat.set(f"0/{len(ips)}")
        set_busy(True)
        v_hint.set(f"正在执行：0/{len(ips)} 台")
        say(f"远程命令：{len(ips)} 台 · 方式 {SC.MODE_LABELS.get(s['mode'], s['mode'])} "
            f"· 并发 {s['workers']}", "info")
        for cmd in cmds:
            say(f"    将执行：{cmd}", "dim")
        say(f"    实际命令：{SC.display_command(s, ips[0], cmds[0])}", "dim")
        stop = state["stop"]

        def work() -> None:
            def on_result(r: SC.HostResult) -> None:
                q.put(("result", r))

            def on_log(msg: str, level: str = "info") -> None:
                q.put(("log", (msg, level)))

            try:
                SC.run_hosts(ips, s, cmds, on_result=on_result, stop=stop,
                             log=on_log)
            except Exception as e:
                q.put(("log", (f"远程命令异常：{e}", "err")))
            finally:
                q.put(("done", len(state["results"])))

        threading.Thread(target=work, daemon=True).start()

    def update_row(r: SC.HostResult) -> None:
        state["results"][r.ip] = r
        code = ""
        if r.outputs and r.outputs[-1][1] is not None:
            code = str(r.outputs[-1][1])
        evidence = SC.first_lines(r.evidence or r.error, 2, 130) or r.error
        vals = (r.ip, r.text, code, evidence)
        if tree.exists(r.ip):
            tree.item(r.ip, values=vals, tags=(r.state,))
        else:
            tree.insert("", "end", iid=r.ip, values=vals, tags=(r.state,))
        total = int(state.get("total") or 0)
        done = len(state["results"])
        v_stat.set(f"{done}/{total}" if total else str(done))
        if total and done < total:
            v_hint.set(f"正在执行：{done}/{total} 台（{r.ip} {r.text}）")
        elif total:
            v_hint.set(f"全部回来了：{done}/{total} 台，正在汇总…")

    def show_detail() -> None:
        sel = tree.selection()
        if not sel:
            return
        r = state["results"].get(sel[0])
        if r is None:
            return
        w = tk.Toplevel(win)
        w.title(f"远端回显 · {r.ip}")
        w.configure(bg=ui.BG)
        head = [f"{r.ip}　{r.text}　耗时 {r.seconds:.1f} 秒"]
        if r.error:
            head.append(f"原因：{r.error}")
        txt = tk.Text(w, wrap="none", font=(MONO, 9), bg="#15171c", fg=ui.FG,
                      insertbackground=ui.FG, relief="flat", padx=10, pady=8)
        sb = ttk.Scrollbar(w, orient="vertical", command=txt.yview)
        hsb = ttk.Scrollbar(w, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)
        lines = list(head)
        lines.append("=" * 60)
        for i, (cmd, code, out) in enumerate(r.outputs, 1):
            lines.append(f"── 命令 {i}/{len(r.outputs)}：{cmd}"
                         + (f"　（退出码 {code}）" if code is not None else ""))
            lines.append(out or "（没有回显）")
            lines.append("")
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")
        txt.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        w.rowconfigure(0, weight=1)
        w.columnconfigure(0, weight=1)
        ttk.Button(w, text="关闭", command=w.destroy).grid(row=2, column=0, pady=6)
        ui.center(w, 900, 560)

    def drain() -> None:
        """把工作线程发回来的结果搬到界面上（Tk 控件只能主线程碰）。"""
        done_n = None
        while True:
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                break
            if kind == "result":
                update_row(payload)
            elif kind == "log":
                msg, level = payload
                say(msg, level)
            elif kind == "done":
                done_n = payload
        if done_n is not None:
            set_busy(False)
            results = list(state["results"].values())
            st = SC.summarize(results)
            stopped = state["stop"].is_set()
            v_hint.set(("已停止：" if stopped else "已完成：")
                       + f"成功 {st[SC.STATE_OK]} · 已下发 {st[SC.STATE_SENT]} · "
                         f"部分 {st[SC.STATE_PART]} · 失败 {st[SC.STATE_FAIL]}")
            say(f"远程命令{'已停止' if stopped else '完成'}："
                f"成功 {st[SC.STATE_OK]} · 已下发 {st[SC.STATE_SENT]} · "
                f"部分成功 {st[SC.STATE_PART]} · 失败 {st[SC.STATE_FAIL]}",
                "ok" if st[SC.STATE_OK] and not st[SC.STATE_FAIL] else "warn")
            fails = [r for r in results if r.state == SC.STATE_FAIL]
            for r in fails[:8]:
                say(f"    ❌ {r.ip}：{r.error or r.evidence}", "err")

    def export_log() -> None:
        results = sorted(state["results"].values(),
                         key=lambda r: [int(x) for x in r.ip.split(".")])
        if not results:
            messagebox.showinfo("还没有结果", "先执行一次，再导出。")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=f"远程命令结果_{time.strftime('%Y%m%d_%H%M')}.txt",
            filetypes=[("文本文件", "*.txt")])
        if not path:
            return
        s = collect()
        lines = [f"远程命令结果　{time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"方式：{SC.MODE_LABELS.get(s['mode'])}　并发：{s['workers']}",
                 f"命令：{' | '.join(SC.parse_commands(s['commands']))}",
                 "=" * 70]
        for r in results:
            lines.append(f"\n[{r.ip}] {r.text}　耗时 {r.seconds:.1f} 秒")
            if r.error:
                lines.append(f"    原因：{r.error}")
            for i, (cmd, code, out) in enumerate(r.outputs, 1):
                lines.append(f"    {i}. {cmd}"
                             + (f"　（退出码 {code}）" if code is not None else ""))
                for ln in (out or "").splitlines():
                    lines.append("       " + ln)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            messagebox.showinfo("导出成功", "已导出到：\n" + path)
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def export_bat() -> None:
        s = save()
        cmds = SC.parse_commands(s["commands"])
        ips, _ = resolve()
        if not cmds or not ips:
            messagebox.showwarning("还没准备好", "先填命令、选好目标，再生成批处理。")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".bat",
            initialfile=f"远程命令_{len(ips)}台.bat",
            filetypes=[("批处理文件", "*.bat")])
        if not path:
            return
        try:
            SC.save_batch(path, SC.build_batch(ips, s, cmds))
            messagebox.showinfo(
                "已生成",
                f"批处理已生成：\n{path}\n\n"
                f"它做的是同一件事：{len(ips)} 台机器，各执行 {len(cmds)} 条命令，\n"
                f"用 echo y | ssh 自动应答首次连接的 yes。\n"
                f"（脚本内部刻意只用英文 —— cmd 在不同代码页下解析中文批处理会出错）")
        except Exception as e:
            messagebox.showerror("生成失败", str(e))

    # ================================================== 定时刷新
    alive = {"run": True}

    def tick() -> None:
        if not alive["run"]:
            return
        try:
            drain()
            # 设备台数 / 本机 ssh 状态定期刷一下（扫描是后台线程在跑，随时会变）
            mode = v_target_mode.get()
            if mode != SC.TARGET_SPEC:
                online, every = device_lists()
                if f"{len(online)}/{len(every)}" != state["last_counts"]:
                    refresh_hint()
            win.after(400, tick)
        except tk.TclError:
            alive["run"] = False

    for var in (v_targets, v_key, v_user, v_extra, v_mode, v_accept,
                v_workers, v_timeout, v_yes_wait, v_between):
        var.trace_add("write", lambda *_a: refresh_hint())
    v_target_mode.trace_add("write", lambda *_a: refresh_hint())
    # 命令框不是变量，改完离开焦点就顺手存一下（免得切来切去白填一遍）
    cmd_text.bind("<FocusOut>", lambda _e: save())
    refresh_hint()
    v_hint.set("目标默认就是右边已经发现的在线设备；填好命令后点「发送到设备」")
    # 窗口销毁后不能再排定时回调（否则 Tcl 会报 invalid command name）
    win.bind("<Destroy>", lambda _e: alive.__setitem__("run", False))
    win.after(400, tick)
    return win
