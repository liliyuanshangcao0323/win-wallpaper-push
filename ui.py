# -*- coding: utf-8 -*-
"""两个界面共用的小控件与配色（避免控制端/被控端重复写 UI 代码）。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------- 配色
BG = "#1b1d23"
PANEL = "#242732"
PANEL_2 = "#2c303c"
FG = "#e8eaf0"
FG_DIM = "#9aa0b0"
ACCENT = "#4c8dff"
OK = "#3ecf8e"
WARN = "#ffc857"
ERR = "#ff6b6b"
BORDER = "#363a48"


def pick_font(size: int = 10, bold: bool = False):
    """优先用微软雅黑，保证中文不发虚。"""
    import tkinter.font as tkfont

    try:
        families = set(tkfont.families())
    except Exception:
        families = set()
    for name in ("Microsoft YaHei UI", "微软雅黑", "Microsoft YaHei", "Segoe UI"):
        if name in families:
            return (name, size, "bold") if bold else (name, size)
    return ("TkDefaultFont", size, "bold") if bold else ("TkDefaultFont", size)


def apply_theme(root: tk.Misc) -> ttk.Style:
    """套用统一的深色主题。"""
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass

    try:
        root.configure(bg=BG)
    except Exception:
        pass

    base = pick_font(10)
    st.configure(
        ".",
        background=BG,
        foreground=FG,
        font=base,
        fieldbackground=PANEL_2,
        bordercolor=BORDER,
        focuscolor=ACCENT,
    )
    st.configure("TFrame", background=BG)
    st.configure("Card.TFrame", background=PANEL)
    st.configure("TLabel", background=BG, foreground=FG)
    st.configure("Dim.TLabel", background=BG, foreground=FG_DIM, font=pick_font(9))
    st.configure("Card.TLabel", background=PANEL, foreground=FG)
    st.configure("Title.TLabel", background=BG, foreground=FG, font=pick_font(15, True))
    st.configure("Sub.TLabel", background=BG, foreground=FG_DIM, font=pick_font(9))

    st.configure("TButton", background=PANEL_2, foreground=FG, borderwidth=0, padding=(12, 7))
    st.map("TButton", background=[("active", "#3a3f4f"), ("disabled", "#22242c")],
           foreground=[("disabled", FG_DIM)])
    st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", font=pick_font(10, True))
    st.map("Accent.TButton", background=[("active", "#3d7ae8"), ("disabled", "#2a3550")],
           foreground=[("disabled", "#8f9bb5")])

    st.configure("TEntry", fieldbackground=PANEL_2, foreground=FG, insertcolor=FG,
                 bordercolor=BORDER, padding=6)
    st.configure("TCombobox", fieldbackground=PANEL_2, background=PANEL_2,
                 foreground=FG, arrowcolor=FG, padding=4, bordercolor=BORDER)
    st.map("TCombobox", fieldbackground=[("readonly", PANEL_2)], foreground=[("readonly", FG)])

    st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                 rowheight=27, borderwidth=0)
    st.configure("Treeview.Heading", background=PANEL_2, foreground=FG_DIM,
                 relief="flat", font=pick_font(9, True), padding=(6, 5))
    st.map("Treeview", background=[("selected", "#33518c")], foreground=[("selected", "#ffffff")])

    st.configure("TLabelframe", background=BG, foreground=FG_DIM, bordercolor=BORDER)
    st.configure("TLabelframe.Label", background=BG, foreground=FG_DIM, font=pick_font(9, True))

    st.configure("Vertical.TScrollbar", background=PANEL_2, troughcolor=BG,
                 bordercolor=BG, arrowcolor=FG_DIM, relief="flat")
    st.configure("TSeparator", background=BORDER)

    # 页签（控制端的「壁纸 / 通知 / 设置」）：不要弹窗，一切都在同一个窗口里
    st.configure("TNotebook", background=BG, borderwidth=0,
                 tabmargins=(0, 2, 0, 0))
    st.configure("TNotebook.Tab", background=PANEL, foreground=FG_DIM,
                 padding=(18, 8), font=pick_font(10), borderwidth=0)
    st.map("TNotebook.Tab",
           background=[("selected", PANEL_2), ("active", "#333846")],
           foreground=[("selected", FG)])

    # 左右分栏可拖动（左边操作、右边设备列表）
    st.configure("TPanedwindow", background=BG)
    st.configure("Sash", sashthickness=9, gripcount=0, background=BORDER)

    root.option_add("*TCombobox*Listbox.background", PANEL_2)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    return st


def center(win: tk.Tk, width: int, height: int) -> None:
    """把窗口居中显示。"""
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = max(0, (sw - width) // 2)
    y = max(0, (sh - height) // 3)
    win.geometry(f"{width}x{height}+{x}+{y}")


def fit(win: tk.Tk, width: int, height: int, margin: int = 90) -> None:
    """按「内容实际需要多大」定窗口尺寸，再受屏幕高度限制，最后居中。

    为什么不能只写死一个尺寸：pack 布局在空间不够时会**从最后放的控件开始
    挤**。控制端内容一多，最先被挤没的就是底部那块日志面板 —— 而日志正是
    排查「扫不到设备」时唯一能看的东西。这里量一下内容需要多高，够得着就
    撑开（屏幕放不下时才退让），并且只放大不缩小，避免选完图片后日志被挤掉。
    """
    win.update_idletasks()
    need_w, need_h = win.winfo_reqwidth(), win.winfo_reqheight()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()

    w = min(max(width, need_w + 10), max(640, sw - 60))
    h = min(max(height, need_h + 10), max(480, sh - margin))
    prev = getattr(win, "_wpp_fit_height", 0)
    win._wpp_fit_height = max(h, prev)
    center(win, w, win._wpp_fit_height)


def checkbutton(master, text: str, variable, command=None, **kw) -> tk.Checkbutton:
    """深色主题下的复选框。

    ttk 的 Checkbutton 在 clam 主题里背景色固定跟随全局样式，塞进
    Card.TFrame（面板色）里会露出一块底色不同的方块，所以这里直接用
    tk 原生控件 + 显式配色。
    """
    return tk.Checkbutton(
        master,
        text=text,
        variable=variable,
        command=command,
        bg=PANEL,
        fg=FG,
        activebackground=PANEL,
        activeforeground=ACCENT,
        selectcolor=PANEL_2,
        highlightthickness=0,
        bd=0,
        anchor="w",
        cursor="hand2",
        font=pick_font(9),
        **kw,
    )


class Tooltip:
    """鼠标停一会儿显示的小提示。

    为什么要它：界面上那些"深度扫描 = 广播之外再逐台单播……"之类的说明行，
    第一次用有用，天天用就是噪音。把它们挂到控件上，界面才清爽得下来。
    """

    def __init__(self, widget, text: str, delay: int = 450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip: tk.Toplevel | None = None
        self.after_id: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._cancel()
        try:
            self.after_id = self.widget.after(self.delay, self._show)
        except Exception:
            pass

    def _cancel(self) -> None:
        if self.after_id:
            try:
                self.widget.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None

    def _show(self) -> None:
        if self.tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 14
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{x}+{y}")
            tk.Label(tip, text=self.text, justify="left", bg=PANEL_2, fg=FG,
                     relief="solid", borderwidth=1, font=pick_font(9),
                     padx=8, pady=5, wraplength=380).pack()
            self.tip = tip
        except Exception:
            self.tip = None

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


def tooltip(widget, text: str) -> Tooltip:
    """给任意控件挂一条悬浮提示。"""
    return Tooltip(widget, text)


class ScrollFrame(ttk.Frame):
    """带竖向滚动的内容容器。

    为什么要它：页面一旦塞进主窗口，就没有"窗口自己长大"这回事了 ——
    内容比可用高度高的时候，pack 会从最后放的控件开始挤掉，
    用户看到的就成了"少了一块"。套一层滚动，多出来的部分滚一下就能看全，
    任何屏幕高度下都不会有控件被裁掉。
    """

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")

        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _on_inner(self, _e=None) -> None:
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:
            pass

    def _on_canvas(self, e) -> None:
        try:
            self.canvas.itemconfigure(self._win, width=e.width)
        except Exception:
            pass

    def _bind_wheel(self, _e=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _unbind_wheel(self, _e=None) -> None:
        try:
            self.canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass

    def _wheel(self, e) -> None:
        try:
            self.canvas.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass


class LogPane(ttk.Frame):
    """带颜色分级、可滚动的日志面板。"""

    def __init__(self, master, height: int = 10, **kw):
        super().__init__(master, **kw)
        self.text = tk.Text(
            self,
            height=height,
            bg="#15171c",
            fg=FG,
            insertbackground=FG,
            relief="flat",
            wrap="word",
            font=pick_font(9),
            padx=10,
            pady=8,
            state="disabled",
            highlightthickness=0,
        )
        sb = ttk.Scrollbar(self, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.tag_configure("info", foreground=FG)
        self.text.tag_configure("ok", foreground=OK)
        self.text.tag_configure("warn", foreground=WARN)
        self.text.tag_configure("err", foreground=ERR)
        self.text.tag_configure("dim", foreground=FG_DIM)

    def write(self, line: str, level: str = "info") -> None:
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n", level)
        # 控制内存：超过 800 行就砍掉开头
        if int(self.text.index("end-1c").split(".")[0]) > 800:
            self.text.delete("1.0", "200.0")
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
