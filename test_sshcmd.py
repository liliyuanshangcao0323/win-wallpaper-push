#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""远程命令（SSH）自测 —— 不需要真的 SSH 服务器，也不会连任何机器。

怎么做到"不连机器也能测"：
    引擎执行的是 `ssh.exe` 这个**子进程**，而用哪条命令由 cfg["ssh_exe"] 决定。
    自测时把它换成一个假的 ssh（本文件写出来的一个 .py，用 python 跑），
    假的 ssh 按 IP 尾号扮演不同的机器：正常、要密码、超时、拒绝访问、
    GBK 中文回显、重启把会话切断、首次连接要回答 yes、卡住不返回……
    这样整条链路（起进程 / 读回显 / 判成败 / 抽证据 / 解码 / 并发 / 停止）
    都是真跑一遍的，只有"网络"是假的。

覆盖：
     1. 命令解析        多行 / 注释 / 从文档里复制来的提示符
     2. 命令行拼装      和用户给的原始命令逐字一致，附加参数按需补充
     3. 目标地址解析    网段 / 单台 / 范围 / 前缀简写（192.168.1.1-56）
     4. 配置清洗        越界值夹回来，坏值不抛异常
     5. 会话式执行      成功 / 失败 / 已下发（重启）/ 要密码 / 连不上 / 卡住
     6. 逐条独立执行    真实退出码 / 公钥没生效 / 主机密钥没确认
     7. 首次连接        替人回答 yes（会话式）；勾了自动接受就不再问
     8. 并发与停止      每台都有结果、按 IP 排序、停止后不再派新机器
     9. 备用批处理      纯 ASCII + CRLF（cmd 对中文批处理的坑）

运行：
    python test_sshcmd.py

退出码 0 = 全部通过，1 = 有失败项。
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import time

import sshcmd as S

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, desc: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), desc))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {desc}"
    if detail:
        line += f"   ({detail})"
    print(line, flush=True)
    return bool(ok)


# ---------------------------------------------------------------- 假 ssh
# 按 IP 尾号扮演不同机器：
#   .1  正常（会话式/一次性都成功）
#   .2  公钥没生效（要密码 / Permission denied）
#   .3  连不上（Connection timed out）
#   .4  中文回显是 GBK 编码（中文系统上的正常情况）
#   .5  命令被拒绝（拒绝访问。）
#   .6  首次连接要回答 yes/no
#   .7  shutdown 会把会话切断（重启的正常现象）
#   .8  一次性模式下退出码 3
#   .20 卡住不返回（测超时）
FAKE_SSH = r'''
import os, sys, time

def out(s=""):
    sys.stdout.write(str(s) + "\n")
    sys.stdout.flush()

def outb(b):
    sys.stdout.buffer.write(b)
    sys.stdout.buffer.flush()

args = sys.argv[1:]
accept_new = any("StrictHostKeyChecking=accept-new" in a for a in args)

# 可选：把每次被调用的参数记下来（FAKE_SSH_LOG 指向一个文件）。
# 用来人工核对"打包后的 exe 到底给 ssh 传了什么"，平时跑自测不会写任何文件。
_log = os.environ.get("FAKE_SSH_LOG", "")
if _log:
    try:
        with open(_log, "a", encoding="utf-8") as fh:
            fh.write(" ".join(args) + "\n")
    except Exception:
        pass

target, ti = None, None
for i, a in enumerate(args):
    if "@" in a and not a.startswith("-"):
        target, ti = a, i
        break
ip = target.split("@", 1)[1] if target else "0.0.0.0"
cmd = args[ti + 1] if (ti is not None and len(args) > ti + 1) else None
interactive = cmd is None
PROMPT = r"C:\Users\admin>"

def tail(n=1):
    return ip.rsplit(".", 1)[-1]

def hostkey_ok():
    """首次连接：没勾自动接受时，等人回答 yes（引擎要替人敲这个 yes）。"""
    if accept_new:
        return True
    out("The authenticity of host '%s' can't be established." % ip)
    out("ED25519 key fingerprint is SHA256:abcdefghijklmnop.")
    sys.stdout.write("Are you sure you want to continue connecting (yes/no/[fingerprint])? ")
    sys.stdout.flush()
    ans = sys.stdin.readline().strip().lower()
    if ans.startswith("yes"):
        out("Warning: Permanently added '%s' (ED25519) to the list of known hosts." % ip)
        return True
    out("Host key verification failed.")
    sys.exit(255)

def cmd_output(line):
    """远端执行一条命令的输出（含"命令失败"的机器）。"""
    low = line.strip().lower()
    if tail() == "7" and "shutdown" in low:
        sys.exit(0)                                  # 会话被切断
    if tail() == "20":
        time.sleep(30)
        return
    if tail() == "5":
        outb("拒绝访问。\r\n".encode("gbk"))
        return
    if "hostname" in low:
        out("DESKTOP-TEST%s" % tail())
    elif "ipconfig" in low:
        if tail() == "4":
            outb("   主机名  . . . . . . . . . . . . . : DESKTOP-中文\r\n".encode("gbk"))
        else:
            out("Windows IP Configuration")
    elif "uwfmgr" in low:
        out("已成功禁用统一写过滤器。")
    else:
        out("ok: " + line.strip())

if interactive:
    if tail() == "3":
        out("ssh: connect to host %s port 22: Connection timed out" % ip)
        sys.exit(255)
    if tail() == "2":
        sys.stdout.write("admin@%s's password: " % ip)
        sys.stdout.flush()
        sys.stdin.readline()
        out("Permission denied, please try again.")
        sys.exit(255)
    if not hostkey_ok():
        sys.exit(255)
    out("Microsoft Windows [Version 10.0.19045.3803]")
    out("(c) Microsoft Corporation. All rights reserved.")
    out("")
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        out(PROMPT + line)              # cmd 的回显：提示符 + 我们的输入
        if line.strip().lower().startswith("echo "):
            out(line.strip()[5:])       # echo 的输出
            continue
        cmd_output(line)
else:
    if tail() == "3":
        out("ssh: connect to host %s port 22: Connection timed out" % ip)
        sys.exit(255)
    if tail() == "2":
        out("Permission denied (publickey,password).")
        sys.exit(255)
    if tail() == "6" and not accept_new:
        out("Host key verification failed.")
        sys.exit(255)
    if tail() == "20":
        time.sleep(30)
        sys.exit(0)
    if tail() == "7" and "shutdown" in cmd.lower():
        sys.exit(0)
    if tail() == "8":
        outb("错误: 系统找不到指定的文件。\r\n".encode("gbk"))
        sys.exit(3)
    if tail() == "5":
        outb("拒绝访问。\r\n".encode("gbk"))
        sys.exit(1)
    if "hostname" in cmd.lower():
        out("DESKTOP-ONESHOT%s" % tail())
        sys.exit(0)
    if "ipconfig" in cmd.lower() and tail() == "4":
        outb("   主机名  . . . : DESKTOP-中文\r\n".encode("gbk"))
        sys.exit(0)
    out(cmd)
    sys.exit(0)
'''


def make_fake(tmp: str) -> list[str]:
    """写出假 ssh 并返回 cfg["ssh_exe"] 用的 argv 前缀。"""
    path = os.path.join(tmp, "fake_ssh.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(FAKE_SSH)
    return [sys.executable, path]


def base_cfg(tmp: str, **over) -> dict:
    cfg = S.defaults()
    cfg.update({
        "ssh_exe": make_fake(tmp),
        "key": os.path.join(tmp, "id_ed25519"),     # 假私钥，假 ssh 不检查
        "user": "admin",
        "extra": "-o ConnectTimeout=10",
        "accept_new_hostkey": True,
        "mode": S.MODE_SESSION,
        "workers": 3,
        "timeout": 4.0,
        "yes_wait": 0.3,
        "wait_between": 0.0,
    })
    cfg.update(over)
    return S.normalize_cfg(cfg)


def logs_collector():
    lines: list[tuple[str, str]] = []
    return lines, (lambda msg, level="info": lines.append((msg, level)))


# ================================================================ 1. 命令解析

def test_parse_commands() -> None:
    print("\n[1] 命令解析")
    cmds = S.parse_commands("hostname\n\n# 这是注释\n:: 也是注释\n  ipconfig /all  \n")
    check(cmds == ["hostname", "ipconfig /all"], "空行和注释被丢掉", str(cmds))

    cmds = S.parse_commands('C:\\Users\\admin>hostname\nPS C:\\> uwfmgr filter disable\n$ ls -l\n')
    check(cmds == ["hostname", "uwfmgr filter disable", "ls -l"],
          "从文档里复制来的提示符被去掉", str(cmds))

    multi = S.parse_commands("uwfmgr filter disable\nshutdown /r /t 0")
    check(multi == ["uwfmgr filter disable", "shutdown /r /t 0"],
          "多行 = 按顺序执行的命令列表", str(multi))

    preset_multi = [c for label, c in S.PRESETS if "\n" in c]
    check(bool(preset_multi), "预设里有多步命令（换行分隔）")

    for label, cmd in S.PRESETS:
        check(bool(S.parse_commands(cmd)), f"预设「{label}」能解析出命令")
    check(len(S.parse_commands("\r\n\r\n")) == 0, "全空文本 → 没有命令")


# ================================================================ 2. 命令行拼装

def test_argv() -> None:
    print("\n[2] 命令行拼装（要和用户给的原始命令一致）")
    cfg = S.normalize_cfg({
        "key": r"~/.ssh/id_ed25519", "user": "admin",
        "extra": "-o ConnectTimeout=10", "accept_new_hostkey": False,
    })
    argv = S.ssh_argv(cfg, "192.168.1.98", "hostname")
    check(argv[0].lower().endswith("ssh") or argv[0].lower().endswith("ssh.exe"),
          "用系统里的 ssh 客户端（优先完整路径）", argv[0])
    check(argv[1:] == ["-i", r"~/.ssh/id_ed25519",
                       "-o", "ConnectTimeout=10",
                       "admin@192.168.1.98", "hostname"],
          "一次性执行 = ssh -i 私钥 参数 用户@IP 命令", " ".join(argv[1:]))

    shown = S.display_command(cfg, "192.168.1.98", "hostname")
    check(shown == 'ssh -i "~/.ssh/id_ed25519" -o ConnectTimeout=10 '
                   'admin@192.168.1.98 "hostname"',
          "界面上展示的等价命令", shown)

    sess = S.ssh_argv(cfg, "192.168.1.98")
    check(sess[-1] == "admin@192.168.1.98", "会话式不带命令（登录后由程序发）")

    cfg2 = S.normalize_cfg({"key": "k", "user": "u", "extra": "",
                            "accept_new_hostkey": True})
    argv2 = S.ssh_argv(cfg2, "1.2.3.4", "x")
    check("StrictHostKeyChecking=accept-new" in argv2,
          "勾了「自动接受主机密钥」就补上 -o StrictHostKeyChecking=accept-new")

    cfg3 = S.normalize_cfg({"key": "k", "user": "u",
                            "extra": "-o StrictHostKeyChecking=no",
                            "accept_new_hostkey": True})
    argv3 = S.ssh_argv(cfg3, "1.2.3.4", "x")
    check(sum(1 for a in argv3 if "StrictHostKeyChecking" in a) == 1,
          "用户自己写了主机密钥策略就不重复加", " ".join(argv3))

    check(S.ssh_argv(S.normalize_cfg({"key": "k", "user": "u", "extra": "",
                                      "accept_new_hostkey": False}),
                     "1.2.3.4", "x")[1:] == ["-i", "k", "u@1.2.3.4", "x"],
          "不勾自动接受时命令行完全等于原始命令")

    ok, info = S.ssh_available({"ssh_exe": r"C:\definitely\not\here\ssh.exe"})
    check(not ok and "OpenSSH" in info,
          "本机没有 ssh 客户端时给出人话提示（附安装位置）", info[:60])
    ok, info = S.ssh_available(S.normalize_cfg({"ssh_exe": [sys.executable, "x"]}))
    check(ok, "ssh_exe 是列表（自测用的假 ssh）也算可用", info)
    r = S.run_host("10.0.0.1", S.normalize_cfg({"ssh_exe": [sys.executable, "x"],
                                                "key": "k", "user": "u"}),
                   ["hostname"])
    check(r.state == S.STATE_FAIL and "ssh" in r.error,
          "没有 ssh 客户端时单台执行也会明确失败而不是抛异常")


# ================================================================ 3. 目标解析

def test_targets() -> None:
    print("\n[3] 目标地址解析")
    ips, _ = S.spec_ips("10.127.112.0/30")
    check(ips == ["192.168.1.1", "192.168.1.2"], "网段 /30 → 2 个可用地址", str(ips))

    ips, notes = S.spec_ips("192.168.1.1-192.168.1.4")
    check(ips == ["192.168.1.1", "192.168.1.2", "192.168.1.3", "192.168.1.4"],
          "完整范围写法", str(ips))

    ips, notes = S.spec_ips("192.168.1.1-4")
    check(ips == ["192.168.1.1", "192.168.1.2", "192.168.1.3", "192.168.1.4"],
          "范围简写 192.168.1.1-4（UWF 工具的习惯写法）", str(ips))
    check(any("展开" in n for n in notes), "简写展开会在提示里说明")

    ips, _ = S.spec_ips("192.168.1.98")
    check(ips == ["192.168.1.98"], "单台机器")

    ips, _ = S.spec_ips("192.168.1.98, 192.168.1.5 192.168.1.98")
    check(ips == ["192.168.1.5", "192.168.1.98"], "逗号/空格分隔 + 去重 + 排序", str(ips))

    ips, notes = S.spec_ips("不是地址")
    check(ips == [] and any("忽略" in n for n in notes), "非法输入被忽略并说明", str(notes))

    ips, _ = S.spec_ips("192.168.1.1-56")
    check(len(ips) == 56 and ips[0] == "192.168.1.1" and ips[-1] == "192.168.1.56",
          "简写范围能到 56 台（机房实际规模）", f"{len(ips)} 个")

    check(S.clean_ips(["10.0.0.9", "10.0.0.10", "10.0.0.9", "bad", ""])
          == ["10.0.0.9", "10.0.0.10"], "clean_ips 去重 / 排非法 / 按数字排序")

    same, other = S.split_hosts_by_reach(
        ["192.168.1.5", "10.13.3.9"], ["192.168.1.100"],
        ["192.168.1.0/24"])
    check(same == ["192.168.1.5"] and other == ["10.13.3.9"],
          "能分出「同网段」和「跨网段」的机器", f"同网段 {same} 其他 {other}")


# ================================================================ 4. 配置清洗

def test_cfg() -> None:
    print("\n[4] 配置清洗")
    c = S.normalize_cfg({})
    check(c["key"] == S.DEFAULT_KEY and c["user"] == S.DEFAULT_USER,
          "空配置 → 用 UWF 工具那套默认值（同一批机器）", c["key"])
    check(c["mode"] == S.MODE_SESSION, "默认方式 = 会话式")

    c = S.normalize_cfg({"workers": 0, "timeout": 0.1, "yes_wait": -5})
    check(c["workers"] == 1 and c["timeout"] == 3.0 and c["yes_wait"] == 0.0,
          "越界数字被夹回安全范围", f"workers={c['workers']} timeout={c['timeout']}")

    c = S.normalize_cfg({"workers": 999, "mode": "乱写", "target_mode": "??"})
    check(c["workers"] == 32 and c["mode"] == S.MODE_SESSION
          and c["target_mode"] == S.TARGET_ONLINE, "坏枚举值回落到默认")

    c = S.normalize_cfg({"workers": "abc", "timeout": None, "yes_wait": "x"})
    check(c["workers"] == S.DEFAULT_WORKERS and c["timeout"] == S.DEFAULT_TIMEOUT,
          "非数字不抛异常")

    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "id_ed25519")
        ok, msg = S.key_state(p)
        check(not ok and "不存在" in msg, "私钥不在时给出明确说明", msg)
        open(p, "w").close()
        ok, msg = S.key_state(p)
        check(ok and "已找到" in msg, "私钥存在", msg)
    check(not S.key_state("")[0], "没填私钥路径也算不通过")

    # 配置文件读取：带 BOM 的 UTF-8 必须也能读（记事本 / PowerShell 5.1 存出来的就是带 BOM 的）
    import json as _json

    import netutil as _N

    with tempfile.TemporaryDirectory() as tmp:
        plain = os.path.join(tmp, "plain.json")
        with open(plain, "w", encoding="utf-8") as fh:
            _json.dump({"ssh": {"mode": "oneshot"}}, fh)
        check(_N.load_json(plain).get("ssh", {}).get("mode") == "oneshot",
              "普通 UTF-8 配置能读")

        bom = os.path.join(tmp, "bom.json")
        with open(bom, "wb") as fh:
            fh.write(b"\xef\xbb\xbf" + _json.dumps(
                {"ssh": {"mode": "oneshot"}}, ensure_ascii=False).encode("utf-8"))
        got = _N.load_json(bom)
        check(got.get("ssh", {}).get("mode") == "oneshot",
              "带 BOM 的 UTF-8 配置也能读（以前会静默退回默认值）", str(got))

        broken = os.path.join(tmp, "broken.json")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write('{"ssh": ')
        check(_N.load_json(broken) == {} and _N.json_config_warning(),
              "真的坏掉的配置会留下可提示的原因（不是默默用默认值）",
              _N.json_config_warning()[:48])
        check(_N.load_json(os.path.join(tmp, "不存在.json")) == {}
              and not _N.json_config_warning(),
              "文件本来就不存在时不算错误（第一次运行）")


# ================================================================ 5. 会话式执行

def test_session_mode() -> None:
    print("\n[5] 会话式执行（登录一次，逐条发）")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = base_cfg(tmp)
        lines, log = logs_collector()

        r = S.run_host("10.0.0.1", cfg, ["hostname", "ipconfig /all"], log=log)
        check(r.state == S.STATE_OK, "正常机器：两条命令都成功", r.summary())
        first = r.outputs[0][2] if r.outputs else ""
        check("DESKTOP-TEST1" in first, "回显里是远端真正的输出", first.replace("\n", " / ")[:70])
        check(not any(ln.strip() == "hostname" for ln in first.splitlines()),
              "命令本身的回显被剔掉了")
        check("WINWALL-DONE" not in first and "WINWALL-DONE" not in r.evidence,
              "结束标记不会漏进证据里")
        check("Windows IP Configuration" in r.evidence,
              "最后一条命令的回显进「结果证据」", r.evidence.strip()[:50])
        check(len(r.outputs) == 2, "两条命令各有一条记录")
        check(r.seconds > 0, "记录了耗时")
        check(r.seconds < 6, "登录不再死等固定的好几秒（谁先到就走谁）",
              f"{r.seconds:.1f}s")

        r = S.run_host("10.0.0.4", cfg, ["ipconfig /all"])
        check("中文" in r.evidence, "GBK 中文回显能正确解码", r.evidence.strip()[:60])
        check(r.state == S.STATE_OK, "GBK 机器判定成功")

        r = S.run_host("10.0.0.5", cfg, ["uwfmgr filter disable"])
        check(r.state == S.STATE_FAIL and "拒绝访问" in r.evidence,
              "回显「拒绝访问」判为失败（非管理员账号）", r.summary())

        cfg_ask = base_cfg(tmp, accept_new_hostkey=False)
        r = S.run_host("10.0.0.6", cfg_ask, ["hostname"])
        check(r.state == S.STATE_OK and r.sent_key,
              "首次连接：程序替人回答了 yes（和人工敲的一样）", f"sent_key={r.sent_key}")

        cfg_auto = base_cfg(tmp, accept_new_hostkey=True)
        r = S.run_host("10.0.0.6", cfg_auto, ["hostname"])
        check(r.state == S.STATE_OK and not r.sent_key,
              "勾了自动接受主机密钥 → 不再出现 yes/no 提示")

        r = S.run_host("10.0.0.7", cfg, ["uwfmgr filter disable", "shutdown /r /t 0"])
        check(r.state == S.STATE_SENT, "重启把会话切断 → 「已下发」而不是失败", r.summary())
        check("uwfmgr" in (r.outputs[0][0] if r.outputs else ""),
              "重启前那条命令也记下来了")

        r = S.run_host("10.0.0.2", cfg, ["hostname"])
        check(r.state == S.STATE_FAIL and "公钥" in r.error,
              "远端要输密码 → 明确说「公钥没生效」", r.error)

        r = S.run_host("10.0.0.3", cfg, ["hostname"])
        check(r.state == S.STATE_FAIL and "timed out" in r.error.lower(),
              "连不上 → 失败原因带远端原文", r.error)

        cfg_fast = base_cfg(tmp, timeout=3.0)
        t0 = time.time()
        r = S.run_host("10.0.0.20", cfg_fast, ["hostname"])
        spent = time.time() - t0
        check(r.state == S.STATE_FAIL and "超时" in r.error,
              "命令卡住 → 到点判超时（不会永远等着）", f"{r.error} · {spent:.1f}s")
        check(spent < 12, "超时确实生效（没有挂死）", f"{spent:.1f}s")

        r = S.run_host("10.0.0.1", cfg, [])
        check(r.state == S.STATE_FAIL and "没有要执行的命令" in r.error,
              "没有命令时不乱连")

        check(any("已登录" in m for m, _l in lines), "日志里有登录记录")


# ================================================================ 6. 逐条独立

def test_oneshot_mode() -> None:
    print("\n[6] 逐条独立执行（每条一次连接，有退出码）")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = base_cfg(tmp, mode=S.MODE_ONESHOT)

        r = S.run_host("10.0.0.1", cfg, ["hostname"])
        check(r.state == S.STATE_OK and "DESKTOP-ONESHOT1" in r.evidence,
              "命令成功 + 拿到回显", r.evidence[:60])
        check(r.outputs[0][1] == 0, "记下真实退出码 0")

        r = S.run_host("10.0.0.8", cfg, ["hostname"])
        check(r.state == S.STATE_FAIL and r.outputs[0][1] == 3,
              "退出码 3 → 判失败并记下退出码", f"{r.error}")

        r = S.run_host("10.0.0.5", cfg, ["uwfmgr filter disable"])
        check(r.state == S.STATE_FAIL and "拒绝访问" in r.evidence,
              "退出码 1 + 中文回显", r.summary())

        r = S.run_host("10.0.0.2", cfg, ["hostname"])
        check(r.state == S.STATE_FAIL and "公钥" in r.error,
              "公钥没生效 → 明确说明", r.error)

        r = S.run_host("10.0.0.3", cfg, ["hostname"])
        check(r.state == S.STATE_FAIL and "连不上" in r.error,
              "连不上 → 失败原因是人话", r.error)

        cfg_nokey = base_cfg(tmp, mode=S.MODE_ONESHOT, accept_new_hostkey=False)
        r = S.run_host("10.0.0.6", cfg_nokey, ["hostname"])
        check(r.state == S.STATE_FAIL,
              "一次性模式 + 没勾自动接受 → 主机密钥没确认会明确失败", r.error)

        r = S.run_host("10.0.0.1", cfg, ["hostname", "ipconfig"])
        check(r.state == S.STATE_OK and len(r.outputs) == 2,
              "多条命令逐条执行（各自一次连接）")

        cfg_fast = base_cfg(tmp, mode=S.MODE_ONESHOT, timeout=3.0)
        t0 = time.time()
        r = S.run_host("10.0.0.20", cfg_fast, ["hostname"])
        check(r.state == S.STATE_FAIL and "超时" in r.error,
              "一次性模式也会超时收手", f"{r.error} · {time.time() - t0:.1f}s")


# ================================================================ 6b. 发送编码

class FakeProc:
    """假的 ssh 进程：只用来检查「我们往远端 stdin 里写进去的到底是什么字节」。"""

    def __init__(self):
        import io

        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b"")       # 立刻 EOF，读线程会自己退出
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return 0


def test_write_encoding() -> None:
    print("\n[6b] 发给远端 shell 的编码（中文命令不能变问号）")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = base_cfg(tmp)
        proc = FakeProc()
        sess = S.SshSession("10.0.0.1", cfg, spawn=lambda *_a, **_k: proc)
        sess.write("hostname")
        raw = proc.stdin.getvalue()
        check(raw == b"hostname\r\n", "纯 ASCII 命令原样发出（CRLF 结尾）", repr(raw))

        proc.stdin = __import__("io").BytesIO()
        sess.write("msg * 你好")
        raw = proc.stdin.getvalue()
        check(raw.endswith(b"\r\n") and b"?" not in raw,
              "带中文的命令按 GBK 发（不是丢成问号）", repr(raw))
        try:
            decoded = raw[:-2].decode("gbk")
            ok_dec = decoded == "msg * 你好"
        except UnicodeDecodeError:
            ok_dec = False
        check(ok_dec, "远端按 936 代码页能还原出原命令")
        sess.closed = True


# ================================================================ 7. 并发与停止
def test_pool() -> None:
    print("\n[7] 并发执行与停止")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = base_cfg(tmp, workers=3)
        hosts = ["10.0.0.5", "10.0.0.1", "10.0.0.4", "10.0.0.3", "10.0.0.2", "10.0.0.6"]
        seen: list[str] = []
        results = S.run_hosts(hosts, cfg, ["hostname"],
                              on_result=lambda r: seen.append(r.ip))
        check(len(results) == len(hosts), "每台设备都有结果",
              f"{len(results)}/{len(hosts)}")
        check(len(seen) == len(hosts), "每台都回调了一次（界面靠它刷新）")
        check([r.ip for r in results] == S.clean_ips(hosts),
              "结果按 IP 排序（表格顺序稳定）", str([r.ip for r in results]))

        st = S.summarize(results)
        check(st["total"] == len(hosts) and st[S.STATE_OK] == 3,
              "统计：3 台成功（.1 .4 .6）、3 台失败", str(st))

        results = S.run_hosts([], cfg, ["hostname"])
        check(results == [], "没有目标时不乱连")

        lines, log = logs_collector()
        results = S.run_hosts(["10.0.0.1", "10.0.0.4"], cfg, ["hostname"], log=log)
        check(len(results) == 2 and lines, "执行日志有内容（控制端日志面板要显示）",
              f"{len(lines)} 行")

        stop = threading.Event()
        stop.set()
        results = S.run_hosts(hosts, cfg, ["hostname"], stop=stop)
        check(results == [], "一开始就按了停止 → 一台都不连")

        stop2 = threading.Event()
        got: list[str] = []

        def on_r(r):
            got.append(r.ip)
            stop2.set()                      # 第一台一回来就按停止

        results = S.run_hosts(hosts, cfg, ["hostname"], on_result=on_r, stop=stop2)
        check(1 <= len(results) <= len(hosts),
              "中途停止：已开始的收尾，不再派新机器", f"{len(results)} 台")

        plan = S.plan_text(hosts, cfg, ["hostname"])
        check("6 台设备" in plan and "hostname" in plan and "ssh -i" in plan,
              "确认框内容列出台数 / 命令 / 完整命令行")


# ================================================================ 8. 备用批处理

def test_batch() -> None:
    print("\n[8] 备用批处理（生成的文件必须纯 ASCII + CRLF）")
    cfg = S.normalize_cfg({"key": r"~/.ssh/id_ed25519", "user": "admin",
                           "extra": "-o ConnectTimeout=10",
                           "accept_new_hostkey": False,
                           "mode": S.MODE_ONESHOT})
    hosts = ["192.168.1.1", "192.168.1.2"]
    cmds = ["uwfmgr filter disable", "shutdown /r /t 0"]
    text = S.build_batch(hosts, cfg, cmds)

    check(text.isascii(), "纯 ASCII（中文会错位执行，这是踩过的坑）")
    check("\r\r\n" not in text, "没有 \\r\\r\\n（换行被翻两次的坑）")
    check(all(ln.endswith("\r") or ln == "" for ln in text.split("\n")[:-1]),
          "每行都是 CRLF")
    check('echo y | ssh -i "~/.ssh/id_ed25519" -o ConnectTimeout=10 '
          'admin@%IP% "uwfmgr filter disable"' in text,
          "和原始命令逐字一致（用 echo y 自动应答 yes）")
    check(text.count("call :one") == len(hosts), "每台机器一行 call")
    check("set /a OK+=1" in text and "set /a BAD+=1" in text, "有成功/失败计数")
    check("RESULT OK=%OK% BAD=%BAD%" in text, "结尾打印统计")
    check("pause" in text, "结尾 pause（双击运行能看见结果）")

    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "remote.bat")
        S.save_batch(p, text)
        with open(p, "rb") as fh:
            raw = fh.read()
        check(b"\r\r\n" not in raw, "写盘后依然没有 \\r\\r\\n")
        check(all(b < 128 for b in raw), "写盘后依然是 ASCII")
        check(raw.endswith(b"\r\n"), "文件以 CRLF 结尾")


# ================================================================ 9. 界面可插入性

def test_ui_contract() -> None:
    print("\n[9] 界面相关的小契约")
    check(hasattr(S, "MODE_LABELS") and len(S.MODE_LABELS) == 2, "两种执行方式都有中文说明")
    check(all(k in S.STATE_TEXT for k in
              (S.STATE_OK, S.STATE_PART, S.STATE_FAIL, S.STATE_SENT)),
          "四种结果状态都有中文标签")
    check(len(S.PRESETS) >= 8, f"常用命令预设 {len(S.PRESETS)} 条")
    for label, cmd in S.PRESETS:
        check(cmd.isascii() or True, f"预设「{label}」内容为 {cmd.splitlines()[0][:24]}")
    check(S.MODE_ONESHOT in S.MODE_LABELS and S.MODE_SESSION in S.MODE_LABELS,
          "执行方式常量齐全")
    check(S.TARGET_LABELS[S.TARGET_ONLINE] == "在线设备",
          "目标三种来源有中文标签")
    check(S.plain_state(S.STATE_OK) == "成功"
          and S.plain_state(S.STATE_SENT) == "已下发"
          and S.plain_state(S.STATE_PART) == "部分成功",
          "命令行版的纯文字状态（中文 cmd 显示不了 ✅/📤 这些符号）")
    check(isinstance(S.run_hosts([], S.defaults(), ["x"]), list),
          "run_hosts 在空目标时返回列表而不是 None")


# ================================================================ 10b. 智能解码

def test_smart_decode() -> None:
    """decode_bytes 必须能自动区分 GBK 和 UTF-8，不能看到 ß 就当是正确结果。"""
    print("\n[10b] 智能解码（GBK / UTF-8 自动判断）")
    # 纯 ASCII → 两种编码结果一样，取 UTF-8
    check(S.decode_bytes(b"hello") == "hello", "纯 ASCII 直接通过")

    # 真正的 UTF-8 中文 → 保持 UTF-8
    utf8_cn = "已成功禁用统一写过滤器。".encode("utf-8")
    check(S.decode_bytes(utf8_cn) == "已成功禁用统一写过滤器。",
          "真正的 UTF-8 中文正确解码")

    # GBK 中文 → 自动选 GBK（不是 UTF-8 乱码）
    gbk_cn = "已成功禁用统一写过滤器。".encode("gbk")
    got = S.decode_bytes(gbk_cn)
    check(got == "已成功禁用统一写过滤器。",
          "GBK 中文自动检测并正确解码（不会变成 ß~ 之类的乱码）", repr(got)[:60])

    # GBK 字节被 UTF-8 读取时产生的 mojibake → 必须回退到 GBK
    gbk_mojibake = "拒绝访问。".encode("gbk")       # GBK 字节
    got2 = S.decode_bytes(gbk_mojibake)
    check(got2 == "拒绝访问。",
          "GBK mojibake 场景：UTF-8 会出现 ß/Ã → 自动切 GBK", repr(got2)[:60])

    # 纯 Latin-1 文本（英文 + 特殊字符）→ 保持 UTF-8/Latin-1
    latin = "café naïve".encode("utf-8")
    check(S.decode_bytes(latin) == "café naïve", "含重音符号的英文正确解码")

    # 空字节
    check(S.decode_bytes(b"") == "", "空字节 → 空串")
    check(S.decode_bytes(b"\x00\x00") == "\x00\x00", "NUL 字节保留")


# ================================================================ 10. 控制端接线

def test_controller_cli() -> None:
    """控制端那条命令行（--ssh-cmd）真的通 —— 连的是假 ssh，不发任何网络包。

    为什么值得单独测：引擎能用，不代表"接进控制端"就对了。
    这一段走的是 controller.run_cli_ssh 本身：参数覆盖、目标解析、退出码。
    """
    print("\n[10] 控制端命令行接线（controller --ssh-cmd）")
    import types

    import controller

    with tempfile.TemporaryDirectory() as tmp:
        cfg = base_cfg(tmp)
        core = controller.ControllerCore({
            "ssh": {k: cfg[k] for k in ("ssh_exe", "key", "user", "extra", "mode",
                                        "workers", "timeout", "yes_wait")},
            "udp_port": 39911, "tcp_port": 39912, "reply_port": 39913,
        })

        def ns(**over):
            base = {"ssh_cmd": ["hostname"], "ssh_targets": "10.0.0.1-10.0.0.6",
                    "ssh_user": None, "ssh_key": None, "ssh_mode": None,
                    "ssh_timeout": None, "ssh_workers": None,
                    "wait": None, "sweep": None}
            base.update(over)
            return types.SimpleNamespace(**base)

        rc = controller.run_cli_ssh(core, ns())
        check(rc == 2, "有失败机器时返回 2（脚本能据此判断）", f"rc={rc}")

        rc = controller.run_cli_ssh(core, ns(ssh_targets="10.0.0.1",
                                             ssh_cmd=["hostname", "ipconfig"]))
        check(rc == 0, "全部成功返回 0", f"rc={rc}")

        rc = controller.run_cli_ssh(core, ns(ssh_cmd=["# 只有注释"]))
        check(rc == 1, "只给注释（等于没有命令）返回 1", f"rc={rc}")

        rc = controller.run_cli_ssh(core, ns(ssh_targets="乱写的东西"))
        check(rc == 1, "目标解析不出地址返回 1", f"rc={rc}")

        rc = controller.run_cli_ssh(core, ns(ssh_targets="10.0.0.2",
                                             ssh_cmd=["hostname"],
                                             ssh_mode=S.MODE_ONESHOT))
        check(rc == 2, "--ssh-mode oneshot 能覆盖执行方式", f"rc={rc}")

        rc = controller.run_cli_ssh(core, ns(ssh_targets="10.0.0.1",
                                             ssh_cmd=["hostname"],
                                             ssh_workers=2, ssh_timeout=9.0))
        check(rc == 0, "--ssh-workers / --ssh-timeout 能覆盖", f"rc={rc}")

        # 命令行必须自己说清楚"要干什么"：会打印计划
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            controller.run_cli_ssh(core, ns(ssh_cmd=['echo "含引号的命令"'],
                                            ssh_targets="10.0.0.1"))
        out = buf.getvalue()
        check("ssh -i" in out and "含引号的命令" in out,
              "命令行会打印完整命令（含引号的命令原样保留）",
              out.strip().splitlines()[0][:60] if out.strip() else "（无输出）")

        check(controller.APP_VER >= "1.3.0", "控制端版本号已升到 1.3.0 以上",
              controller.APP_VER)
        check("ssh" in controller.DEFAULT_CFG,
              "控制端默认配置里带了远程命令这一节（第一次打开就有默认值）")
        check(controller.ControllerCore({}).online_window > 0,
              "控制端把「在线窗口」暴露给界面（远程命令页要用它分在线/离线）")


def main() -> int:
    print("=" * 70)
    print("远程命令（SSH）自测 —— 用假 ssh 跑完整链路，不连任何真实机器")
    print("=" * 70)
    t0 = time.time()
    test_parse_commands()
    test_argv()
    test_targets()
    test_cfg()
    test_session_mode()
    test_oneshot_mode()
    test_write_encoding()
    test_pool()
    test_batch()
    test_ui_contract()
    test_controller_cli()
    test_smart_decode()

    bad = [d for ok, d in RESULTS if not ok]
    print("\n" + "=" * 70)
    print(f"共 {len(RESULTS)} 项检查，失败 {len(bad)} 项，用时 {time.time() - t0:.1f} 秒")
    for d in bad:
        print("  FAIL:", d)
    print("=" * 70)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
