# -*- coding: utf-8 -*-
"""通知（Toast）规格：控制端负责编辑、被控端负责渲染，两边共用同一套校验。

字段与 BurntToast 的 `New-BurntToastNotification` 参数一一对应：

| 规格字段 | BurntToast 参数 | 说明 |
|---|---|---|
| `text` | `-Text` | 最多 3 行，第一行是标题 |
| `app_logo` | `-AppLogo` | 应用图标，传的是**本工具的资源名**（走 TCP 下发，落地成本地路径） |
| `hero_image` | `-HeroImage` | 大图，同上 |
| `attribution` | `-Attribution` | 底部署名 |
| `sound` / `silent` | `-Sound` / `-Silent` | 二者互斥 |
| `snooze` | `-SnoozeAndDismiss` | 系统自带的「稍后提醒 / 关闭」 |
| `header` | `-Header` | 分组标题（`New-BTHeader -Id -Title`） |
| `progress` | `-ProgressBar` | `New-BTProgressBar -Title -Status -Value/-Indeterminate` |
| `unique_id` | `-UniqueIdentifier` | 同标识的新通知会顶掉旧的 |
| `expire_minutes` | `-ExpirationTime` | 多少分钟后从通知中心移除 |
| `suppress_popup` | `-SuppressPopup` | 只进通知中心，不弹横幅 |
| `urgent` | `-Urgent` | 「重要通知」，可穿透专注助手 |
| `duration` | （XML `duration` / `scenario`） | 横幅停留多久：短 / 长 / 直到用户处理 |
| `buttons` | `-Button` | `New-BTButton -Content -Arguments -ActivationType` |

关于**停留时长**（Windows 只给这几档，没有"随便填秒数"这回事）：

| `duration` | 实际效果 | 怎么实现 |
|---|---|---|
| `short`（默认） | 横幅约 5 秒后自动收起，进通知中心 | 不加任何属性 |
| `long` | 横幅约 25 秒 | XML `duration="long"` |
| `until_dismissed` | **一直显示到用户处理**（像闹钟：循环响铃，不自动消失） | XML `duration="long"` + `scenario="alarm"` + 循环音频 |

> 注意区分：`expire_minutes` 管的是**通知中心里留多久**，`duration` 管的是**横幅在屏幕上停多久**。
>
> `until_dismissed` 会循环播放声音直到用户点掉，比较"凶"，默认不用；
> 它和 `urgent` 不能同时用（Windows 的 `scenario` 只能有一个值），同时给会被拒收。

**故意不支持的三类**（都要在文档里写清楚）：

* `ActivatedAction` / `DismissedAction` —— 那是 PowerShell **ScriptBlock**。
  从网络接收并执行它，等于让局域网里任何人在这台机器上跑任意命令。
* `DataBinding` / `Column` / `CustomTimestamp` —— 远程推送场景用不上。
* 按钮的 `Arguments` 只允许 `http/https`（协议 activation）或 `dismiss`/`snooze`
  （系统 activation），避免"点一下就执行任意命令"。

被控端拿到网络来的规格后**必须再校验一遍**（`normalize`），不合法就拒绝并回报
原因 —— 控制端可能是别人伪造的，被控端不能盲信。
"""

from __future__ import annotations

import re

MAX_TEXTS = 3
MAX_TEXT_LEN = 200
MAX_ATTRIBUTION_LEN = 200
MAX_BUTTONS = 5
MAX_BUTTON_LEN = 60
MAX_URL_LEN = 500
MAX_UNIQUE_ID_LEN = 64
MAX_ASSET_LEN = 4 * 1024 * 1024          # 单张图 4 MB 上限
MIN_LEN_HINT = 1

# BurntToast 1.1.0 的 ValidateSet，直接抄过来（GUI 下拉就用这个）
SOUNDS = (
    "Default", "IM", "Mail", "Reminder", "SMS",
    "Alarm", "Alarm2", "Alarm3", "Alarm4", "Alarm5",
    "Alarm6", "Alarm7", "Alarm8", "Alarm9", "Alarm10",
    "Call", "Call2", "Call3", "Call4", "Call5",
    "Call6", "Call7", "Call8", "Call9", "Call10",
)
DEFAULT_SOUND = "Default"

# 横幅停留时长（Windows 只认这几档，没有"随便填秒数"）
DURATION_SHORT = "short"          # 约 5 秒，默认
DURATION_LONG = "long"            # 约 25 秒
DURATION_UNTIL = "until_dismissed"   # 一直显示到用户处理（闹钟场景 + 循环响铃）
DURATIONS = (DURATION_SHORT, DURATION_LONG, DURATION_UNTIL)
DURATION_LABELS = {
    DURATION_SHORT: "短（约 5 秒）",
    DURATION_LONG: "长（约 25 秒）",
    DURATION_UNTIL: "一直显示到用户处理（循环响铃）",
}
# 「一直显示」必须配循环音频；这几个声音本来就是循环用的
LOOPING_SOUNDS = tuple(s for s in SOUNDS if s.startswith(("Alarm", "Call")))
DEFAULT_LOOPING_SOUND = "Alarm2"

ACTIVATION_PROTOCOL = "protocol"
ACTIVATION_SYSTEM = "system"
SYSTEM_ARGS_OK = ("dismiss", "snooze")
URL_SCHEMES = ("http", "https")
ASSET_EXTS = (".png", ".jpg", ".jpeg", ".gif")

_ASSET_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_URL_RE = re.compile(r"^(https?)://[^\s]{3,}$", re.IGNORECASE)
_HEADER_ID_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")
_BAD_NAME_CHARS = set('\\/:*?"<>|')


def empty_spec() -> dict:
    """一份空规格（界面直接拿它做初值）。"""
    return {
        "text": ["", "", ""],
        "app_logo": "",
        "hero_image": "",
        "attribution": "",
        "sound": DEFAULT_SOUND,
        "silent": False,
        "urgent": False,
        "snooze": False,
        "suppress_popup": False,
        "unique_id": "",
        "expire_minutes": 0,
        "duration": DURATION_SHORT,
        "header": {"id": "", "title": ""},
        "progress": {"title": "", "status": "", "value": -1.0},
        "buttons": [],
    }


# ---------------------------------------------------------------- 单项校验

def clean_text_lines(raw) -> list[str]:
    """文本行：去掉空行、裁剪长度、最多 3 行。"""
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for item in list(raw or [])[:MAX_TEXTS]:
        line = str(item or "").strip()
        if not line:
            continue
        out.append(line[:MAX_TEXT_LEN])
    return out


def check_url(url: str) -> tuple[bool, str]:
    """按钮地址：只允许 http/https。"""
    text = (url or "").strip()
    if not text:
        return False, "按钮地址不能为空"
    if len(text) > MAX_URL_LEN:
        return False, f"按钮地址太长（>{MAX_URL_LEN}）"
    if not _URL_RE.match(text):
        return False, "按钮地址只允许 http:// 或 https:// 开头"
    return True, ""


def check_asset(name: str) -> tuple[bool, str]:
    """资源名（图片文件名）：不允许路径、不允许控制字符，扩展名必须是图片。

    允许中文等非 ASCII 名字（截图文件名常常是中文），只挡「路径穿越」和
    「拿别的文件当图片」这两类问题 —— 被控端存盘时会再取一次 basename。
    """
    text = (name or "").strip()
    if not text:
        return True, ""                      # 空 = 没图，合法
    if len(text) > 64:
        return False, "图片名太长（最多 64 个字符）"
    if any(c in _BAD_NAME_CHARS for c in text) or text.startswith(".") or ".." in text:
        return False, "图片名不能带路径或这些字符 \\ / : * ? \" < > |"
    if any(ord(c) < 32 for c in text):
        return False, "图片名里有不可见字符"
    if not text.lower().endswith(ASSET_EXTS):
        return False, "图片只支持 png / jpg / jpeg / gif"
    return True, ""


def normalize(data: dict) -> tuple[bool, dict, str]:
    """校验并规范化规格。返回 (是否合法, 干净规格, 错误说明)。

    被控端对**任何**网络来的规格都要先过这一关；不合法一律拒绝。
    顺手把不认识 / 危险字段丢弃：`activated_action`、`dismissed_action`
    这类脚本块即使被塞进来也不会往下传。
    """
    if not isinstance(data, dict):
        return False, {}, "规格不是对象"

    # 明确拦掉脚本执行相关字段（哪怕只出现也要拒绝，并说清原因）
    for bad in ("activated_action", "dismissed_action", "event_data_variable"):
        if data.get(bad):
            return False, {}, (f"规格里带了 {bad}（远程脚本），出于安全原因拒绝执行。"
                               f"脚本只能在本机设置。")

    spec = empty_spec()

    spec["text"] = clean_text_lines(data.get("text"))
    if not spec["text"]:
        return False, {}, "至少要有一行文字（第一行会当标题）"

    for key in ("app_logo", "hero_image"):
        name = str(data.get(key) or "").strip()
        ok, err = check_asset(name)
        if not ok:
            return False, {}, f"{key}：{err}"
        spec[key] = name
    if spec["app_logo"] and spec["hero_image"] and spec["app_logo"] == spec["hero_image"]:
        return False, {}, "图标和大图不能是同一个文件"

    spec["attribution"] = str(data.get("attribution") or "").strip()[:MAX_ATTRIBUTION_LEN]

    spec["silent"] = bool(data.get("silent"))
    sound = str(data.get("sound") or DEFAULT_SOUND).strip()
    if sound not in SOUNDS:
        return False, {}, f"不认识的声音「{sound}」（可选：{', '.join(SOUNDS[:6])} …）"
    spec["sound"] = sound

    spec["urgent"] = bool(data.get("urgent"))
    spec["snooze"] = bool(data.get("snooze"))
    spec["suppress_popup"] = bool(data.get("suppress_popup"))

    # ---------------- 横幅停留时长
    raw_duration = str(data.get("duration") or DURATION_SHORT).strip().lower()
    # 容错：写 long/短/short 之外的常见说法
    alias = {"": DURATION_SHORT, "short": DURATION_SHORT, "default": DURATION_SHORT,
             "短": DURATION_SHORT, "long": DURATION_LONG, "长": DURATION_LONG,
             "until_dismissed": DURATION_UNTIL, "until-dismissed": DURATION_UNTIL,
             "persist": DURATION_UNTIL, "一直": DURATION_UNTIL}
    duration = alias.get(raw_duration, "")
    if not duration:
        return False, {}, (f"不认识的停留时长「{raw_duration}」"
                           f"（可选：{'、'.join(DURATIONS)}）")
    if duration == DURATION_UNTIL:
        # 「一直显示」= 闹钟场景（循环响铃）。Windows 的 scenario 只能有一个值，
        # 所以不能同时又要"紧急"（穿透专注助手）—— 明确报错，别让用户以为两个都生效了。
        if bool(data.get("urgent")):
            return False, {}, ("「紧急」和「一直显示到用户处理」不能同时用："
                               "Windows 的 scenario 只能有一个值。"
                               "要穿透专注助手就选「紧急」，要停到用户点掉就选停留时长")
        if spec["silent"]:
            return False, {}, ("「一直显示到用户处理」必须响铃（靠循环声音把通知留在屏幕上），"
                               "不能同时勾「静音」。想安静就用「长（约 25 秒）」")
        if sound not in LOOPING_SOUNDS:
            return False, {}, (
                "「一直显示到用户处理」靠循环铃声把通知留在屏幕上，"
                f"所以要选一个循环音（{'、'.join(LOOPING_SOUNDS[:6])} …），"
                f"当前是「{sound}」。想安静地用「长（约 25 秒）」")
    spec["duration"] = duration

    spec["unique_id"] = str(data.get("unique_id") or "").strip()[:MAX_UNIQUE_ID_LEN]

    try:
        expire = int(data.get("expire_minutes") or 0)
    except (TypeError, ValueError):
        return False, {}, "过期时间必须是分钟数"
    if expire < 0 or expire > 10080:            # 最多 7 天
        return False, {}, "过期时间要在 0（不过期）到 10080 分钟（7 天）之间"
    spec["expire_minutes"] = expire

    header = data.get("header") or {}
    if isinstance(header, dict) and (header.get("id") or header.get("title")):
        hid = str(header.get("id") or "").strip()
        title = str(header.get("title") or "").strip()[:MAX_TEXT_LEN]
        if not hid or not _HEADER_ID_RE.match(hid):
            return False, {}, "分组编号只能用字母数字（1~16 位）"
        if not title:
            return False, {}, "分组标题不能为空"
        spec["header"] = {"id": hid, "title": title}
    else:
        spec.pop("header", None)     # 没分组就别把空壳带给被控端

    progress = data.get("progress") or {}
    if isinstance(progress, dict):
        raw_value = progress.get("value")
        try:
            has_progress = bool(progress.get("status") or progress.get("title")) or (
                raw_value is not None and float(raw_value) != -1.0)
        except (TypeError, ValueError):
            return False, {}, "进度值必须是 0~1 的数字（-1 = 不确定进度）"
        if has_progress:
            try:
                value = float(raw_value if raw_value is not None else -1)
            except (TypeError, ValueError):
                return False, {}, "进度值必须是 0~1 的数字（-1 = 不确定进度）"
            if value != -1 and not 0.0 <= value <= 1.0:
                return False, {}, "进度值要在 0~1 之间（-1 = 不确定进度）"
            status = str(progress.get("status") or "").strip()[:MAX_TEXT_LEN]
            ptitle = str(progress.get("title") or "").strip()[:MAX_TEXT_LEN]
            # BurntToast 的 New-BTProgressBar 里 -Status 是必填参数，而它就是进度条
            # 下面显示的那行字。以前这里会偷偷补一句「处理中」—— 用户看到一行自己
            # 没写过的"处理中"，根本不知道哪来的。现在改成直接说清楚要写什么。
            if not status:
                return False, {}, (
                    "进度条要写一句状态文字（就是进度条下面那行字，"
                    "例如「正在下发 40%」）；不想显示进度条就把 progress 整段去掉")
            spec["progress"] = {"title": ptitle, "status": status, "value": value}
        else:
            # 没填进度条就把这段去掉：空着的 progress 传到 PowerShell 那边
            # 会变成「Status 参数是空字符串」的报错，整条通知都弹不出来。
            spec.pop("progress", None)

    buttons = data.get("buttons") or []
    if not isinstance(buttons, list):
        return False, {}, "按钮必须是数组"
    if len(buttons) > MAX_BUTTONS:
        return False, {}, f"按钮最多 {MAX_BUTTONS} 个"
    clean_buttons: list[dict] = []
    for raw in buttons:
        if not isinstance(raw, dict):
            return False, {}, "按钮格式不对"
        content = str(raw.get("content") or "").strip()[:MAX_BUTTON_LEN]
        if not content:
            return False, {}, "按钮文字不能为空"
        kind = str(raw.get("activation_type") or ACTIVATION_PROTOCOL).strip().lower()
        args = str(raw.get("arguments") or "").strip()
        if kind == ACTIVATION_SYSTEM:
            if args.lower() not in SYSTEM_ARGS_OK:
                return False, {}, (f"系统按钮只支持 "
                                   f"{'、'.join(SYSTEM_ARGS_OK)}")
            args = args.lower()
        else:
            kind = ACTIVATION_PROTOCOL
            ok, err = check_url(args)
            if not ok:
                return False, {}, f"按钮「{content}」：{err}"
        clean_buttons.append({"content": content, "arguments": args,
                              "activation_type": kind})
    spec["buttons"] = clean_buttons

    return True, spec, ""


def summarize(spec: dict) -> str:
    """一行摘要，给日志和设备列表用。"""
    text = spec.get("text") or []
    title = text[0] if text else "（无标题）"
    bits = [f"「{title}」"]
    if len(text) > 1:
        bits.append(f"+{len(text) - 1} 行")
    if spec.get("urgent"):
        bits.append("紧急")
    dur = spec.get("duration") or DURATION_SHORT
    if dur == DURATION_LONG:
        bits.append("停留:长(约25秒)")
    elif dur == DURATION_UNTIL:
        bits.append("停留:直到用户处理")
    if spec.get("silent"):
        bits.append("静音")
    elif spec.get("sound") and spec["sound"] != DEFAULT_SOUND:
        bits.append(f"声音:{spec['sound']}")
    if spec.get("buttons"):
        bits.append(f"{len(spec['buttons'])} 个按钮")
    if spec.get("hero_image"):
        bits.append("大图")
    if spec.get("app_logo"):
        bits.append("图标")
    if spec.get("header", {}).get("title"):
        bits.append(f"分组:{spec['header']['title']}")
    if spec.get("progress"):
        bits.append("进度条")
    if spec.get("suppress_popup"):
        bits.append("只进通知中心")
    return " ".join(bits)


def asset_names(spec: dict) -> list[str]:
    """规格里引用到的图片资源名（去重）。"""
    names: list[str] = []
    for key in ("app_logo", "hero_image"):
        name = spec.get(key) or ""
        if name and name not in names:
            names.append(name)
    return names
