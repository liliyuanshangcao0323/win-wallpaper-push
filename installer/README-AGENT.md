# Win 壁纸推送 —— 被控端（已通过 MSI 安装）

这个文件由安装程序放到 `C:\Program Files\WinWallpaperPush\`，用来快速说明
本机上都被改动了什么、怎么排查问题。

## 本机改动清单

| 项目 | 位置 |
|---|---|
| 主程序 | `C:\Program Files\WinWallpaperPush\WallpaperAgent.exe` |
| 文字控制台 | `C:\Program Files\WinWallpaperPush\agent_console.bat` |
| 机器级配置 | `C:\ProgramData\WinWallpaperPush\agent_config.json` |
| 开机自启 | `HKLM\Software\Microsoft\Windows\CurrentVersion\Run` → `WinWallpaperPushAgent` |
| 防火墙规则 | 入站 UDP 38571（按端口放行，和免安装版的 add_firewall_rules.bat 一致） |
| 日志文件 | `%LOCALAPPDATA%\WinWallpaperPush\agent.log`（每用户；超过 512 KB 滚动成 .1） |
| 开始菜单 | `Win 壁纸推送` → 显示壁纸接收端窗口 / 壁纸接收端控制台 |
| 壁纸缓存 | `%LOCALAPPDATA%\WinWallpaperPush\wallpapers\`（每用户各一份） |

卸载 MSI 会自动移除前面几项（日志和壁纸缓存除外 —— 它们是每用户数据，
需要的话手动删掉 `%LOCALAPPDATA%\WinWallpaperPush\`）。

## 它跑在哪儿、怎么看见它、怎么关掉它

被控端是**静默后台**运行的：没有窗口、没有托盘图标。所以有三个入口：

| 想做的事 | 怎么做 |
|---|---|
| 看它到底在不在跑 | 开始菜单 → `壁纸接收端控制台` → 选 1；或命令行 `WallpaperAgent.exe --status` |
| 把界面叫出来（看状态、点退出） | 开始菜单 → `显示壁纸接收端窗口`；或 `WallpaperAgent.exe --show` |
| 让它停止运行 | 控制台选 3；或 `WallpaperAgent.exe --stop` |

几点说明：

* **同一台机器只会跑一个被控端。** 自启项、快捷方式、双击都拉不动第二个实例。
* **`--stop` 只停这一次运行**：自启项还在，下次登录它会重新起来。要永久停掉，
  用开始菜单里的控制台停掉之后，再去「任务管理器 → 启动」里禁用自启项，
  或者直接卸载 MSI。
* `--stop` 走的是「同会话」控制通道，所以要在**同一个用户会话**里执行。
  多用户同时登录时，其它会话里的实例需要管理员用
  `taskkill /IM WallpaperAgent.exe /F /T` 一起结束（`/T` 不能省：单文件打包的
  exe 有父子两个进程）。
* 任务管理器里看到两个 `WallpaperAgent.exe` 是正常的（父进程负责解包，
  子进程跑真正的程序）。
* 打开窗口（`--show`）后，信息卡片上有两行值得看：
  **「开机自启」**显示 `由 MSI 安装包管理（HKLM Run）` —— 机器级自启项归安装包管，
  想取消就卸载 MSI，别在窗口里点；
  **「防火墙」**显示入站 `UDP 38571` 有没有放行，未放行时点旁边的「放行…」
  会弹一次 UAC 把规则补上。

## 面板密码（第一次打开窗口时设置）

被控端的面板是受保护的，**第一次打开窗口**（开始菜单的「显示壁纸接收端窗口」）
会要求你先设置一个密码，之后：

| 动作 | 要不要密码 |
|---|---|
| 打开控制台看状态（`--status`） | 不用 |
| **打开面板窗口**（双击 / `--show` / 开始菜单） | **要** |
| **停止运行**（控制台选 3 / `--stop`） | **要** |
| **卸载、取消自启**（`--uninstall`、`uninstall_agent.bat`、卸载 MSI） | 要（MSI 的 `msiexec /x` 除外，那是管理员操作） |

```bat
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --set-password
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --set-password --clear
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --status
```

密码存的是加盐哈希，不存明文。管理员身份设置的话会写进
`C:\ProgramData\WinWallpaperPush\secure\agent_password.json` —— 那个目录 MSI 已经
设成「Users 只读」，普通用户改不掉也删不掉，所以拦得住人；普通用户自己设置的话，
`--status` 会显示「未锁」，防护弱一些。

> 说清楚边界：这道口令拦的是**同机普通用户随手打开面板 / 停止 / 卸载**。
> 本机管理员或任何本地用户仍然可以 `taskkill` 结束进程、直接删掉程序目录 ——
> 那要靠 Windows 权限 / ACL / 应用白名单来管。

## 通知功能（Toast）

本机除了换壁纸，还能接收控制端推来的 Windows 通知（标题/正文/图标/大图/按钮/
声音/进度条等）。渲染用的是 **BurntToast** 这个 PowerShell 模块：

```bat
:: 看通知功能是否就绪（会说明模块在哪）
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --status

:: 本机弹一条测试通知
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --test-toast

:: 装/修通知组件（首次安装会自动跑；内网离线时用 --source 指向本地模块目录）
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --install-toast-module
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --install-toast-module --source D:\BurntToast
```

通知组件（BurntToast）**已经打进 `WallpaperAgent.exe` 里面了**，正常情况下什么都不用做：
拷一个 exe 过去就能发通知，没外网也行。上面两条命令只在极端情况下才用得上。

真要手工补模块（比如想换版本）：在能上网的机器上跑一次 `prepare_toast_module.bat`，
或执行 `powershell -Command "Save-Module BurntToast -Path .\"`，把生成的 `BurntToast`
目录拷到 `C:\Program Files\WinWallpaperPush\` 下面即可（**放在 exe 旁边的模块优先于内置的那份**，
所以这样升级模块不需要重新打包；也可以在 `agent_config.json` 里用 `toast_module_path` 指定别的路径）。

配置里和通知有关的开关：

| 字段 | 说明 |
|---|---|
| `toast_enabled` | 收不收通知，默认 `true`；设 `false` 就只收壁纸 |
| `toast_module_path` | BurntToast 目录；留空按「exe 旁边 → 本机模块目录 → **exe 内置的那份** → 系统已装」找 |
| `toast_min_interval` | 两条通知之间至少隔几秒，默认 `1.0`（防刷屏） |
| `toast_auto_install` | 缺 BurntToast 时是否自动装（需要能上 PSGallery），默认 `true` |
| `toast_app_name` | 通知上显示的应用名（通知顶部 + 通知中心分组名）；留空 = 「Win 壁纸推送」。集中下发这个字段就能整批客户机换成「IT 运维通知」之类 |

**通知组件不用你装**：BurntToast 已经打进 `WallpaperAgent.exe` 里了，第一次发通知时
会自动释放一份到 `%LOCALAPPDATA%\WinWallpaperPush\modules\BurntToast`（约 1 MB）再用，
所以客户机不需要外网、不需要 PSGallery，也不会往系统里装任何东西。

> 顺带一个坑（已在代码里绕过，不用你管）：BurntToast 1.1.0 的 `New-BTText` 会把文字
> **包进大括号**，通知上就会显示成 `{标题}`。被控端生成的 `toast.ps1` 会把文字按原文
> 写回 XML，所以看到 `{}` 说明脚本是旧版 —— 换新 exe 重启被控端即可（脚本会自动重写）。

**没装模块会怎样**：控制端会看到失败原因（「本机没有 BurntToast 通知模块」+ 怎么装）。
被控端自己会在**启动 8 秒后**和**每次要弹通知前**再查一次并自动补装，所以多数情况下
你不用管；内网不通外网就把离线模块目录放到 `WallpaperAgent.exe` 旁边（见上面）。

安全说明：通知里**不能带可执行的脚本**（BurntToast 的 ActivatedAction /
DismissedAction 会被直接拒收），按钮也只能是 http/https 网址 —— 所以客户机上
不会被"点一下就跑命令"。

**通知以谁的名义弹出来**：被控端会注册自己的通知身份，通知显示成
「Win 壁纸推送」+ 我们自己的图标（不然会显示成「Windows PowerShell」，
既让人看不懂，又很像钓鱼弹窗）。注册的东西就三样，都是当前用户的，不需要管理员：

| 内容 | 位置 |
|---|---|
| AppUserModelID | `HKCU\Software\Classes\AppUserModelId\WinWallpaperPush.Agent` |
| 图标 | `%LOCALAPPDATA%\WinWallpaperPush\wpp.ico` |
| 开始菜单快捷方式（点开就是面板） | `%APPDATA%\...\Start Menu\Programs\Win 壁纸推送.lnk` |

想确认状态：`--status` 里会写「通知身份：已注册为「Win 壁纸推送」」；
显示成 PowerShell 的话，跑一次 `--test-toast` 即可重新注册。

**通知优先级（第一次运行时自动设为最高）**：被控端第一次运行会在这个键下写入
`AllowUrgentNotifications = 1`（DWORD）：

```
HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\WinWallpaperPush.Agent
```

它等于系统「设置 → 系统 → 通知 → Win 壁纸推送 → 允许紧急通知」那个开关。
打开后，控制端勾了「紧急」的通知会被系统当成重要通知：**能穿透专注助手（勿扰）**、
在通知中心里排前面。设置过之后会记一个标记（
`%LOCALAPPDATA%\WinWallpaperPush\notification_priority.json`），以后启动不再改动 ——
用户在系统设置里关掉就是关掉了，不会被程序偷偷改回来。要显式改：

```bat
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --allow-urgent on
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --allow-urgent off
```

（面板上也有「设为最高（允许紧急通知）」的勾选框。）

**改通知上显示的应用名**（默认「Win 壁纸推送」）：

```bat
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --set-toast-app-name "IT 运维通知"
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --set-toast-app-name default
```

改完对之后弹出的通知生效；开始菜单里那个带 AppId 的快捷方式会跟着改名，旧名字会清掉。
面板上「通知应用名」那一行的「改名…」按钮是同一件事。

> 想让**每条通知**带自己的落款（比如某次维护写成「网络组」），那是控制端发消息时填的
> 「署名」（`attribution`），跟着消息走，不需要在客户机上改任何东西。

**通知横幅停多久**由控制端在发消息时决定（短约 5 秒 / 长约 25 秒 / 一直显示到用户处理），
客户机不用配置。想在本机现场看效果：

```bat
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --test-toast --duration long
```

## 它是怎么启动的

被控端不是 Windows 服务，而是通过 `HKLM\...\Run` 在**每个用户登录时**
启动到该用户自己的会话里。

这不是偷懒 —— 桌面壁纸是「每用户」设置，而 Windows 服务运行在会话 0，
它只能改 SYSTEM 账户的壁纸，用户桌面上根本看不到变化。所以壁纸类工具
必须跑在用户的交互会话里。

副作用：安装完成后**不会立即启动**，要等下次登录。想马上让它在当前会话
跑起来，执行：

```bat
start "" "C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --silent
```

## 常用排查命令

```bat
:: 运行状态（在跑返回 0，没跑返回 1；顺带打印端口 / 配置 / 日志路径）
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --status

:: 环境自检（配置文件路径、防火墙/自启状态、本机网卡与广播目标网段）
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --selftest

:: 看进程在不在（单文件打包会有两个同名进程：解包器 + 真正的程序）
tasklist /FI "IMAGENAME eq WallpaperAgent.exe"

:: 看端口有没有监听（UDP 要加 -p UDP）
netstat -ano -p UDP | findstr 38571

:: 看防火墙规则（端口级规则，名字里带 UDP 38571 的都算）
netsh advfirewall firewall show rule name=all dir=in | findstr 38571

:: 看开机自启项
reg query "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" /v WinWallpaperPushAgent

:: 手动结束（/T 必须带，否则子进程还在跑）
taskkill /IM WallpaperAgent.exe /F /T
```

## 改配置

`C:\ProgramData\WinWallpaperPush\agent_config.json`：

```json
{
  "udp_port": 38571,
  "style": "填充",
  "keep": 10,
  "wallpaper_dir": "",
  "silent": true,
  "retry": 3,
  "hello_interval": 60,
  "log_file": "",
  "log_max_kb": 512
}
```

改完重启被控端生效（`--stop` 后重新启动，或下次登录）。

- `style` 可选：填充 / 适应 / 拉伸 / 平铺 / 居中 / 跨区
- `silent: true` 表示不带 `--silent` 启动时也静默（自启项本来就带 `--silent`；
  显式 `--show` 优先）
- `wallpaper_dir` 留空则用 `%LOCALAPPDATA%\WinWallpaperPush\wallpapers`
- `hello_interval` 是「主动报到」间隔（秒，默认 60）：本机启动后会向局域网
  广播一条很小的在线消息，控制端不用等扫描就能发现这台机器。设为 `0` 可关闭。
- `log_file` 留空用 `%LOCALAPPDATA%\WinWallpaperPush\agent.log`；填 `off` 关闭
- `log_max_kb` 日志上限（KB），超过就滚动成 `agent.log.1`

## 控制端找不到这台机器？

按顺序检查：

1. **它到底在不在跑**：`WallpaperAgent.exe --status`（没跑就先启动；
   MSI 装完要等下次登录才自启）。
2. **日志里怎么说**：`%LOCALAPPDATA%\WinWallpaperPush\agent.log`，静默后台时
   所有线索都在这儿（有没有收到广播、下载失败在哪一步）。
3. **本机网卡对不对**：`WallpaperAgent.exe --selftest`，看「本机网卡」那几行
   （有线、无线都会列出来），确认网段和广播地址。
4. **端口在监听**：`netstat -ano -p UDP | findstr 38571`。
5. **防火墙放行了**：`netsh advfirewall firewall show rule name=all dir=in | findstr 38571`。
   UDP 38571 既要能收到广播，也要能收到控制端的逐台单播探测。
6. **控制端那边的入站端口**：`UDP 38573`（主动报到发到控制端的这个端口）。

## 面板打不开 / 提示要重新运行设置密码？

（老版本踩过的坑，新版本已修，这里留个排查依据）

被控端平时是**隐藏**着跑的（静默后台、自启动）。老版本把密码输入框挂在隐藏窗口下
（`transient`），而 Windows 上这种对话框**根本不会显示**（实测 `viewable=False`、尺寸 1×1），
于是：用户看不到输入框 → 等 300 秒超时被判成"取消" → 弹出"还没有设置密码，面板不会打开，
请再运行一次本程序" → 重跑一次还是同一个看不见的框 → **永远设不上密码、面板永远打不开**，
但壁纸和通知一切正常（后台实例在跑）。

新版本做了两件事：

1. 密码框不再给隐藏窗口当子窗口，**一定可见**（并且有回归用例守着）；
2. **没设过密码时面板不再被锁死**：点取消也放行，同时明确提醒「现在任何本地用户都能
   打开面板/停止/卸载它」，随时能从面板上的「修改密码…」补上。

老版本上的救急办法（命令行不经过图形界面，任何版本都能用）：

```bat
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --set-password
```

如果连这个都不方便，删掉下面三处再重设：
`C:\ProgramData\WinWallpaperPush\secure\agent_password.json`、
配置里的 `password` 段、注册表 `HKCU\...\WinWallpaperPush\Agent\PasswordHash`。

## 卸载

```bat
msiexec /x {产品代码} /qn
```

产品代码可以用下面这条命令查到：

```bat
wmic product where "name like '%%壁纸推送%%'" get IdentifyingNumber
```

或者直接在「设置 → 应用」里卸载，也可以右击原来的 MSI 选「卸载」。
