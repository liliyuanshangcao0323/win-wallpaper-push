<div align="center">

# 🖥️ Win 壁纸推送系统

**局域网桌面壁纸集中推送 + 远程命令（SSH）工具**

控制端选一张图广播出去，局域网内所有被控端自动换壁纸。
也可以通过 SSH 在每台设备上执行命令，远端回显作为证据带回。

[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078d4?logo=windows&logoColor=white)](https://www.microsoft.com/windows)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

</div>

---

## 📐 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         局 域 网                                 │
│                                                                 │
│  ┌──────────┐    ┌──────────────┐         ┌──────────────┐      │
│  │  管理员   │───▶│   控制端      │────────▶│  被控端 ×N    │      │
│  │          │    │ Controller   │  UDP/TCP │  Agent       │      │
│  └──────────┘    │              │◀────────│              │      │
│                  │ ┌──────────┐ │         │ ┌──────────┐ │      │
│                  │ │ 壁纸推送 │ │         │ │ 壁纸缓存 │ │      │
│                  │ │ 通知下发 │ │   SSH   │ │ 通知弹窗 │ │      │
│                  │ │ 远程命令 │─┼────────▶│ │ 换壁纸   │ │      │
│                  │ └──────────┘ │         │ └──────────┘ │      │
│                  └──────────────┘         └──────────────┘      │
│                                                                 │
│  端口: UDP 38571(广播) | TCP 38572(传输) | UDP 38573(回执)       │
└─────────────────────────────────────────────────────────────────┘
```

| 功能 | 流程 |
|------|------|
| **壁纸推送** | 控制端广播公告 → 被控端回连拉取图片 → SHA-256 校验 → 换壁纸 → 回报结果 |
| **通知下发** | 控制端下发通知规格 → 被控端用 BurntToast 弹窗 → 回报结果 |
| **远程命令** | 控制端通过 SSH 在每台设备执行命令 → 远端回显作为证据带回 |

---

## 🚀 快速开始

### 1. 安装依赖并打包

```powershell
pip install pyinstaller pillow
build.bat
```

产物在 `dist/` 目录：
- `WallpaperController.exe` — 控制端（约 19 MB）
- `WallpaperAgent.exe` — 被控端（约 18 MB）

### 2. 放行防火墙

```powershell
# 控制端 + 被控端都要执行（管理员运行）
add_firewall_rules.bat
```

### 3. 部署被控端

把 `WallpaperAgent.exe` 拷到每台客户机，双击或静默启动：

```powershell
# 静默后台运行（无窗口，开机自启）
WallpaperAgent.exe --silent --install
```

### 4. 启动控制端

双击 `WallpaperController.exe`，界面四个页签：

| 页签 | 功能 |
|------|------|
| **壁纸** | 选图 → 扫描设备 → 广播推送 |
| **通知** | 编辑通知 → 批量下发弹窗 |
| **远程命令** | 填命令 → 选目标 → SSH 执行 → 查看每台回显 |
| **设置** | 网段 / 扫描 / 通知应用名 |

---

## 🛠️ 功能详解

### 壁纸推送

1. 点「浏览…」选一张图片
2. 右侧「🔍 扫描」确认设备在线
3. 点「🚀 广播推送」

支持格式：JPG / PNG / BMP / WebP / GIF
契合度：填充（推荐）/ 适应 / 拉伸 / 平铺 / 居中 / 跨区

### 通知下发

通知页内置编辑器（不是弹窗），支持：

| 功能 | 说明 |
|------|------|
| 标题 + 正文 | 支持多行，含大括号的文字也能正确显示 |
| 声音 | 10 种系统音效可选 |
| 停留 | 短 / 长（~25秒）/ 一直显示到用户处理 |
| 紧急 | 勾选后通知优先级最高，不会被折叠 |
| 按钮 | 最多 5 个，只能打开网址（安全考虑） |
| 高级选项 | 第三行文字 / 图片 / 署名 / 分组 / 进度条 / 过期时间 |

### 远程命令（SSH）

在「远程命令」页填命令，一键发到所有设备执行：

```
目标：◉在线设备（48）  ○全部已发现  ○自定义网段/IP
命令：uwfmgr filter disable / shutdown /r /t 0 / hostname ...
结果：每台设备的远端回显作为证据，双击看完整输出
```

两种执行方式：

| 方式 | 特点 |
|------|------|
| **会话式**（默认） | 登录一次，逐条发命令，靠回显判断完成 |
| **逐条独立** | 每条命令单独连接，有真实退出码 |

常用命令预设：hostname / ipconfig / uwfmgr / shutdown / systeminfo 等 13 条。

被控端准备：装 OpenSSH 服务器 + 把控制端公钥放进 `authorized_keys` + 放行 TCP 22。

### 局域网发现

- 自动枚举所有网卡（有线 / 无线 / 虚拟）
- 每张网卡定向广播 + 逐台单播探测
- 设备按 IP 归并（同名克隆机不会合并成一行）
- 自动重扫（默认 30 秒）

---

## 📋 命令行用法

```powershell
# 扫描在线设备
WallpaperController.exe --scan --wait 5

# 推送壁纸
WallpaperController.exe --push "D:\壁纸\图.jpg"

# 远程命令
WallpaperController.exe --ssh-cmd "hostname"
WallpaperController.exe --ssh-cmd "uwfmgr filter disable" --ssh-targets 192.168.1.1-56

# 通知推送
WallpaperController.exe --push-toast toast.json

# 被控端状态
WallpaperAgent.exe --status
WallpaperAgent.exe --test-toast
```

---

## 📁 项目结构

```
├── controller.py          控制端主程序（GUI + CLI + 网络）
├── agent.py               被控端主程序（静默后台 + 面板）
├── sshcmd.py              SSH 远程命令引擎
├── sshui.py               控制端「远程命令」页面
├── toast.py               通知渲染（BurntToast）
├── toastui.py             通知编辑器页面
├── toastspec.py           通知规格与校验
├── protocol.py            通信协议定义
├── netutil.py             网络工具（扫描/配置/日志）
├── wallpaper.py           Win32 壁纸接口
├── ui.py                  深色主题与控件
├── agentauth.py           面板密码保护
├── winipc.py              控制通道（单实例/停止/叫出）
│
├── test_sshcmd.py         SSH 自测（140 项，假 ssh 端到端）
├── test_scan.py           网络发现自测（71 项）
├── test_loopback.py       端到端自测（30 项）
├── test_toast.py          通知自测（103 项）
├── test_agent_auth.py     口令自测（51 项）
├── test_agent_control.py  启停自测（61 项）
├── test_gui_smoke.py      界面冒烟测试
│
├── build.bat              打包两个 exe
├── install_agent.bat      一键安装
├── add_firewall_rules.bat 添加防火墙规则
│
├── screenshots/           界面截图
└── docs/                  架构图
```

---

## 🧪 运行测试

```powershell
python test_sshcmd.py          # SSH 远程命令（140 项，不连真实机器）
python test_scan.py            # 网络发现（71 项，不动壁纸）
python test_loopback.py        # 端到端（30 项，会临时换壁纸并还原）
python test_toast.py           # 通知（103 项，会弹 1-2 条通知）
python test_agent_auth.py      # 口令（51 项）
python test_agent_control.py   # 启停（61 项，真起进程后恢复）
python test_gui_smoke.py       # 界面（布局裁切检查）
```

---

## 🐛 故障排查

| 问题 | 解决方案 |
|------|---------|
| **壁纸没变** | 看被控端日志；远程桌面会话下壁纸设置可能不生效 |
| **扫不到设备** | 检查防火墙 UDP 38571 / TCP 38572；确保同一网段；「设置」页填额外网段 |
| **通知显示成「Windows PowerShell」** | 跑 `WallpaperAgent.exe --test-toast` 重新注册通知身份 |
| **远程命令全失败** | 检查：① 被控端装了 OpenSSH 服务器 ② 公钥在 authorized_keys 里 ③ 防火墙放行 TCP 22 |
| **远程命令乱码** | 程序自动检测 GBK/UTF-8 编码，通常无需处理 |
| **改了配置没生效** | 配置文件可能是 UTF-8 带 BOM → 已修复，更新 exe 即可 |
| **开机没自启** | 跑 `enable_autostart.bat` 或检查任务管理器→启动 |

---

## 📜 许可

[MIT License](LICENSE)
