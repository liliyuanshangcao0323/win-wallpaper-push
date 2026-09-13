#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 License.txt 转成 MSI 许可协议对话框能正确显示的 License.rtf。

为什么需要这一步：
    RTF 标准里的非 ASCII 字符要用 \\uNNNN? 转义表示。如果直接把 UTF-8 的
    中文塞进 .rtf，Windows Installer 的 RichEdit 控件会按 ANSI 代码页去读，
    在许可协议页面显示成一堆乱码。

用法：
    python make_license_rtf.py            # 读同目录 License.txt，写 License.rtf
"""

from __future__ import annotations

import os
import sys


def escape_rtf(text: str) -> str:
    """把一段纯文本转成 RTF 正文片段。"""
    out = []
    for ch in text:
        code = ord(ch)
        if ch == "\n":
            out.append("\\par\n")
        elif ch == "\t":
            out.append("\\tab ")
        elif ch in "\\{}":
            out.append("\\" + ch)
        elif code < 128:
            out.append(ch)
        elif code <= 0xFFFF:
            # \uc1 表示转义后跟 1 个替代字节，用 ? 占位
            out.append(f"\\u{code}?")
        else:
            # BMP 之外的字符拆成 UTF-16 代理对
            code -= 0x10000
            hi = 0xD800 + (code >> 10)
            lo = 0xDC00 + (code & 0x3FF)
            out.append(f"\\u{hi}?\\u{lo}?")
    return "".join(out)


def build_rtf(text: str, font: str = "Microsoft YaHei") -> str:
    body = escape_rtf(text.replace("\r\n", "\n").replace("\r", "\n"))
    return (
        r"{\rtf1\ansi\ansicpg936\deff0\nouicompat"
        r"{\fonttbl{\f0\fnil\fcharset134 " + font + r";}}"
        "\n"
        r"{\*\generator WinWallpaperPush license generator}\viewkind4\uc1" "\n"
        r"\pard\f0\fs18 " + body + "}\n"
    )


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, "License.txt")
    dst = os.path.join(here, "License.rtf")

    try:
        with open(src, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"读取失败：{src} ({e})", file=sys.stderr)
        return 1

    # RTF 文件本体是纯 ASCII（中文都转义了），所以用 ascii 写就够
    with open(dst, "w", encoding="ascii", errors="strict", newline="") as f:
        f.write(build_rtf(text))

    print(f"已生成 {dst}  ({os.path.getsize(dst)} 字节, 全部 ASCII 转义)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
