# -*- coding: utf-8 -*-
"""控制端 / 被控端 共享的网络协议定义。

设计说明
--------
壁纸图片动辄几百 KB 到几 MB，直接用 UDP 广播分片传输非常不可靠：
局域网广播没有重传机制，任何一个分片丢失，接收端拿到的就是一张损坏的图。

因此本协议采用业界标准做法（RustDesk / 数字标牌系统常用）：

    1. 控制端用 UDP 广播一个很小的「公告包」(announce)，里面只有
       task_id / 文件名 / 大小 / sha256 / TCP 端口 —— 几百字节，丢一两个也无所谓。
    2. 被控端收到公告后，用 TCP 主动回连控制端，把图片完整拉下来。
       TCP 自带重传与顺序保证，再配合 sha256 校验，图片一定完整。

对外表现仍然是「控制端广播发包，各机器自动换壁纸」，但可靠性高得多。
"""

from __future__ import annotations

import json

# ---------------------------------------------------------------- 常量

MAGIC = "WPPR/1"          # 协议标识 + 版本，版本不匹配的包会被直接丢弃
UDP_PORT = 38571          # 广播 / 发现端口（控制端与被控端都监听这个端口）
TCP_PORT = 38572          # 壁纸文件传输端口（仅控制端监听）
REPLY_PORT = 38573        # 回执专用端口（仅控制端监听，收 pong / applied）

# 为什么要单独开一个 REPLY_PORT？
#   Windows 允许多个 socket 用 SO_REUSEADDR 绑定同一个 UDP 端口（这是广播能被
#   多方同时收到的前提），但此时「单播」数据报只会投递给其中**一个** socket。
#   如果回执也发往 38571，同机运行控制端+被控端时回执可能被投给被控端自己，
#   控制端就永远收不到确认。用一个独占端口收单播，结果就确定了。
HEADER_MAX = 8192         # 单行 JSON 头的最大字节数
CHUNK = 256 * 1024        # TCP 收发分块大小

# 消息类型
MSG_ANNOUNCE = "announce"   # 控制端 -> 广播：有新壁纸了
MSG_PING = "ping"           # 控制端 -> 广播 / 单播：谁在线？
MSG_PONG = "pong"           # 被控端 -> 单播：我在线（对 ping 的应答）
MSG_APPLIED = "applied"     # 被控端 -> 单播：壁纸换好了 / 换失败了
MSG_GET = "get"             # 被控端 -> 单播(TCP)：把壁纸文件给我
MSG_HELLO = "hello"         # 被控端 -> 广播：我上线了（主动报到，控制端不用等扫描）
MSG_TOAST = "toast"         # 控制端 -> 广播：弹一条通知（带 toastspec 规格）
MSG_SETNAME = "setname"     # 控制端 -> 广播：改被控端「通知上显示的应用名」

# 为什么通知也走广播 + TCP 拉取，而不塞进一个 UDP 包：
#   纯文字的通知规格只有几百字节，UDP 广播完全够用；但通知可以带图标/大图，
#   图片是几十上百 KB，UDP 广播没有重传、丢了就是花屏。所以沿用壁纸那套：
#   UDP 广播「规格 + 资源清单（名字/大小/sha256）」，被控端再按需用 TCP 拉图片。

# TCP 请求里指定资源名时用这个字段（不填就是拉壁纸本身）
FIELD_ASSET = "asset"

# 改「通知上显示的应用名」用的字段（MSG_SETNAME）
# 空字符串 = 恢复默认名字。被控端会把它写进自己的配置并立刻重新注册，
# 所以这是**持久化**的改动（重启后仍然是新名字）。
FIELD_APP_NAME = "app_name"
MAX_APP_NAME = 40           # 名字最长多少个字符（和 toast.MAX_APP_NAME_LEN 一致）

# 被控端主动报到的默认间隔（秒）。设为 0 可关闭。
# 为什么要这个：控制端往往一直开着，而客户机是后来才开机的。只靠控制端
# 主动扫描的话，客户机得等到下一次扫描才出现；主动报到能让它开机几秒内
# 就出现在设备列表里。报文很小（约 200 字节）。
HELLO_INTERVAL = 60.0

# 壁纸样式 -> (注册表 WallpaperStyle, 注册表 TileWallpaper)
# 这几个值就是 Windows「个性化 -> 背景 -> 选择契合度」的底层取值
STYLES = {
    "填充": ("10", "0"),
    "适应": ("6", "0"),
    "拉伸": ("2", "0"),
    "平铺": ("0", "1"),
    "居中": ("0", "0"),
    "跨区": ("22", "0"),
}
DEFAULT_STYLE = "填充"
STYLE_NAMES = list(STYLES.keys())

# 命令行/批处理里不方便输入中文（.bat 存成 UTF-8 会在 GBK 控制台上变乱码），
# 所以再给每个样式配几个 ASCII 别名，以及直接填注册表数值也行。
STYLE_ALIASES = {
    "fill": "填充",
    "fit": "适应",
    "stretch": "拉伸",
    "tile": "平铺",
    "center": "居中",
    "centre": "居中",
    "span": "跨区",
    "10": "填充",
    "6": "适应",
    "2": "拉伸",
    "0": "居中",
    "22": "跨区",
}


def normalize_style(name) -> str | None:
    """把用户输入变成标准样式名；无法识别时返回 None。

    接受中文名（填充）、英文别名（fill）和注册表数值（10）。
    """
    if name is None:
        return DEFAULT_STYLE
    text = str(name).strip()
    if not text:
        return DEFAULT_STYLE
    if text in STYLES:
        return text
    return STYLE_ALIASES.get(text.lower())


def style_help() -> str:
    """生成一行可读的样式说明，用于命令行报错。"""
    pairs = []
    for alias, cn in STYLE_ALIASES.items():
        if not alias.isdigit() and len(alias) < 8:
            pairs.append(f"{alias}={cn}")
    return "、".join(STYLE_NAMES) + "（英文别名：" + " ".join(pairs) + "）"


def normalize_app_name(raw) -> tuple[bool, str]:
    """校验「通知应用名」：返回 (是否合法, 清洗后的名字)。

    被控端**必须**独立做这层校验（控制端可能是别人伪造的）。规则：
      * 只允许单行可见文本，去掉首尾空白、把连续空白压成一个空格；
      * 不允许换行/制表/控制字符（那会破坏通知标题栏的排版）；
      * 最长 MAX_APP_NAME 个字符；空字符串是合法值（= 恢复默认名字）。
    """
    text = str(raw or "")
    # 先按"任何空白"切开再拼回去：换行、制表符都会被处理掉
    parts = [p for p in text.split() if p]
    clean = " ".join(parts)
    if len(clean) > MAX_APP_NAME:
        return False, f"名字太长（最多 {MAX_APP_NAME} 个字符）"
    for ch in clean:
        if ord(ch) < 32 or ord(ch) == 127:
            return False, "名字里不能有控制字符"
    return True, clean


# ---------------------------------------------------------------- 编解码

def dumps(obj: dict) -> bytes:
    """把消息编码成 UTF-8 JSON 字节流（紧凑格式，省带宽）。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def loads(raw: bytes | None):
    """解析消息；不是本协议的消息一律返回 None（容错，不抛异常）。"""
    if not raw:
        return None
    try:
        obj = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("magic") != MAGIC:
        return None
    return obj


def make(type_: str, **fields) -> dict:
    """构造一条带 magic 的消息。"""
    msg = {"magic": MAGIC, "type": type_}
    msg.update(fields)
    return msg
