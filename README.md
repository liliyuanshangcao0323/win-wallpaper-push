# Win 壁纸推送系统

局域网桌面壁纸集中推送工具。**控制端**选一张图广播出去，局域网内所有**被控端**自动把 Windows 桌面壁纸换成这张图。

```
┌──────────────────────┐                      ┌──────────────────────┐
│   控制端 Controller   │                      │   被控端 Agent (N台)  │
│                      │                      │                      │
│  ① 选图 + 算 sha256   │                      │                      │
│         │            │                      │                      │
│         ▼            │  ② UDP 广播公告       │                      │
│  ┌──────────────┐    │ ───────────────────▶ │  ┌────────────────┐  │
│  │ 广播 38571   │    │  (task/大小/校验值)   │  │ 监听 38571     │  │
│  └──────────────┘    │                      │  └───────┬────────┘  │
│                      │  ③ TCP 回连拉取图片   │          │           │
│  ┌──────────────┐    │ ◀─────────────────── │          ▼           │
│  │ 传输 38572   │    │ ───────────────────▶ │  ┌────────────────┐  │
│  └──────────────┘    │   完整文件 + 校验      │  │ 校验 + 落盘    │  │
│                      │                      │  └───────┬────────┘  │
│  ┌──────────────┐    │  ④ UDP 回报执行结果   │          ▼           │
│  │ 回执 38573   │    │ ◀─────────────────── │  SystemParameters  │
│  └──────────────┘    │  (成功/失败原因)      │  InfoW 换壁纸      │
└──────────────────────┘                      └──────────────────────┘
```

---

## 一、为什么是「广播公告 + TCP 拉取」

需求原本的描述是「壁纸通过广播发包发给各个机器」。如果真把几 MB 的 JPEG
切成 UDP 分片广播，会有一个硬伤：**UDP 没有重传**，广播出去的几十上百个分片
只要丢一个，接收端拿到的就是一张花屏的废图，而且发送端根本不知道。

所以这里采用数字标牌 / 远程桌面行业的标准做法，对外表现仍然是「广播推送」：

| 步骤 | 协议 | 内容 | 大小 |
|---|---|---|---|
| ① 公告 | UDP 广播 38571 | task_id、文件名、大小、sha256、TCP 端口 | 约 250 字节 |
| ② 拉取 | TCP 38572 | 完整图片二进制 | 实际大小 |
| ③ 回报 | UDP 单播 38573 | 成功 / 失败原因 | 约 150 字节 |

小包走广播（丢了也无所谓，控制端连发 3 遍），大文件走 TCP（自带重传 + 顺序保证），
再用 sha256 兜底校验。**结果：既保留了「广播推送」的使用体验，又能保证每一台
机器换上去的图都是完整正确的。**

### 为什么回执要单独占一个端口

Windows 允许多个 socket 用 `SO_REUSEADDR` 绑定同一个 UDP 端口 —— 这正是广播
包能被控制端和被控端**同时**收到的前提。但此时**单播**数据报只会投递给其中
一个 socket。如果回执也发往 38571，同一台电脑上同时跑控制端 + 被控端做测试时，
回执可能被投给被控端自己，控制端就永远等不到确认。用 38573 这个独占端口收
单播，结果就确定了。

### 设备发现：有线 / 无线网段都要覆盖

控制端点「🔍 扫描在线设备」时做了两件事，缺一不可：

| 方式 | 发到哪 | 解决什么问题 |
|---|---|---|
| ① 定向广播 | 每张网卡**各自网段**的广播地址（如 `192.168.1.255`、`10.13.255.255`），外加 `255.255.255.255` | 主要通道，一个包覆盖整个网段 |
| ② 逐台单播 | 网段内每个地址各发一个 ping（大网段退化为「本机 /24 + ARP 名单」） | 交换机 / 无线 AP 拦掉广播（客户端隔离）时，单播照样能通；被控端对单播 ping 一样会应答 |

几个容易踩的坑，这里都已经处理：

* **只发 `255.255.255.255` 不够。** 受限广播只会从「默认路由」那张网卡发出去。
  笔记本插了网线又连着 WiFi 时，默认路由通常是 WiFi，**有线网段上的机器一台
  都收不到**。所以控制端会枚举**全部网卡**（有线 / 无线 / 虚拟），逐个算出
  定向广播地址，并且**每张网卡各发一份** —— 发送套接字绑定在该网卡自己的
  地址上，强制从那张网卡出去。
* **前缀长度按网卡上报的真实值算。** 办公网常见 `/16`（掩码 `255.255.0.0`），
  按 `/24` 猜出来的 `10.13.3.255` 只能覆盖 254 个地址，绝大多数机器收不到。
  现在 `/16` 就算成 `10.13.255.255`。
* **被控端上线会主动报到。** 客户机常常是控制端开着之后才开机 / 登录的。
  被控端启动后会向控制端广播一条很小的「我在线」消息（默认每 60 秒一条，
  配置项 `hello_interval: 0` 可关闭），控制端不用等下一次扫描就能看到它。
* **自动重扫。** 控制端默认每 30 秒自动重扫一次（`auto_scan`），拔插网线、
  切换 WiFi 后设备列表会自己跟上；界面上还有「重新检测网卡」按钮可以立即重算。

单播探测的流量很小：扫一个 /24 网段就是 254 个约 200 字节的小包（≈50 KB）。

本机网卡（哪张有线、哪张无线、各自网段和广播地址、单播探测范围）在控制端
启动日志里逐行列出来，`--selftest` 也会打印：

```bat
WallpaperController.exe --selftest
WallpaperAgent.exe --selftest
```

---

## 二、快速开始

被控端有**两种部署方式，二选一**（不要在同一台机器上混用）：

| 方式 | 适用场景 | 怎么做 |
|---|---|---|
| **免安装版 exe** | 几台机器，手动拷 | 直接拷 `WallpaperAgent.exe`，再跑一次 `add_firewall_rules.bat` |
| **MSI 安装包** | 几十上百台，GPO / SCCM 批量下发 | `build_msi.bat` 打包，然后 `msiexec /i ... /qn` |

下面先讲免安装版；MSI 版见下面的「四、MSI 无人值守部署」。

### 1. 安装依赖并打包

```bat
python -m pip install pyinstaller pillow
build.bat
```

打包完成后在 `dist\` 下得到两个 exe：

| 文件 | 说明 | 放在哪 |
|---|---|---|
| `WallpaperController.exe` | 控制端，带界面 | 你自己操作的电脑 |
| `WallpaperAgent.exe` | 被控端，带状态界面 | 每一台要换壁纸的机器 |

**每个 exe 都是彻底自包含的单文件**：Python 运行时、通知组件（BurntToast）全都打在里面，
拷过去就能跑，不需要外网、不需要装 Python、不往系统里写任何依赖。被控端只拷
`WallpaperAgent.exe` 一个文件即可（`dist\BurntToast\` 只是可选的「覆盖/升级模块」入口）。

> 打包用的是单文件模式（`--onefile`），首次启动会把运行时解压到临时目录，
> 大约需要 2~5 秒。如果想要秒开，把 `build.bat` 里的 `--onefile` 换成
> `--onedir` 重新打包即可（产物是一个文件夹，启动快很多）。

### 2. 放行防火墙（**每台机器都要做，包括控制端**）

右键 `add_firewall_rules.bat` → **以管理员身份运行**。

手动等价命令：

```bat
netsh advfirewall firewall add rule name="Win壁纸推送-UDP广播" dir=in action=allow protocol=UDP localport=38571
netsh advfirewall firewall add rule name="Win壁纸推送-回执"   dir=in action=allow protocol=UDP localport=38573
netsh advfirewall firewall add rule name="Win壁纸推送-TCP传输" dir=in action=allow protocol=TCP localport=38572
```

### 3. 部署被控端

**免安装版现在一条命令就装好了**（写开机自启 + 放行防火墙 + 立刻静默后台运行）：

```bat
WallpaperAgent.exe --install
```

它做的事：

| 步骤 | 说明 |
|---|---|
| 1 | 写 `HKCU\...\Run\WinWallpaperAgent` → `"<exe 自己的路径>" --silent`（每个用户登录时静默启动） |
| 2 | 放行入站 `UDP 38571`；不是管理员时会**单独弹一次 UAC** 只为了加这条规则，被控端本身仍以普通权限运行 |
| 3 | 立刻在后台把被控端跑起来，并等它确认真的活了（不是喊一声就算） |
| 4 | 打印自启状态，随时可查 |

不想用命令行也行：**把 exe 拷过去双击运行，在窗口里勾上「登录时自动运行」** —— 界面上还有防火墙的状态和「放行…」按钮，一个窗口里就能把该配的都配完。

想撤掉：

```bat
WallpaperAgent.exe --uninstall     :: 停掉运行中的实例 + 删掉自启（不删程序文件）
```

脚本包装版（可选，等价于上面两条）：

```bat
enable_autostart.bat          :: -> WallpaperAgent.exe --install
enable_autostart.bat /task    :: 改用「登录计划任务」（Run 项被组策略/优化软件封掉时用，需管理员）
disable_autostart.bat         :: -> WallpaperAgent.exe --uninstall
```

#### 重启后没有自启？一条命令看原因

```bat
WallpaperAgent.exe --status
```

它会把自启项逐条列出来：写在 HKLM 还是 HKCU、命令行长什么样、目标文件还在不在、
有没有被「任务管理器 → 启动」禁用。常见原因就这四种：

| 原因 | `--status` 会显示 | 怎么修 |
|---|---|---|
| 压根没设置 | `开机自启：未设置` | `WallpaperAgent.exe --install` |
| 被「任务管理器 → 启动」或某个"优化大师"禁用了 | `【已被禁用】` | 在任务管理器里启用它，或重新跑 `--install`；如果总被关，用 `enable_autostart.bat /task` |
| exe 被挪走 / 删掉了 | `【目标文件不存在】` | 把 exe 放回原位置，重新跑 `--install` |
| 没人登录，或换了个用户登录 | — | 见下面两条 |

两条容易误解的地方：

* **自启项是在「用户登录时」执行，不是「开机时」。** 桌面壁纸是每用户设置，
  服务/开机阶段没有用户会话，改了也没人看得见。所以机器停在锁屏、一直没人登录，
  就不会有壁纸变化 —— 这不是没自启。
* **免安装版写的 `HKCU` 只对「运行 --install 的那个用户」生效。**
  要所有登录用户都自启，用 MSI（写 `HKLM\...\Run`），或者对每个用户各跑一次。

> 如果 `--status` 显示自启项在、也没被禁用，但**登录后进程还是没起来**：大概率是
> 组策略把 Run 项封了（`DisableLocalMachineRun` / `DisableCurrentUserRun` 之类），
> 或者安全软件拦了未签名程序。前者用 `enable_autostart.bat /task`，后者给 exe 加白名单。

### 4. 启动 / 看状态 / **停止**（重要）

被控端默认就是「静默后台」跑的：没有窗口、没有托盘图标。所以它专门有一组
命令，不用去任务管理器里翻进程：

```bat
WallpaperAgent.exe --silent    :: 静默启动（后台，窗口藏起来）
WallpaperAgent.exe --show      :: 把后台那个窗口叫出来（能看到状态，也能点退出）
WallpaperAgent.exe --status    :: 在不在跑？端口、配置、日志都在哪（在跑返回 0）
WallpaperAgent.exe --stop      :: 让后台那个进程退出
```

懒人版：

```bat
start_agent.bat      :: 静默启动 + 打印状态
stop_agent.bat       :: 停止（顺带告诉你自启项还在不在）
```

几个要点：

* **同一台机器只会跑一个被控端。** 自启项、快捷方式、用户双击都可能把它拉起来，
  第二个实例会自己退出，不会出现两台一起抢广播。
* **`--stop` 只停当前这次运行。** 自启项还在，下次登录它会重新起来；要彻底
  不再自启，用 `disable_autostart.bat`（免安装版）或卸载 MSI。
* **`--stop` 要和你平时登录的那个用户会话对上**（控制通道是「同会话」的）。
  多用户同时登录时，其它会话里的实例要用管理员的 `taskkill /IM WallpaperAgent.exe /F /T`
  一起结束 —— `stop_agent.bat` 会提示这一点。
* **装了 MSI 的机器**不用记命令：开始菜单里有「Win 壁纸推送 → 显示壁纸接收端窗口」
  和「壁纸接收端控制台（状态 / 停止 / 看日志）」。

> `/T` 不能省（手动 taskkill 时）。PyInstaller 单文件模式的 exe 会派生子进程
> （父进程负责解包、子进程跑真正的程序），任务管理器里能看到两个
> `WallpaperAgent.exe`，只结束一个另一个可能还在跑。`taskkill /T` 会连子进程
> 一起结束。

**日志文件**：被控端会把日志同时写到
`%LOCALAPPDATA%\WinWallpaperPush\agent.log`（超过 512 KB 滚动成 `.1`）。
静默后台运行时这是唯一的排查线索 —— 界面看不到、控制台也没有。用
`WallpaperAgent.exe --status` 能直接看到它的完整路径。

**配置文件位置**：程序第一次运行会在**自己所在目录**生成 `agent_config.json` /
`controller_config.json`。也就是说 exe 放在哪，配置就在哪。把 exe 拷到别的机器时
配置文件不会跟着走（新机器会重新生成一份默认的），这通常正是想要的行为。

### 5. 面板密码（第一次运行会让你设置）

被控端跑在别人机器上、又没有窗口，所以它有一道口令闸门：

* **第一次运行**（第一次打开面板，或跑 `--install`）会提示你**设置密码**（输两次确认）。
* 之后**每次打开面板都要输密码** —— 双击 exe、`--show`、开始菜单的「显示窗口」都一样。
* **停止运行**（`--stop`、控制台里选 3）和**卸载 / 取消自启**（`--uninstall`）同样要输密码。
* 密码连错 3 次直接拒绝；**无人值守环境（没有输入界面）一律拒绝，而且不会卡在那里等输入**
  （这一点专门测过：没输入时 0.1 秒返回失败，不是挂住）。
* **还没有设过密码时，面板不会被锁死**：先问你要不要现在设一个，你要是点了取消（或者这台机器
  上根本没有可见的输入界面），面板**照样打开**，同时明确提醒「现在任何本地用户都能打开面板 /
  停止 / 卸载它」，面板上那行「面板密码：未设置」也会一直提醒你。
  这样任何时候都有入口补上密码 —— 不会出现"没密码 → 面板打不开 → 没地方设密码"的死循环。
* 面板平时可能是**隐藏**着跑的（静默后台/自启动），所以密码框是特意做成"一定能显示"的：
  如果给隐藏窗口当子窗口（`transient`），Windows 上那个框根本不会显示出来
  （实测 `viewable=False`、尺寸 1×1），用户看不到输入框、等超时被算成"取消"，
  于是永远设不上密码 —— 这个坑已经修掉并且有回归用例。

```bat
WallpaperAgent.exe --set-password            :: 设置 / 修改（改要输旧密码）
WallpaperAgent.exe --set-password --clear    :: 取消密码保护（也要旧密码）
WallpaperAgent.exe --status                  :: 看有没有设、口令存在哪儿
```

密码存的是**加盐 PBKDF2-SHA256 哈希**，没有明文。三个位置，按可信度取用：

| 位置 | 谁能改 | 说明 |
|---|---|---|
| `C:\ProgramData\WinWallpaperPush\secure\agent_password.json` | 管理员（安装程序设好 ACL） | 最可信，存在就以它为准 |
| `agent_config.json` 里的 `password` 段 | 普通用户 | 免安装版方便用；可以把这段复制到别的机器，整个机群共用一个密码 |
| `HKCU\...\WinWallpaperPush\Agent\PasswordHash` | 普通用户 | 兜底，防止「只删配置文件」就绕过 |

**想要真的拦得住人**：用**管理员身份**设置一次密码（MSI 装的机器直接 `--set-password`；
免安装版用管理员 cmd 跑一次）—— 这样文件属主是 Administrators、ACL 也锁好，
普通用户既改不掉也删不掉（`--status` 会显示「已锁：普通用户只读」）。普通用户自己设的话，
ACL 虽然挡着，但文件属主是他本人，`--status` 会显示「未锁」。

> **这道闸门防得住什么、防不住什么**（先说实话，别当安全产品用）：
>
> * 防的是：客户机上的普通用户随手打开面板改设置、点退出、把被控端卸掉、清掉自启。
> * **不防**：本机管理员或任何本地用户仍然可以 `taskkill` 结束进程、直接删掉程序目录。
>   要连这些也管住得靠 Windows 权限 / ACL / 应用白名单，不是被控端自己能做主的。
> * MSI 的卸载（`msiexec /x`）也拦不住口令 —— 那是管理员/SYSTEM 在操作。

### 6. 控制端界面（2026-09 重做）

打开 `WallpaperController.exe`，整个软件就是**一个窗口、四个页面**，没有弹窗：

```
┌ 📡 壁纸推送控制端                    在线 6 台     ● 就绪 ┐
├ ( 壁纸 ) ( 通知 ) ( 远程命令 ) ( 设置 )  ┆  局域网设备  [🔍 扫描] ┤
│                                 ┆                            │
│  图片  [___________] [浏览…]    ┆  在线 6 台 / 共发现 8 台   │
│  契合度 [填充 ▾]                ┆  IP │ 计算机名 │ 用户 │ …  │
│  ┌──────── 预览 ────────┐       ┆  ───┼─────────┼──────┼───  │
│  └──────────────────────┘       ┆                            │
│  [🚀 广播推送]  本次任务 …       ┆  （设备列表是主体，常驻）   │
├─────────────────────────────────┴────────────────────────────┤
│ ▸ 运行日志                                        [清空]      │
└──────────────────────────────────────────────────────────────┘
```

* **壁纸 / 通知 / 远程命令 / 设置** 四个页签 —— **发送通知不再是弹窗**，就在「通知」页里就地编辑，
  编辑时右边的设备列表、底下的日志全都看得见；
* **设备列表常驻右侧**（推给谁、结果如何是这个软件最核心的信息），
  中间那条竖线可以拖动，左右比例自己调；
* **运行日志默认收起**成一行，点标题才展开（排查时才占地方），`Ctrl+L` 清空；
* 偶尔才改的东西全挪进「设置」页：**额外网段/IP**、深度扫描、自动重扫、重新检测网卡；
* 解释性文字改成**鼠标悬停提示**，界面上只留字段名；
* 窗口大小会记住（写在 `controller_config.json` 的 `win_w` / `win_h`，第一次打开按内容自适应）；
* 快捷键：`F5` 扫描、`Ctrl+Enter` 推送壁纸、`Ctrl+Enter` 发送通知、`Ctrl+L` 清空日志。

> 界面截图放在 `screenshots\`（壁纸页 / 通知页 / 通知页展开高级 / 设置页 / 日志展开 / 打包后的 exe），
> 换界面之后可以直接对比着看。

### 7. 推送壁纸

在「壁纸」页：

1. 点「浏览…」选一张图，下面显示预览（尺寸 / 大小 / 文件名）
2. 选「契合度」（填充 / 适应 / 拉伸 / 平铺 / 居中 / 跨区）
3. 点右侧「🔍 扫描」确认能看到目标机器
   * 「深度扫描（逐台单播）」默认勾选：广播之外再挨个单播一遍，
     交换机 / AP 拦广播时靠它找人（关掉就只广播）
   * 「自动重扫」默认勾选：每 30 秒自动扫一次，后开机的客户机会自己出现
   * 「重新检测网卡」：拔插网线、切换 WiFi 之后点一下，重新枚举网卡和网段
   * **扫不到某个网段**（例如有线那个 `10.127.112.0`）→ 到「设置」页填
     **额外网段/IP**，然后点「应用」：

     ```
     10.127.112.0/24                一个网段：定向广播 10.127.112.255 + 逐台探测 254 个地址
     10.127.112.255                 广播地址
     10.127.112.10                  单台机器（单播给它）
     10.127.112.1-10.127.112.60     地址范围
     ```

     多个用空格或逗号分隔；填完下面那行灰字会**立刻告诉你解析成了什么**
     （发到哪、要探测多少个地址），填错了当场标红。同网段本来就会自动广播，
     这个框是给"跨网段 / 大网段扫不全"用的（/16 这种默认只扫本机那 254 个地址）。
4. 点「🚀 广播推送」

**推送是怎么保证「每台都改」的**（设备一多就漏机器的问题）：

1. **广播**：每张网卡各发一份、重复 3 遍（抵消 UDP 丢包）；
2. **直接单播给已知设备**：设备列表里出现过的机器，公告会再单独发一次 ——
   就算交换机 / AP 把广播拦了，只要它在线就能收到；
3. **自动补发**：隔几秒查一次回执，谁没确认就单独再单播给它（最多 3 轮），
   日志里会写「第 2 轮补发：N 台还没回执」。

设备列表会实时显示每台机器的 **已应用 / 失败 / 待确认** 状态。「设置」页最上面写着
控制端当前挂在哪些网段上（**连断开的网卡也会列出来并说明原因**）—— 扫不到设备时先看这里。

### 8. 发送通知（Toast）

切到「通知」页（同一个窗口，不弹窗）：

* 默认只显示常用的几项（标题、正文、声音、紧急、按钮）—— 界面不吓人；
* **停留**：横幅在屏幕上停多久，三档可选（Windows 只给这三档，没有"随便填秒数"）：

| 选「停留」 | 实际效果 |
|---|---|
| 短（约 5 秒） | 默认。横幅 5 秒左右自动收起，进通知中心 |
| 长（约 25 秒） | XML 加 `duration="long"`，横幅停约 25 秒 |
| 一直显示到用户处理（循环响铃） | `duration="long"` + `scenario="alarm"` + 循环音频：**不自动消失**，要用户点掉（像闹钟）。选它时会自动把声音换成循环铃、取消「紧急」和「静音」（Windows 的场景只能有一个） |

> 别搞混两个"时间"：**停留** = 横幅在屏幕上停多久；**过期**（高级选项）= 这条通知在**通知中心**里留多久。

* 顶上有 **预设**（普通通知 / 维护公告 / 紧急提醒 / 到期提醒），点一下就把字段填好
  （维护公告和紧急提醒默认用「长」停留）；
* 点 **「▸ 高级选项」** 才展开第三行文字、图标/大图、署名、分组、进度条、唯一标识、
  过期时间、只进通知中心；展开后内容变高也不怕 —— 这一页可以滚动，不会有控件被切掉；
* 高级区是**单列、随宽度自适应**的排版（字段名一列 + 会伸展的输入框），页宽窄到 470px
  也不会把右边的字段挤出去；进度条那三项（状态文字 / 进度标题 / 进度值）分三行排，
  不互相挤；
* **「📢 发送通知」固定在页脚**：高级区展开后内容有两屏高，按钮也不会被滚出视野；
* 下面实时预览，改哪个字段都能立刻看到大概长什么样；
* 「📢 发送通知」下面那个「只发本机（测试用）」勾上就只发给 127.0.0.1，方便试样式。

展开后每个字段对应 BurntToast `New-BurntToastNotification` 的参数：

| 界面上的项 | 对应参数 | 说明 |
|---|---|---|
| 标题 / 正文 / 第三行 | `-Text` | 第一行是标题，最多 3 行 |
| 应用图标 / 大图 | `-AppLogo` / `-HeroImage` | 选本地图片，随通知下发给被控端（sha256 校验） |
| 声音 / 静音 | `-Sound` / `-Silent` | 26 种系统声音（Default、Alarm1-10、Call1-10、IM、Mail、Reminder、SMS…） |
| 紧急 | `-Urgent` | 「重要通知」，可穿透专注助手 |
| 稍后提醒 + 关闭 | `-SnoozeAndDismiss` | 系统自带的那两个按钮 |
| 只进通知中心 | `-SuppressPopup` | 不弹横幅，只在通知中心里躺着 |
| 署名 | `-Attribution` | 底部一行小字 |
| 分组 | `-Header` | `New-BTHeader -Id -Title`，同类通知归类 |
| 进度条 | `-ProgressBar` | 标题（可选）+ **状态文字（必填，就是进度条下面那行字）**；可固定百分比，也可「不确定进度」转圈。两个都不填 = 不显示进度条 |
| 停留 | XML `duration` / `scenario` | 横幅停多久：短（约 5 秒）/ 长（约 25 秒）/ 一直显示到用户处理（闹钟场景 + 循环响铃） |
| 唯一标识 | `-UniqueIdentifier` | 留空自动用任务号；填了的话同标识的新通知顶掉旧的 |
| 过期时间 | `-ExpirationTime` | 多少分钟后从通知中心消失（0 = 不过期） |
| 按钮（最多 5 个） | `-Button` | `New-BTButton`，只能打开 http/https 网址 |

发送后在右侧设备列表和（展开后的）日志里能看到每台机器的结果 —— 失败原因也会带回来，
而且是**能照着做**的说明（比如「本机没有 BurntToast 通知模块」后面直接跟四条安装办法）。

命令行也能推（方便脚本化 / 计划任务）：

```bat
WallpaperController.exe --push-toast "D:\toast.json" --wait 8
WallpaperController.exe --push-toast "D:\toast.json" --toast-local   :: 只发本机，测试用

:: 批量改被控端「通知上显示的应用名」（持久化；default = 恢复默认）
WallpaperController.exe --set-app-name "IT 运维通知" --wait 8
WallpaperController.exe --set-app-name default --wait 8
```

```json
{
  "text": ["系统维护通知", "今晚 22:00 起进行网络割接", "预计 30 分钟"],
  "sound": "Reminder",
  "urgent": true,
  "duration": "long",
  "attribution": "运维组",
  "hero_image": "hero.png",
  "assets": { "hero.png": "D:\\图片\\维护公告.png" },
  "buttons": [{ "content": "查看详情", "arguments": "https://intranet.example.com/notice" }],
  "expire_minutes": 1440
}
```

> `assets` 只在命令行模式下用：JSON 里写资源名，值是本地文件路径。界面里是用
> 「浏览…」选图，效果一样。

**「署名」和「通知上显示的应用名」是两回事**（容易被问）：

| 想改的是 | 字段 / 设置 | 作用范围 |
|---|---|---|
| 通知**底部**那行小字（如「运维组」） | 通知里的 `attribution` | **跟着每条消息走**，控制端在「通知」页 → 高级选项 → 署名里填 |
| 通知**顶部**那个名字（默认「Win 壁纸推送」，通知中心里也用它分组） | 三种都行：① 控制端「设置」页 → **被控端通知应用名** → 填名字 → 「下发到被控端…」（**批量**改，最省事）；② 被控端本机 `--set-toast-app-name "IT 运维通知"` / 面板「通知应用名」→「改名…」；③ 配置 `toast_app_name` 集中下发 | ① 广播给所有在线被控端，它们会**写进自己的配置**（重启仍生效）；②③ 是每台机器各自的设置 |

> **远程改名是持久化改动**，所以被控端保留了拒绝的权利：配置里
> `"allow_remote_app_name": false` 就完全不接受控制端的改名指令（回执里会说明原因）。
> 默认是**允许**（这是给运维批量统一署名的功能）。风险提示：本工具的通知推送本身
> 没有鉴权，局域网里能发包的人本来就能推任意通知；改名只是让"冒充的名字"也能被改掉，
> 拿来做钓鱼更像真的。介意的话就在客户机上关掉这个开关，或者把整个网络管好。

**通知组件（BurntToast）已经打进 exe 里面了** —— 客户机**只拷一个 `WallpaperAgent.exe`**
就能发通知，不需要外网、不需要 PSGallery、不需要管理员、也不会有第二个文件夹。
被控端还额外留了三层自愈：

1. `--install` 时如果连内置模块都读不到，再 `Install-Module BurntToast -Scope CurrentUser`；
2. 启动后 8 秒在后台查一次，缺了就自动补；
3. **每次要弹通知前**再查一次 —— 缺模块就现场装，装不上则**拒绝这条通知并把安装办法回报给控制端**
   （设备列表里能看到，不会再出现"莫名其妙的模块加载失败"）。

模块的查找顺序（先找到的先用）：

| 顺序 | 位置 | 说明 |
|---|---|---|
| 1 | 配置里 `toast_module_path` | 运维手工指定 |
| 2 | exe 旁边的 `BurntToast\` | **覆盖/升级模块用**，放这里就盖过内置的那份，不用重新打包 |
| 3 | `%LOCALAPPDATA%\WinWallpaperPush\modules\BurntToast` | 自动装的、`--source` 拷的，或由内置模块释放来的 |
| 4 | **exe 内部**（打包时 `--add-data`） | 默认走这条：发现内置模块时会顺手释放一份到第 3 条那个目录再用 |
| 5 | 系统已装的 PowerShell 模块 | 最后兜底 |

> 为什么内置模块要先"释放"一份出来：exe 内部的东西运行时解包在 `%TEMP%\_MEIxxxx`，
> 那是临时目录 —— 存储感知、CCleaner 这类清理工具可能在程序还运行着的时候就把它删掉，
> 而弹通知时 PowerShell 是真要去读那个目录的，删掉就变成查不出原因的静默失败。
> 释放到 `%LOCALAPPDATA%` 之后路径稳定（也方便运维进去看/替换），释放失败时仍然会
> 直接用临时目录里那份，不会误报"缺模块"。

为什么不影响体积：内置的是**裁剪版**模块。完整的 BurntToast 里 21.8 MB 中有 20.75 MB 是
`lib\Microsoft.Windows.SDK.NET\Microsoft.Windows.SDK.NET.dll`，它是 Microsoft Toolkit 那条
**兼容提交路径**（`ToastNotificationManagerCompat`）专用的；本工具为了通知署名正确，
本来就用显式 AppId 直接走 WinRT 提交，从不加载那条路径 —— 所以裁掉它之后模块只有约 1 MB
（`--test-toast`、带大图/按钮/紧急标记的通知都实测正常），打进 exe 后
`WallpaperAgent.exe` 从 17.3 MB 变成 17.8 MB，启动几乎无感。

真要用完整版（比如你自己写脚本用 Toolkit 的兼容 API），两条路：

```bat
prepare_toast_module.bat      :: 在能上网的机器上跑一次，产出 .\BurntToast\（已裁剪到约 1 MB）
```

```bat
WallpaperAgent.exe --install-toast-module                          :: 先用 exe 内置的那份；没有才去 PSGallery
WallpaperAgent.exe --install-toast-module --source D:\BurntToast    :: 从本地目录装（裁剪版也行）
```

`--install-toast-module` 现在优先用 exe 内置的模块（释放到本机模块目录，不需要外网），
只有 exe 里那份也不在时才会去连 PSGallery —— 所以离线客户机上跑它也不会卡住。

想关掉自动安装（比如受管控的机器不允许联网取模块）：配置里 `"toast_auto_install": false`。

**通知是以谁的名义弹出来的**：默认情况下 BurntToast 会跟着「宿主进程」的身份走，
通知显示成「Windows PowerShell」——用户看着莫名其妙，而且 PowerShell 发的带网址按钮的
通知正是典型的钓鱼样式，很容易被 IT / 安全软件盯上。所以被控端会注册**自己的通知身份**：

| 做了什么 | 位置 |
|---|---|
| 注册 AppUserModelID（名字 + 图标） | `HKCU\Software\Classes\AppUserModelId\WinWallpaperPush.Agent` |
| 生成图标 | `%LOCALAPPDATA%\WinWallpaperPush\wpp.ico` |
| 建一个带该 AppId 的开始菜单快捷方式（点开就是面板） | `%APPDATA%\...\Start Menu\Programs\Win 壁纸推送.lnk` |

渲染时用**带 AppId 的 notifier** 提交（不是 BurntToast 那个无参的
`CreateToastNotifier()`，否则身份又会变成 PowerShell）。之后通知就显示
「Win 壁纸推送 + 我们的图标」。这几步是幂等的，装机时做一次，之后每次启动会自愈。

**通知优先级（第一次运行自动设为最高）**。Windows 把「每个应用的通知设置」放在：

```
HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\<AppUserModelID>
```

被控端第一次运行时会在自己的这个子键下写：

| 值 | 写什么 | 作用 |
|---|---|---|
| `AllowUrgentNotifications` | `1`（DWORD） | 就是系统「设置 → 系统 → 通知 → Win 壁纸推送 → **允许紧急通知**」那个开关 |

打开它的意义：控制端发通知时勾上「紧急」（toast XML 里的 `scenario="urgent"`），
这条通知就会被系统当成**重要通知** —— 能**穿透专注助手（勿扰）**、在通知中心里排在前面、
并且不会因为"通知太多"被自动折叠掉。这是这台机器上真实存在、能靠注册表控制的那个开关
（实测 Win11 26200：该子键下只有 `AllowUrgentNotifications` / `LastNotificationAddedTime`
等值，没有 `Enabled`、也没有按应用的 `Priority`）。顺带说明：Win11 设置里那个
「**设置优先级通知**」的应用列表是 CloudStore 托管的、没有公开稳定的注册表值，
所以本工具不去硬写它 —— 「最高优先级」是通过上面这个开关 + 发通知时带紧急标记实现的。
如果这台机器上恰好存在老版 Windows 的全局值 `NOC_GLOBAL_SETTING_PRIORITY_APPS`，
程序会顺带把我们的 AppId 追加进去（保留原有列表格式，不覆盖别人）。

**只设一次，不跟用户对着干**：设置过之后会写一个标记
`%LOCALAPPDATA%\WinWallpaperPush\notification_priority.json`，以后启动不再改动 ——
用户在系统设置里关掉「允许紧急通知」就是关掉了，不会被程序偷偷改回来。
要显式打开/关闭：

```bat
WallpaperAgent.exe --allow-urgent on     :: 设为最高（写 AllowUrgentNotifications=1）
WallpaperAgent.exe --allow-urgent off    :: 关掉（=0），并记住这个选择
```

装不上不影响壁纸推送，只是通知发不出去（设备列表里会看到失败原因和安装办法）。被控端随时自检：

```bat
WallpaperAgent.exe --test-toast      :: 本机弹一条测试通知（会打印模块来自哪：内置 / exe 旁边 / 本机模块目录）
WallpaperAgent.exe --status          :: 「通知：BurntToast 就绪（exe 内置 → 已释放到本机模块目录）」+「通知身份：已注册为「Win 壁纸推送」」+「通知优先级：最高（AllowUrgentNotifications=1）」
```

---

### 9. 远程命令（SSH）—— 在每台设备上执行命令

控制端的「远程命令」页：填一条命令（或几条），一次发到局域网里的每台设备上执行，
每台的**远端回显**都带回来当证据。它执行的就是这条原始命令：

```bat
ssh -i "C:\User\.ssh\id_ed25519" Lonovo@<IP> "命令"
```

```
┌ 目标（发给哪些设备） ──────────────────────────────────────┐
│ ◉在线设备（48）  ○全部已发现（52）  ○自定义网段/IP        │
│ 自定义 [10.127.112.1-56            ] [取右侧选中] [扫描]  │
│ 解析出 56 个地址（10.127.112.1 … 10.127.112.56）          │
├ 登录设置 ─────────────────────────────────────────────────┤
│ 私钥 -i [C:\User\.ssh\id_ed25519             ] [浏览…]    │
│ 用户名 [Lonovo]  附加参数 [-o ConnectTimeout=10]          │
│ ☑首次连接自动接受主机密钥  并发[4] 单条超时[25]秒 …        │
│ ssh 客户端：C:\WINDOWS\System32\OpenSSH\ssh.EXE · 私钥已找到│
├ 命令（一行一条，按顺序执行） ─────────────────────────────┤
│ 常用命令[禁用写保护并重启（两步）▾][填入][追加] 方式[会话式▾]│
│ ┌ uwfmgr filter disable                                  ┐│
│ │ shutdown /r /t 0                                        ││
│ └─────────────────────────────────────────────────────────┘│
├ 执行结果（双击某一行看完整远端回显） ─────────────────────┤
│ IP 地址    │ 状态     │ 退出码 │ 远端回显（证据）          │
│ 10.127.112.1│ ✅ 成功  │        │ 已成功禁用统一写过滤器。   │
│ 10.127.112.5│ ❌ 失败  │        │ 拒绝访问。                │
├───────────────────────────────────────────────────────────┤
│ [▶ 发送到设备] [■ 停止] [生成批处理…] [导出结果…]  提示行   │
└───────────────────────────────────────────────────────────┘
```

**为什么这件事做进了控制端，而不是另做一个工具**：目标默认就是控制端
**已经发现的在线设备** —— 不用再手填 IP 段，也不用两边对名单。想只发给某几台，
就在右边设备列表里选中它们（可多选），点「取右侧选中」。

**两种执行方式**（下拉框里选）：

| 方式 | 怎么做的 | 怎么算成功 | 什么时候用 |
|---|---|---|---|
| **会话式**（默认） | 登录一次，在同一个会话里逐条发命令 | 按远端回显判断（有「拒绝访问 / 不是内部或外部命令」这类字样就是失败） | 连着做几件事、命令之间要共享状态（例如先禁写保护再重启） |
| **逐条独立** | 每条命令单独一次连接 | 看**真实退出码**：0 = 成功，其它 = 失败 | 就是要确认"到底成没成"；一条失败会跳过这台剩下的命令 |

会话式判断「这条命令跑完了」用的是**回显标记**：命令发完紧接着发一行
`echo ---WINWALL-DONE-n---`，远端把它回显出来就说明上一条执行完了。
不是"死等 N 秒"—— 机器快慢不一样，等短了会误判、等长了白等。

**四个按钮**：

* **▶ 发送到设备** —— 执行前一定弹确认框，把「台数 + 按顺序的命令 +
  以第一台为例的完整命令行」原样列出来；`shutdown` 这类命令在几十台机器上
  跑出去是收不回来的，所以不做"点一下就发";
* **■ 停止** —— 已经开始的机器会收尾，不再派新的机器；
* **生成批处理…** —— 生成一个不依赖本软件的 `.bat`（用 `echo y | ssh` 自动应答
  首次连接的 yes），换台机器、进计划任务都能跑。脚本内部**只用英文** ——
  cmd 在不同代码页下解析含中文的批处理会错位（这个坑踩过）；
* **导出结果…** —— 把每台的每一条命令和完整回显写成文本文件。

**结果状态有四种**（都写进表格和日志）：

| 状态 | 意思 |
|---|---|
| ✅ 成功 | 命令执行完，回显里没有错误字样 |
| 📤 已下发 | 命令发出去了，但结果拿不到 —— 例如 `shutdown /r /t 0` 把会话切断了，这是正常的 |
| ⚠ 部分成功 | 几条命令里有成功的也有失败的 |
| ❌ 失败 | 失败原因和目标机器的原话都在「远端回显」列里 |

**常用命令预设**（下拉框，选完点「填入」或「追加」）：
`hostname`、`ipconfig /all`、`query user`、`uwfmgr get-config`、
`uwfmgr filter disable`、`uwfmgr filter disable + shutdown /r /t 0`（两步）、
`shutdown /r /t 0`、`shutdown /s /t 0`、`shutdown /a`、`systeminfo`、
`wmic logicaldisk`、`where ssh`、`del /q /f /s %TEMP%\*`。

命令框的规矩：**一行一条命令**，空行忽略，`#` 或 `::` 开头的行是注释
（可以把命令存下来下次直接用）。从文档里复制时带的提示符（`C:\>` / `PS C:\>` / `$ `）
会被自动去掉。

**要在被控端准备什么**（一次就够）：

1. 装 **OpenSSH 服务端**（被控端）：`设置 → 应用 → 可选功能 → 添加功能 → OpenSSH 服务器`，
   然后 `Start-Service sshd; Set-Service sshd -StartupType Automatic`；
2. 把控制端那把**公钥**放进目标账号的
   `C:\Users\<用户>\.ssh\authorized_keys`（管理员账号才能跑 `uwfmgr` 这类命令）；
3. 被控端防火墙放行 **TCP 22**（装 OpenSSH 服务端时通常已自动放行）。

控制端这一侧不需要装任何东西（Windows 10/11 自带 `ssh.exe`）；界面上会实时显示
"ssh 客户端：…… · 私钥已找到"，**没有 ssh 客户端会直接标红**并告诉你去哪装 ——
这是"一条命令都没跑成"最常见的原因。

命令行用法：

```bat
:: 发给当前在线设备（先自动扫一遍局域网）
WallpaperController.exe --ssh-cmd "hostname"

:: 只发给某个网段 / 范围（不扫描）
WallpaperController.exe --ssh-cmd "uwfmgr filter disable" --ssh-targets 10.127.112.1-56
WallpaperController.exe --ssh-cmd "shutdown /r /t 0" --ssh-targets 10.127.112.0/24

:: 多条命令（给多次 --ssh-cmd，或在一条里用换行）
WallpaperController.exe --ssh-cmd "uwfmgr filter disable" --ssh-cmd "shutdown /r /t 0"

:: 要真实退出码（每条命令单独一次连接）
WallpaperController.exe --ssh-cmd "ipconfig /all" --ssh-mode oneshot --ssh-targets 10.127.112.98

:: 覆盖用户名 / 私钥 / 并发 / 超时
WallpaperController.exe --ssh-cmd "hostname" --ssh-user Lonovo --ssh-key "D:\keys\id_ed25519" --ssh-workers 8 --ssh-timeout 30
```

退出码：`0` 全部成功 · `2` 有失败 · `3` 没找到目标设备 · `1` 参数/环境有问题
（没给命令、解析不出地址、本机没有 ssh 客户端）。

**安全边界（说清楚）**：这个功能就是"用 SSH 在整批机器上执行命令"，
权限完全取决于那把私钥对应的账号 —— 用管理员账号的密钥，就等于在被控端以管理员
身份执行；私钥不会被上传或传输，只是作为 `-i` 参数传给系统自带的 `ssh.exe`。
命令行方式**没有确认框**（本来就是给脚本/计划任务用的），请自己确认命令内容。

---

## 三、命令行用法（方便脚本化 / 批量运维）

打包成 exe 后同样支持。exe 以「无控制台」方式打包，带参数运行时会自动把自己
挂到 cmd 窗口的控制台上，所以照样能看到输出。

```bat
:: 环境自检
WallpaperAgent.exe --selftest
WallpaperController.exe --selftest

:: 被控端：在不在跑 / 停止 / 叫出窗口
WallpaperAgent.exe --status
WallpaperAgent.exe --stop
WallpaperAgent.exe --show

:: 不开界面，直接广播推送一张图，并等待 10 秒收集结果
WallpaperController.exe --push "D:\壁纸\新年.jpg" --style fill --wait 10

:: 只扫描在线设备（默认广播 + 逐台单播）
WallpaperController.exe --scan --wait 5

:: 推一条通知（JSON 文件或直接给 JSON；--toast-local 只发本机做测试）
WallpaperController.exe --push-toast "D:\toast.json" --wait 8

:: 被控端：本机弹一条测试通知 / 安装通知组件
WallpaperAgent.exe --test-toast
WallpaperAgent.exe --install-toast-module

:: 被控端：通知优先级（允许紧急通知）—— 第一次运行会自动打开
WallpaperAgent.exe --allow-urgent on
WallpaperAgent.exe --allow-urgent off

:: 被控端：改通知上显示的应用名（默认「Win 壁纸推送」）；default = 恢复默认
WallpaperAgent.exe --set-toast-app-name "IT 运维通知"
WallpaperAgent.exe --set-toast-app-name default

:: 被控端：现场试三种停留时长（这个只能靠眼睛看）
WallpaperAgent.exe --test-toast --duration long
WallpaperAgent.exe --test-toast --duration until_dismissed

:: 只广播，不做逐台单播（网络设备对 UDP 扫描敏感时用）
WallpaperController.exe --scan --no-sweep --wait 5

:: 关掉后台自动重扫（默认 30 秒）
WallpaperController.exe --auto-scan 0

:: 跨网段推送（额外指定目标网段的广播地址）
WallpaperController.exe --push "D:\壁纸\a.jpg" --targets 192.168.2.255,10.0.0.255

:: 被控端：直接用一张本地图片设置壁纸（测试用）
WallpaperAgent.exe --set "D:\图片\test.jpg"
```

**契合度可以用英文别名**（批处理文件里写中文容易乱码，推荐用英文）：

| 英文别名 | 中文 | 效果 |
|---|---|---|
| `fill` | 填充 | 铺满屏幕，超出部分裁掉，**不变形**（推荐） |
| `fit` | 适应 | 完整显示，可能有黑边 |
| `stretch` | 拉伸 | 强制铺满，**会变形** |
| `tile` | 平铺 | 原尺寸重复排列 |
| `center` | 居中 | 原尺寸居中 |
| `span` | 跨区 | 跨多显示器拼接 |

也接受注册表数值：`10`=fill `6`=fit `2`=stretch `0`=center `22`=span。

**`--push` 的退出码**：`0` 至少一台确认成功 · `2` 有设备在线但无人确认 · `3` 没发现在线设备。

### 在批处理 / 计划任务里使用退出码

exe 是无控制台程序，**cmd.exe 不会等待它结束**，直接写 `%ERRORLEVEL%` 会拿到空值。
两种正确用法：

```bat
:: 方式一：用 start /wait（已封装成 push_wallpaper.bat，推荐）
start /wait "" "dist\WallpaperController.exe" --push "D:\壁纸\a.jpg" --style fill
echo 退出码 = %ERRORLEVEL%

:: 方式二：用 PowerShell（PowerShell 会等待进程结束）
powershell -Command "& 'dist\WallpaperController.exe' --push 'D:\壁纸\a.jpg'; exit $LASTEXITCODE"
```

项目里已经带了一个开箱即用的包装脚本：

```bat
push_wallpaper.bat "D:\壁纸\新年.jpg" fill
```

清屏后会打印推送结果和退出码，适合直接丢进「任务计划程序」做定时轮换壁纸。

---

## 四、MSI 无人值守部署（批量部署推荐）

要部署到几十上百台机器时，别再一台台拷 exe —— 用 MSI 配合 GPO / SCCM / Intune
一条命令推下去。

> **只想在 cmd 里一条命令装完**，直接跳到 [4.2 静默安装](#42-静默安装--卸载)：
>
> ```bat
> powershell -NoProfile -Command "Start-Process 'D:\wpp\install_agent.bat' -Verb RunAs -Wait"
> ```
>
> （`install_agent.bat` 和 `WallpaperAgent.msi` 放在同一个文件夹里即可；
> 已提权的 cmd 里也可以直接 `msiexec /i "D:\wpp\WallpaperAgent.msi" /qn /norestart`）

### 4.1 构建 MSI

```bat
build_msi.bat
```

首次运行会自动从 **NuGet** 下载 WiX v3.14 工具链（约 40 MB，缓存在 `.tools\`），
之后可离线重复构建。

产物：

```
dist\WallpaperAgent.msi      约 18 MB
```

> **为什么用 WiX v3 而不是 v4/v5/v6**：v4 以上要装 .NET SDK（约 200 MB）。v3 的
> `candle.exe` / `light.exe` 直接跑在 Windows 自带的 .NET Framework 4.x 上，
> 开箱即用。本机没有 .NET 3.5 也没有 SDK，v3 是唯一不需要额外前置条件的方案。

> **为什么从 NuGet 而不是 GitHub Releases 下载**：GitHub Releases 的实际文件由
> `objects.githubusercontent.com` 下发。很多企业网络允许访问 github.com 本身，
> 却把这个 CDN 域名挡掉 —— 表现出来就是「能取到元数据，但文件下载超时」。
> NuGet 是微软官方包仓库，做 Windows 部署的环境基本都会放行。
>
> `get_wix.py` **只走 NuGet 一个来源**，但会依次尝试三种出口：
> **直连 → 环境变量代理 → 系统（WinINET）代理**。全部失败时打印手工下载步骤：
> 把 `.nupkg`（就是个 zip）解压到 `.tools\wix3\`，再跑一次 `build_msi.bat`
> 即可跳过下载。
>
> 内网有 NuGet 镜像的话，可以直接指过去：
>
> ```bat
> python installer\get_wix.py --dest .tools\wix3 --url https://内网镜像/wix.3.14.1.nupkg
> ```
>
> 用 Python 做下载而不是 PowerShell / curl，是因为在受限网络里实测
> Windows PowerShell 5.1 的 `Invoke-WebRequest` 和系统自带的 `curl.exe`
> 都连不上，而 Python（本身就是本项目硬依赖）可以。

### 4.2 静默安装 / 卸载

#### 一条命令装完（推荐）

把 `install_agent.bat` 和 `WallpaperAgent.msi` 放在**同一个文件夹**里
（例如 `D:\wpp\`，脚本会自己在同目录 / `dist\` 子目录里找 MSI），然后在
**任意 cmd 窗口**（不需要是管理员）粘贴这一条：

```bat
powershell -NoProfile -Command "Start-Process 'D:\wpp\install_agent.bat' -Verb RunAs -Wait"
```

它会弹**一次** UAC，然后一口气做完：静默安装 → 复查防火墙端口 → 在当前用户会话里
把被控端静默启动 → 打印进程状态和 `--status`，最后停住等你按键，方便看结果。

无人值守 / 脚本里跑、不需要看输出时，加一个 `/quiet`（不 `pause`，装完窗口就关，
退出码就是 msiexec 的退出码，`0` / `3010` 为成功）：

```bat
powershell -NoProfile -Command "Start-Process 'D:\wpp\install_agent.bat' -ArgumentList '/quiet' -Verb RunAs -Wait"
```

如果 cmd 本来就是**以管理员身份**打开的，最短的一条是：

```bat
msiexec /i "D:\wpp\WallpaperAgent.msi" /qn /norestart
```

> 这条只装 MSI：端口防火墙规则、`HKLM\...\Run` 开机自启、`ProgramData` 默认配置
> 都由 MSI 自己建好，被控端会在**下次登录**时自动起来。它不做「复查防火墙」和
> 「立刻启动」，需要这两样就用上面那条 `install_agent.bat`。

远程 / 批量下发（PsExec，SYSTEM 身份，不需要 UAC）：

```bat
psexec \\PC01 -s msiexec /i "\\dc\share\WallpaperAgent.msi" /qn /norestart
```

装完想立刻确认，再两条：

```bat
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --status
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --silent
```

#### 各场景对照

| 场景 | 用什么 |
|---|---|
| 单台机器，人工装 | 双击 `install_agent.bat`（自动提权，最后停住让你看结果） |
| **cmd 里一条命令（要能看到输出）** | `powershell -NoProfile -Command "Start-Process 'D:\wpp\install_agent.bat' -Verb RunAs -Wait"` |
| **cmd 里一条命令（不看输出）** | 同上，加 `-ArgumentList '/quiet'` |
| 已提权的 cmd | `msiexec /i "D:\wpp\WallpaperAgent.msi" /qn /norestart` |
| 域内批量（GPO 软件安装 / 启动脚本） | `msiexec /i "\\DC\share\WallpaperAgent.msi" /qn /norestart` |
| SCCM / Intune | 应用类型选 Windows Installer，安装命令同上 |
| 脚本 / 计划任务（已提权上下文） | `install_agent.bat /quiet` |

**`install_agent.bat /quiet` 做了什么**（每步都会打印，失败返回非 0）：

| 步骤 | 做什么 |
|---|---|
| 1 | 检查管理员权限。**不提权、不弹 UAC**，没权限就直接返回 `1603` |
| 2 | `msiexec /i ... /qn /norestart /l*v dist\install.log` 静默安装 |
| 3 | 复查防火墙：入站 UDP 38571 有没有放行，缺了就补一条 |
| 4 | 在当前用户会话里**立刻**静默启动被控端（以普通权限运行） |
| 5 | 打印进程是否在跑 + `--status` 完整状态 |

第 4 步在「以 SYSTEM 身份运行」时会**自动跳过**（那时候没有用户会话，启动了也改不了
任何人的壁纸），并提示你用「用户上下文」的那一步去启动 —— 这正是 SCCM / GPO 下发的场景。

**批量场景：装完怎么让被控端马上跑起来**

没人会去点「立即启动」，而被控端必须跑在用户会话里，所以 MSI 装完**默认要等下次登录**
（`HKLM\...\Run` 自启）。想装完就生效，就再加一个「以登录用户身份运行」的步骤：

| 平台 | 做法 |
|---|---|
| SCCM | 追加一个程序 / 应用，命令行 `"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --silent`，勾选「Run with user's rights」（部署到用户集合） |
| Intune | 加一个 **Platform script**（PowerShell），内容 `Start-Process "$env:ProgramFiles\WinWallpaperPush\WallpaperAgent.exe" -ArgumentList '--silent'`，并勾选「Run this script using the logged on credentials」 |
| GPO | 用户配置 → Windows 设置 → 脚本 → 登录（其实 HKLM Run 已经把这件事做了，这一步只是想「不用重登」） |

被控端本身就是**单实例**的，就算自启项和这一步同时触发，也只会有一个进程留下来。

```bat
:: 带完整日志安装（排查失败时用）
msiexec /i "dist\WallpaperAgent.msi" /qn /norestart /l*v install.log

:: 卸载（静默）
msiexec /x "dist\WallpaperAgent.msi" /qn /norestart
```

也可以直接双击 `install_agent.bat` / `uninstall_agent.bat`（会自动请求提权）。

**关于权限**：per-machine 安装必须有管理员权限。静默模式下 MSI **无法自行弹 UAC**，
未提权运行会返回 `1603`，日志里的根因是
`错误 1925：你没有足够的特权为该计算机所有用户完成此安装`。

这是设计如此，不是缺陷 —— GPO / SCCM 下发时它们自带 SYSTEM 权限，不会有这个问题。

**退出码**：`0` / `3010` 都算成功（3010 = 装了但建议重启，本工具通常不需要）；
`1603` 致命错误（看 `install.log`）；`1618` 另一个安装正在进行；`1619` 打不开 MSI
（UNC 权限或文件被占用）；`1605` 卸载时表示本来就没装。

### 4.3 MSI 到底改了什么

| 项目 | 值 |
|---|---|
| 安装目录 | `C:\Program Files\WinWallpaperPush\` |
| 写入文件 | `WallpaperAgent.exe`、`agent_console.bat`、`README-AGENT.md` |
| 配置文件 | `C:\ProgramData\WinWallpaperPush\agent_config.json`（Users 可改写） |
| 开机自启 | `HKLM\...\Run` → `"...\WallpaperAgent.exe" --silent` |
| 防火墙 | 入站 UDP 38571，**按端口放行**（与 `add_firewall_rules.bat` 一致） |
| 开始菜单 | `Win 壁纸推送` → 「显示壁纸接收端窗口」+「壁纸接收端控制台（状态 / 停止 / 看日志）」 |
| 日志文件 | `%LOCALAPPDATA%\WinWallpaperPush\agent.log`（每用户，卸载不清理） |
| 卸载清理 | 程序文件、自启项、防火墙规则、开始菜单入口自动移除 |

安装/卸载前还会执行一次 `taskkill /IM WallpaperAgent.exe /F /T` 结束正在运行的旧版本
—— 单文件打包的 exe 运行期间一直占着自身文件，不先结束掉，MSI 替换文件会失败并要求重启。

> **防火墙为什么是端口级而不是绑定 exe**：早先的版本写的是
> `Program="...\WallpaperAgent.exe"`，看着更收敛，但程序路径一变（换目录、
> 换打包方式、单文件解包方式不同）规则就会**静默失配**，表现正好是最难查的
> 「装好了、进程在跑、就是收不到推送」。端口级规则和免安装版
> `add_firewall_rules.bat` 完全一致，那套在现场验证过能用。被控端只有
> UDP 38571 一个入站端口，收敛性上可以接受。

### 4.4 为什么开机自启不写成 Windows 服务

这是壁纸类工具最容易做错的地方。

**桌面壁纸是「每用户」设置，而 Windows 服务运行在会话 0（session 0）。**
服务能改的只是 SYSTEM 账户的壁纸，用户在桌面上**看不到任何变化**。

所以被控端必须运行在**用户自己的登录会话**里。MSI 把自启项写进 `HKLM\...\Run`，
每个用户登录时各启动一份，各改各的壁纸。

副作用：**安装完成后不会立即启动，要等下次登录。** 想马上在当前会话跑起来：

```bat
start "" "C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --silent
```

`install_agent.bat` 会自动做这一步（用一次性计划任务，以**普通权限**而不是管理员
权限启动 —— 提权运行的被控端，普通权限的 `--stop` 打不开它的控制通道，就又会变成
「关不掉」）。

想看看它到底起没起来、日志在哪：

```bat
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --status
```

开始菜单里的「壁纸接收端控制台」就是这条命令的图形壳子（状态 / 显示窗口 / 停止 / 看日志）。

### 4.5 GPO 批量下发

1. 把 `WallpaperAgent.msi` 放到共享目录，例如 `\\DC\share\WallpaperAgent.msi`
   （**必须是 UNC 路径，GPO 软件安装不认识本地盘符**）
2. 组策略管理 → 计算机配置 → 策略 → 软件设置 → 软件安装 → 新建 → 已分配
3. 客户端重启后自动安装

### 4.6 SCCM / Intune

SCCM 程序命令行：

```
msiexec /i "WallpaperAgent.msi" /qn /norestart
```

**检测规则建议用「文件存在」，不要用注册表 ProductCode。**

因为 `.wxs` 里写的是 `<Product Id="*">`，每次重新构建都会生成**全新的 ProductCode**。
固定的只有 `UpgradeCode`（`{6C5CD981-1939-4A76-B5AB-0A1173653E8D}`），
升级安装靠它识别「这是同一个产品的不同版本」。用 ProductCode 做检测，
下次重新打包 MSI 检测就会失效。

推荐检测规则：

```
文件  C:\Program Files\WinWallpaperPush\WallpaperAgent.exe
```

或者按 Publisher / DisplayName 反查卸载项（`uninstall_agent.bat` 就是这么做的）：

```powershell
Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall' |
  Where-Object { $_.GetValue('Publisher') -eq 'WinWallpaperPush' } |
  Select-Object PSChildName, @{n='Name'; e={ $_.GetValue('DisplayName') }}
```

Intune：把 MSI 打成 `.intunewin`，安装命令同上；卸载命令用
`uninstall_agent.bat` 里那套逻辑 —— 先按 Publisher 查出 ProductCode，再
`msiexec /x "<ProductCode>" /qn`。

### 4.7 部署后怎么验证

在被控端机器上执行：

```bat
"C:\Program Files\WinWallpaperPush\WallpaperAgent.exe" --selftest
```

输出包含程序目录、配置文件路径（以及是否只读）、壁纸目录、本机网卡与广播网段
（有线 / 无线都会列出）、开机自启项的实际注册表值。要逐表核对 MSI 本身的内容：

```powershell
powershell -File installer\validate_msi.ps1 -Msi dist\WallpaperAgent.msi
```

这个脚本直接用 Windows Installer 的 COM 接口读 MSI 的表，
**不需要管理员权限，也不会真的安装任何东西**。

### 4.8 集中管理配置

MSI 会在 `C:\ProgramData\WinWallpaperPush\agent_config.json` 放一份默认配置
（默认 `silent: true`），并授予 Users 写权限。所以改端口 / 契合度 / 静默开关
只需要改这个文件再重启被控端，**不必重新打包 MSI**。

被控端的配置查找顺序：

```
--config 参数  →  exe 旁边  →  C:\ProgramData\WinWallpaperPush\  →  %LOCALAPPDATA%\WinWallpaperPush\
```

装到 `Program Files` 后 exe 旁边写不了，程序会自动落到 `ProgramData`；
万一那里也只读，会退到 `%LOCALAPPDATA%` 并在日志里说明。

### 4.9 卸载会留下什么

| 内容 | 是否清理 |
|---|---|
| 程序文件、`HKLM` 自启项、防火墙规则 | ✅ 自动清除 |
| `C:\ProgramData\WinWallpaperPush\agent_config.json` | 通常一并删除；被改写过可能残留，手动删即可 |
| `%LOCALAPPDATA%\WinWallpaperPush\wallpapers\` | ❌ **每用户数据，不会清理** |

### 4.10 上线前建议做代码签名

当前 exe 和 MSI **都没有 Authenticode 签名**，会有两个后果：

- 双击 MSI 时可能弹 SmartScreen 警告
- 部分企业的 AppLocker / WDAC 策略会直接拒绝运行未签名程序

正式批量部署前建议买一张代码签名证书，**先签 exe、再打 MSI**（这样装进去的 exe 也是签名的）：

```bat
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f your-cert.pfx dist\WallpaperAgent.exe
build_msi.bat
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f your-cert.pfx dist\WallpaperAgent.msi
```

---

## 五、配置文件

两个 exe 第一次运行会在**自己旁边**生成配置文件（UTF-8 JSON，可直接改）。
读的时候按 `utf-8-sig` 读 —— **带 BOM 的 UTF-8 也认**（记事本 / PowerShell 5.1 存出来的就是带 BOM 的）；
真读不出来（例如少了个逗号）时控制端会在日志里明说一句，而不是默默用默认值。

**`agent_config.json`**

```json
{
  "udp_port": 38571,
  "style": "填充",
  "keep": 10,
  "wallpaper_dir": "",
  "silent": false,
  "retry": 3,
  "hello_interval": 60,
  "log_file": "",
  "log_max_kb": 512
}
```

| 字段 | 说明 |
|---|---|
| `udp_port` | 监听端口，改了要和控制端一致 |
| `style` | 默认契合度 |
| `keep` | 本地最多保留几张历史壁纸（自动清理旧的） |
| `wallpaper_dir` | 壁纸存放目录，留空则用 `%LOCALAPPDATA%\WinWallpaperPush\wallpapers` |
| `silent` | 不带 `--silent` 启动时是否静默。MSI 的默认配置是 `true`；显式 `--silent` / `--show` 优先于它 |
| `retry` | 单张壁纸下载失败时的重试次数 |
| `hello_interval` | 主动报到的间隔（秒），默认 60；设为 `0` 则不主动报到，只等控制端扫描 |
| `log_file` | 日志文件路径，留空用 `%LOCALAPPDATA%\WinWallpaperPush\agent.log`；填 `off` 关闭写文件 |
| `log_max_kb` | 日志文件上限（KB），超过了滚动成 `agent.log.1`，默认 512 |
| `toast_enabled` | 收不收控制端推来的通知，默认 `true`（关掉就只收壁纸） |
| `toast_module_path` | BurntToast 模块目录；留空按「exe 旁边 → **exe 内置** → 自动装的位置 → 系统已装」找 |
| `toast_min_interval` | 两条通知之间至少隔几秒，默认 `1.0`（防刷屏） |
| `toast_auto_install` | 没有 BurntToast 时是否自动安装（需要能上 PSGallery），默认 `true` |
| `toast_app_name` | 通知上显示的应用名（通知顶部 + 通知中心分组名）；留空 = 「Win 壁纸推送」。改完对**之后弹出**的通知生效 |
| `allow_remote_app_name` | 允不允许控制端广播一下就把本机通知应用名改掉（**持久化**改动）。默认 `true`；设 `false` 就完全拒绝，回执里会写明原因 |

**`controller_config.json`**

```json
{
  "udp_port": 38571,
  "tcp_port": 38572,
  "reply_port": 38573,
  "style": "填充",
  "targets": [],
  "sweep": true,
  "auto_scan": 30.0,
  "last_dir": "",
  "last_file": "",
  "ssh": {
    "key": "C:\\User\\.ssh\\id_ed25519",
    "user": "Lonovo",
    "extra": "-o ConnectTimeout=10",
    "accept_new_hostkey": true,
    "mode": "session",
    "workers": 4,
    "timeout": 25.0,
    "yes_wait": 5.0,
    "wait_between": 0.0,
    "target_mode": "online",
    "targets": "",
    "commands": "hostname"
  }
}
```

| 字段 | 说明 |
|---|---|
| `targets` | 额外网段 / IP 数组，跨网段或大网段扫不全时填。四种写法：`10.127.112.0/24`、`10.127.112.255`、单台 IP、`a-b` 范围。等价于界面「设置」页的「额外网段/IP」 |
| `sweep` | 是否「深度扫描」：广播之外再逐台单播探测（默认 `true`） |
| `auto_scan` | 后台自动重扫间隔（秒），`0` = 关闭，默认 `30` |

`ssh` 这一段就是「远程命令」页的设置（界面上改哪个字段就存哪个字段）：

| 字段 | 说明 |
|---|---|
| `key` / `user` / `extra` | 就是 `ssh -i 私钥 … 用户@IP` 这三段；`extra` 默认 `-o ConnectTimeout=10`（连不通的机器 10 秒收手），清空 = 完全照原始命令 |
| `accept_new_hostkey` | 首次连接自动接受主机密钥（等价于替人敲 `yes`），默认 `true`；关掉后程序仍会替人回答 yes，但主机密钥变过会明确报错 |
| `mode` | `session`（默认，登录一次逐条发，按回显判断）或 `oneshot`（每条命令一次连接，有退出码） |
| `workers` / `timeout` / `yes_wait` / `wait_between` | 并发台数（4）· 单条命令超时秒数（25）· 首次连接等待秒数（5）· 两条命令之间的额外等待（0 = 只靠回显标记判断） |
| `target_mode` | `online`（默认，已发现的在线设备）/ `all`（全部发现过的）/ `spec`（用下面的 `targets`） |
| `targets` | `target_mode=spec` 时的目标：`10.127.112.0/24`、`10.127.112.1-10.127.112.60`、`10.127.112.1-56`（前缀简写）、单台 IP，可空格/逗号分隔多个 |
| `commands` | 命令框里的内容（一行一条，`#` / `::` 开头是注释） |

---

## 六、支持的图片格式

`jpg` / `jpeg` / `png` / `bmp` / `webp` / `gif`。

Windows 10/11 直接接受 jpg/png；如果遇到老系统拒绝设置，程序会自动调用
Pillow 转成 BMP 再试一次。

**避坑：** 壁纸文件必须一直留在磁盘上，删掉图片壁纸就会变黑。被控端会把图片
存到 `%LOCALAPPDATA%` 下并保留最近 `keep` 张，不要手动去删这个目录。

---

## 七、故障排查

| 现象 | 原因 / 处理 |
|---|---|
| **重启/重新登录后没有自启** | 先跑 `WallpaperAgent.exe --status`，看「开机自启」那几行：`未设置`（跑 `enable_autostart.bat`）、`【已被禁用】`（任务管理器 → 启动里启用，或 `enable_autostart.bat /task`）、`【目标文件不存在】`（exe 被挪走，放回去重跑脚本）。另外注意：自启是**登录时**执行、免安装版的 HKCU 项只对写它的那个用户生效 |
| **通知发不出去 / 客户机没弹** | 正常情况下**不会**再出现：模块已经内置在 `WallpaperAgent.exe` 里，拷一个 exe 过去就能用。真出现 `本机没有 BurntToast 通知模块` 时，设备列表里会直接给出办法（有外网 `--install-toast-module`；没外网用 `prepare_toast_module.bat` 产出的 `BurntToast\` 放到 exe 旁边，或 `--install-toast-module --source D:\BurntToast`）。被控端启动后 8 秒和每次发通知前都会自己补一次。另外 `toast_enabled=false` 会关掉通知、`toast_auto_install=false` 会关掉自动装 |
| **通知显示成「Windows PowerShell」** | 说明通知身份没注册上（老版本、或注册被清掉了）。跑一次 `WallpaperAgent.exe --test-toast` 或 `--install` 会重新注册；确认 `--status` 里「通知身份：已注册为「Win 壁纸推送」」 |
| **通知里出现一行自己没写过的「处理中」** | 老版本的行为：进度条没写「状态文字」时程序会偷偷补一句「处理中」。现在改成**直接报错**（`进度条要写一句状态文字…`），不会再凭空造内容。控制端编辑器高级区里那两格是「进度」（状态文字，必填）+「进度标题」（可空），两个都空 = 不显示进度条 |
| **通知一闪就没了 / 想让它在屏幕上多停会儿** | 「通知」页的 **停留** 选「长（约 25 秒）」，或选「一直显示到用户处理」（循环响铃、要用户点掉）。要自己验就 `WallpaperAgent.exe --test-toast --duration long`（对照 `--duration short`）。注意 Windows 只给短/长两档，没有"自定义秒数"；另外 **停留**（横幅）和 **过期**（通知中心留多久）是两回事 |
| **通知上显示的名字想改成自己的（比如「IT 运维通知」）** | 批量改：控制端「设置」页 → 被控端通知应用名 → 填好 → 「下发到被控端…」（或 `WallpaperController.exe --set-app-name "IT 运维通知"`），被控端会写进自己的配置，重启仍生效。只想单台改：被控端 `--set-toast-app-name`，或面板「通知应用名」的「改名…」。只想每条消息带落款：用「通知」页高级选项里的**署名**（跟消息走，不改机器设置）。被控端若不接受远程改名，回执里会说 `本机禁止远程改通知应用名（allow_remote_app_name=false）` |
| **通知的标题/正文两边出现 `{}`** | 老版本的真 bug，已修。根因在 BurntToast 1.1.0 自带的 Toolkit 7.1.0：`New-BTText` 会把每行文字**包进大括号**（字面的大括号还会翻倍，`{already}` → `{{already}}`），Windows 就原样把 `{}` 显示出来。被控端的 `toast.ps1` 现在会把文字按原文写回 XML（`PS_SCRIPT_VERSION` 已从 5 提到 6，升级后第一次运行会自动重写脚本）。自检：`python test_toast.py` 里的 `[2i] 通知文字不带大括号` —— 连"用户自己写的大括号要原样保留"都测了 |
| **「局域网设备」列表里只显示一台，但日志里一堆设备** | 老版本按**计算机名**归并设备，而克隆镜像 / 同批装机的客户机经常同名 —— 50 台被合并成 1 行。现在改成按 **IP** 归并：同名不同 IP 分开显示、回执也分开统计（顶部的「在线 N 台 / 共发现 M 台」才是真实数字）；同名很多时那一行会注明「N 个计算机名重名，已按 IP 分开显示」。只有"同一台机器从 127.0.0.1 和局域网 IP 各报一次"仍并成一条 |
| **探测的设备不够 / 有的机器压根没出现** | ① 先确认「设置」页的 **深度扫描** 是勾上的 —— 关掉就只发广播，日志里会明确警告「深度扫描已关闭…设备会扫不全」（不少交换机 / 无线 AP 拦广播）；② 跨网段就填「额外网段/IP」（见上面第 3 步）；③ 用 `WallpaperController.exe --selftest` 看会探测多少个地址 |
| **48 台在线，但有一批没回执 / 壁纸没变** | 控制端现在会**自己诊断**并写进日志，照着看就行：<br>① `补发 3 轮后仍有 N 台没回执：10.x.x.x、…` —— 点名；<br>② 再探一次这些机器，分成两类结论：<br>　• `其中 M 台能回应扫描：网络是通的，它们多半没收到公告、或者下载壁纸失败` → 去被控端看 `%LOCALAPPDATA%\WinWallpaperPush\agent.log`，并确认**控制端**入站 TCP 38572 已放行（`add_firewall_rules.bat`）；<br>③ `另有 K 台连扫描也不回应：我这边的广播/单播到不了它们` → 常见是**无线 AP 客户端隔离 / VLAN 隔离 / 那台机器防火墙挡了 UDP 38571**。注意这类网络常是**单向**的：它们的主动报到能到控制端（所以列表里看得到 48 台），但控制端发给它们的包被拦掉 —— 这种只能在那个网段里再放一个控制端，或让网络放行<br>另外控制端的 UDP 收包缓冲已从默认 64 KB 提到 2 MB（几十台同时回执时不再因为缓冲溢出丢回执） |
| **换了控制端但客户端还是老版本** | 可以：协议没变。上面这些改进（设备列表按 IP、单播投递与补发、额外网段、探测范围）**都在控制端**，客户端不用动。唯一需要客户端升级的是之前那几条通知相关的修复（大括号、快捷方式 AppId、停留时长） |
| **下发改名显示「成功」，但客户机上名字没变** | 老版本的真 bug，已修（两个）：① 建开始菜单快捷方式用的是 BurntToast 的 `New-BTShortcut`，它内部要 `Import-Module BurntToast`；而客户机上的模块是我们打进 exe、临时释放的，**不在系统模块路径里** → 导入失败 → 快捷方式建不出来 → `System.AppUserModel.ID` 关联不上 → 改名在屏幕上不生效；② 这种情况下 `ensure_app_id()` 还**返回成功**，于是控制端报"成功 1 · 失败 0"，把人误导了。现在：快捷方式改用系统自带的 `WScript.Shell` **自己建（零依赖）**，再用 `IPropertyStore` 写入 `System.AppUserModel.ID` 并**读回校验**，校验不过就**如实回报失败**（回执里带原因，设备列表看得到）。升级后客户机第一次启动会自动重建快捷方式 |
| **改了名但通知上还是旧名字 / 改了没效果** | 见上一条（快捷方式没带 AppId）。自检三处：① `WallpaperAgent.exe --status` 显示「快捷方式已带 AppId」；② `Get-StartApps` 里能看到 `名称="你的名字" AppID="WinWallpaperPush.Agent"`；③ 开始菜单里只有一个我们建的快捷方式。注意客户机上必须跑**新版本 exe 并重启被控端**（老进程里还是老代码） |
| **改名后屏幕上还是旧名字（刚改完那一刻）** | 名字/图标是 shell 缓存的：程序改完会发一次 `SHCNE_ASSOCCHANGED` 让资源管理器刷新；要是还不变，注销重登一次。另外已经躺在通知中心里的旧通知不会改名，看**新弹**的那条 |
| **通知不显眼 / 没穿透专注助手（勿扰）** | 查 `WallpaperAgent.exe --status` 里的「通知优先级」：显示 `最高（AllowUrgentNotifications=1）` 才对。不是的话用 `--allow-urgent on`，或在被控端面板上勾「设为最高（允许紧急通知）」，或去系统「设置 → 系统 → 通知 → Win 壁纸推送」把「允许紧急通知」打开。另外控制端发通知时要勾上「紧急」（`scenario=urgent`）才会走这条路 |
| **不想让它抢优先级了** | `WallpaperAgent.exe --allow-urgent off`（面板上取消勾选也行）。程序只会在第一次运行时设一次，之后不会偷偷改回来 |
| 通知弹出来了但没声音 / 被专注助手挡了 | 声音要在编辑器里选（默认 Default）；要穿透专注助手的通知请勾「紧急」（`-Urgent`），它会被标成「重要通知」 |
| 通知里的图片没显示 | 图片只支持 png/jpg/jpeg/gif，单张上限 4 MB；控制端日志会写「这些图片没找到…」，被控端日志会写「图片没拿到，这条通知不带…」 |
| **第一次运行面板打不开 / 一直提示「请重新运行一次设置密码」** | 老版本的死循环：被控端平时是**隐藏**着跑的，而密码输入框以前挂在隐藏窗口下（`transient`），Windows 上这种框不会显示出来 —— 用户看不到输入框，等 300 秒超时被判成"取消"，于是提示"请重新运行一次"，重跑一次还是同一个看不见的框；壁纸和通知却一切正常。现在已修：① 密码框不再给隐藏窗口当子窗口，一定可见；② 没设过密码时面板不再被锁死（点取消也放行，并提醒风险），随时能从面板上的「修改密码…」或命令行补上：`WallpaperAgent.exe --set-password` |
| **忘了面板密码** | 密码只是哈希、找不回来，只能清掉重设：删掉 `C:\ProgramData\WinWallpaperPush\secure\agent_password.json`（管理员）、配置里的 `password` 段、注册表 `HKCU\...\WinWallpaperPush\Agent\PasswordHash` 三处，然后重新运行 `--set-password` |
| 面板/停止/卸载提示要密码，但没设过 | 说明有人设过（或配置/注册表里还留着哈希）。跑 `--status` 看「面板密码」那行和来源；确认要清掉就按上一行删三处 |
| 客户机用户抱怨「打不开面板」 | 这是设计如此：面板、停止、卸载都要密码，密码给管理员，别给终端用户。想临时放开就 `--set-password --clear`（需要旧密码） |
| 扫描不到任何设备 | ① 被控端没运行（`WallpaperAgent.exe --status` 一看就知道）；② 被控端防火墙没放行 UDP 38571；③ 两台机器不在同一网段 / 交换机做了 VLAN 隔离 |
| **扫不到某个网段（比如有线那个 10.127.112.0）** | ① 先看「设置」页最上面的网卡列表 —— 有线网卡如果是**已断开 / 没拿到地址**，这里会写出来（网线没插、网卡被禁用、没 DHCP 到地址都会这样）；② 控制端和被控端不在同一网段时，广播过不去：在「设置」页的 **额外网段/IP** 里填 `10.127.112.0/24`（或 `10.127.112.255`）再点「应用」，它会定向广播 + 逐台探测这 254 个地址；③ 大网段（/16 之类）默认只扫本机那 254 个地址，要扫别的段就按 ② 显式填进来；④ 用 `WallpaperController.exe --selftest` 可以把「网卡 / 广播目标 / 会探测多少个地址」全打出来 |
| **设备多的时候总有几台没改** | 已修（现在是三层投递）：① 广播（每张网卡各一份、重复 3 遍）；② **直接单播给已知设备**（绕开被交换机/AP 拦掉的广播）；③ **没回执的自动补发**最多 3 轮，日志里有「第 2 轮补发：N 台还没回执」。还是漏的话看设备列表：状态停在「待确认」说明它收到了公告但连不上 TCP 38572（控制端防火墙），「失败」会写明原因 |
| 跨网段推不到 | 广播不能跨路由。在被控端所在网段找一台机器跑控制端，或在「设置」页「额外网段/IP」里填目标网段（`192.168.2.0/24`）—— 工具会定向广播 + 逐台单播探测 |
| **MSI 装了没反应，exe 版本却正常** | 按顺序查：① `WallpaperAgent.exe --status` —— 没在跑就先启动（MSI 装完要**等下次登录**才自启，或用 `install_agent.bat` 立即启动）；② 看 `%LOCALAPPDATA%\WinWallpaperPush\agent.log`，静默后台时所有线索都在这；③ 确认入站 UDP 38571 已放行：`netsh advfirewall firewall show rule name=all dir=in \| findstr 38571`（旧版 MSI 的规则绑定在 exe 路径上，路径一变就静默失配，现在改成端口级了） |
| **静默后台跑的关不掉** | `WallpaperAgent.exe --stop`（免安装版用 `stop_agent.bat`；MSI 装在开始菜单「壁纸接收端控制台」里，或「显示壁纸接收端窗口」叫出来后点退出）。`--stop` 只停当次运行，自启项还在 |
| 不知道后台到底有没有在跑 | `WallpaperAgent.exe --status`：在跑返回 0，没跑返回 1，顺带打印端口 / 配置 / 日志路径 |
| 一台机器上出现多个被控端进程 | 正常情况只有一个：第二个实例会自己退出（自启项 + 双击 + 快捷方式同时触发也一样）。任务管理器里看到**两个** `WallpaperAgent.exe` 是单文件打包的父子进程，属正常现象，用 `taskkill /T` 才能一起结束。真要是「免安装版」和「MSI」同时装在一台机器上（两份自启项指向两个不同的 exe），建议只留一种：卸载 MSI 或删掉 HKCU 自启项 |
| 只扫到 WiFi 上的机器，有线的一台都没有 | 控制端有多张网卡（笔记本插网线 + WiFi）时，受限广播只会走默认路由那张。当前版本会给**每张网卡各发一份**定向广播，不会再漏；先看控制端启动日志里「网卡 …」那几行，确认有线网卡的网段和广播地址是否正确 |
| 网段不是 /24（比如掩码 255.255.0.0） | 按网卡真实前缀长度算广播地址（`/16` → `x.y.255.255`），不需要手工填。要确认可以跑 `WallpaperController.exe --selftest`，或点界面上的「重新检测网卡」 |
| 广播被交换机 / AP 拦掉 | 保持「深度扫描（逐台单播）」勾选：单播 UDP 一般不会被拦。命令行加 `--sweep` 同理 |
| 客户机后开机，列表里看不到 | 被控端启动后会主动报到（默认每 60 秒一次），控制端也会自动重扫（默认 30 秒），等几秒即可。若仍无，检查控制端防火墙有没有放行 UDP 38573（主动报到发到这个端口） |
| 设备列表里同一台机器出现两行 | 正常情况下按计算机名归并；如果两台机器重名，就会合并成一行，改计算机名即可 |
| 扫描到了但推送后一直「待确认」 | 控制端防火墙没放行 TCP 38572，被控端连不上来下载 |
| 日志显示「下发失败」 | 同上，或用了 WiFi 客户端隔离（AP isolation） |
| 跨网段推不到 | 广播不能跨路由。在被控端所在网段找一台机器跑控制端，或在控制端「额外广播地址」里填目标网段的定向广播地址（如 `192.168.2.255`） |
| 界面底部日志面板不见了 | 现在日志**默认就是收起的**：点左下角「▸ 运行日志」展开；从老版本升上来的话，窗口尺寸会沿用上次记住的值（`win_w`/`win_h`），嫌小可以拉大，下次就按新尺寸开 |
| 找不到「发送通知」按钮了 | 新版把通知编辑器做成了「通知」页签（不再弹窗）：点顶部页签切过去就行，编辑时设备列表照样看得见 |
| 设备列表太窄 / 想让它更大 | 中间那条竖线可以拖动；布局整体要么「壁纸页宽一点」要么「设备列表宽一点」，自己拖到顺手 |
| 高级选项里的字段看不见 / 被挡住 | 已修：通知页的可用宽度只有 ~470px（右边是常驻的设备列表），老版本高级区是"两列网格"（需要 ~790px），右半边整片被推到可视区外面 —— 现在改成单列自适应排版，冒烟测试会逐个量控件的右边缘（`页面内容没有横向溢出`），出现溢出就报 FAIL |
| 壁纸页里的字段名和输入框没对齐 / 文字被切 | 不会了：冒烟测试会逐个数「文字比格子还宽」的控件（`clipped_texts`），出现就报 FAIL，所以这类问题在提交前就被拦住 |
| **改了配置文件却一点效果都没有** | 已修的一个真坑：配置文件存成「UTF-8 **带 BOM**」时，老版本读不出来会**静默**退回默认值，然后还把默认值写回文件（用户的设置就被覆盖了）。记事本选「UTF-8」和 Windows PowerShell 5.1 的 `Set-Content -Encoding utf8` 都会写出 BOM。现在两端都按 `utf-8-sig` 读（带不带 BOM 都认），真读不出来时控制端会在日志里明说「配置文件读不出来，这次用的是默认设置」 |
| 壁纸没变 | 看被控端日志（界面上、或 `agent.log`）的具体错误；远程桌面会话下壁纸设置可能不生效，需在本地会话验证 |
| **远程命令：一台都连不上 / 全是 `Permission denied`** | 这是 SSH 自己的事，和壁纸广播无关。按顺序查：① 被控端有没有装 **OpenSSH 服务端**并启动（`Get-Service sshd`）；② 那把**公钥**在不在目标账号的 `C:\Users\<用户>\.ssh\authorized_keys` 里（`Permission denied` 十有八九是这条）；③ 被控端防火墙有没有放行 **TCP 22**；④ 用户名对不对（`Whoami`）。界面上「远端回显」那一列写的就是 ssh 的原话，照着它查最快 |
| **远程命令：点「发送到设备」立刻报「本机没有 ssh 客户端」** | 控制端这台机器缺 OpenSSH **客户端**（Windows 的「可选功能」，不是默认装的）：设置 → 应用 → 可选功能 → 添加功能 → **OpenSSH 客户端**。装好后界面上那行「ssh 客户端：…」会变成绿色路径，再点执行 |
| **远程命令：提示「远端要输密码」** | 公钥登录没生效（`authorized_keys` 里没有这把公钥，或权限/换行格式不对）。程序不会去猜密码 —— 一看到密码提示就判失败并把这句话写进结果里。注意：`authorized_keys` 里一行一把公钥，文件末尾要有换行 |
| **远程命令：命令卡住 / 一直「执行中」** | 单条命令有超时（默认 25 秒，界面可改），到点会判「超时」并继续下一台，不会永远等着。真遇到某条命令本身要交互（例如 `pause`、`diskpart`），会话式下它会吃掉后面那行结束标记 —— 结果就是这条命令超时，把它去掉即可 |
| **远程命令：`shutdown /r /t 0` 显示「📤 已下发」而不是「✅ 成功」** | 这是对的：机器重启会把 SSH 会话切断，此时拿不到回显，程序不会假装成功、也不会当成失败。重启类命令（`shutdown /r` `/s`、`Restart-Computer`、`logoff`）会话断了就算「已下发」 |
| **远程命令：想确认它到底执行了什么命令** | 「登录设置」那张卡片底下那行灰字就是**当前设置拼出来的完整命令**（`ssh -i "私钥" 参数 用户名@IP "命令"`），和人工在 cmd 里敲的一模一样；执行前的确认框里也会再列一次。要留档就点「导出结果…」（含每台的每一条命令和完整回显） |
| 提示 `invalid command name ...poll` | 旧版本遗留，当前代码已修复 |
| 想确认端口有没有被占 | `netstat -ano \| findstr "38571 38572 38573"` |

日志位置：两个程序界面上都有实时日志面板（控制端默认收起，点一下展开）；命令行模式直接打印到终端。

---

## 八、项目结构

```
win壁纸软件/
├── controller.py              控制端主程序（界面 + 命令行）
├── agent.py                   被控端主程序（界面 + 后台静默）
├── protocol.py                共享网络协议定义（端口、消息格式、壁纸样式与别名）
├── netutil.py                 收发工具、网卡枚举（有线/无线）、广播地址计算、日志文件、配置路径解析
├── winipc.py                  被控端控制通道（命名事件：单实例 / 停止 / 叫出窗口）
├── agentauth.py               被控端面板口令（加盐哈希、多位置存放、闸门与提示）
├── toastspec.py               通知规格与校验（两端共用；含「拒绝远程脚本」等安全规则）
├── toast.py                   被控端通知渲染（探测内置/外置模块、装模块、toast.ps1）
├── toastui.py                 控制端「发送通知」界面（简单模式 + 高级选项 + 预设 + 预览）
├── sshcmd.py                  远程命令引擎（走系统 ssh.exe：会话式/逐条独立、回显证据、并发、批处理生成）
├── sshui.py                   控制端「远程命令」界面（目标选择 + 登录设置 + 命令框 + 结果表）
├── wallpaper.py               Win32 壁纸接口封装（核心：SystemParametersInfoW）
├── ui.py                      两个界面共用的深色主题与日志控件
├── test_loopback.py           端到端回环自测（会临时换壁纸并自动还原）
├── test_scan.py               网络发现自测（网卡枚举 / 广播 / 单播 / 主动报到，不动壁纸）
├── test_sshcmd.py             远程命令自测（用**假 ssh** 跑完整链路：成功/失败/超时/GBK/重启/并发/停止）
├── test_agent_control.py      启停 / 自启 / 一键安装自测（真起进程、真读写注册表后恢复）
├── test_agent_auth.py         面板口令自测（哈希 / 存放优先级 / 闸门不卡死 / 端到端拦截）
├── test_toast.py              通知自测（规格校验 / 危险规格拒绝 / 真实弹窗 + 图片下发）
├── test_gui_smoke.py          界面冒烟测试（开窗跑几帧后自动关闭，含布局裁切检查）
├── requirements.txt           打包依赖（pyinstaller、pillow）
│
├── build.bat                  打包两个 exe
├── build_msi.bat              打包被控端 MSI（自动获取 WiX 工具链）
├── install_agent.bat          一键无人值守安装（提权 + 静默安装 + 补防火墙 + 立即启动 + 验证）
├── uninstall_agent.bat        静默卸载 MSI（自动 UAC 提权）
├── prepare_toast_module.bat   【打包用】取回 BurntToast 模块并裁剪到约 1 MB（供 build.bat 打进 exe）
├── BurntToast\                【模块源】prepare 脚本产出 / build.bat 打进 exe；放 exe 旁边则覆盖内置的那份
├── start_agent.bat            免安装版：静默启动 + 打印状态
├── stop_agent.bat             免安装版：停止后台运行（多会话时提示用管理员）
├── push_wallpaper.bat         命令行推送包装（自动处理退出码，可用于计划任务）
├── add_firewall_rules.bat     给「免安装版 exe」添加防火墙规则（需管理员）
├── remove_firewall_rules.bat
├── enable_autostart.bat       给「免安装版 exe」设置开机自启
├── disable_autostart.bat
│
└── installer/                 MSI 安装包工程
    ├── Agent.wxs              WiX 源文件（安装逻辑与设计注释都在里面）
    ├── agent_config.default.json  安装后放到 ProgramData 的默认配置
    ├── agent_console.bat      装到 Program Files 的文字控制台（状态 / 停止 / 看日志）
    ├── README-AGENT.md        随 MSI 装到 Program Files 的现场说明
    ├── License.txt            许可协议正文（要改就改这个）
    ├── make_license_rtf.py    把 License.txt 转成 RTF（中文用 \uNNNN? 转义）
    ├── get_wix.py             从 NuGet 下载 WiX v3.14 工具链（支持 --url 指向内网镜像）
    └── validate_msi.ps1       静态校验 MSI 各表（无需管理员、不实际安装）
```

> 两个部署路径二选一即可：
> **免安装版**（拷 exe + 跑 `add_firewall_rules.bat` / `enable_autostart.bat`）
> 适合几台机器；**MSI 版**适合 GPO / SCCM 批量下发。两者不要在同一台机器上混用。

打包产物：

```
dist/
├── WallpaperController.exe   约 18 MB
├── WallpaperAgent.exe        约 18 MB
└── WallpaperAgent.msi        约 18 MB   （被控端无人值守安装包）
```

> 所有 `.bat` 脚本里的提示文字都是纯英文。这不是偷懒 —— cmd.exe 按**字节偏移**
> 重读批处理文件，UTF-8 多字节字符（哪怕只是 `rem` 注释里的中文）会让它落到
> 字符中间，把后续行开头的字符吃掉，整段命令就废了。中文说明集中在本文档。

### 换壁纸的实现

```python
# wallpaper.py
ctypes.windll.user32.SystemParametersInfoW(20, 0, path, 0x01 | 0x02)
#                                         │  │  │      └ SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
#                                         │  │  └ 图片绝对路径
#                                         │  └ uiParam (0)
#                                         └ SPI_SETDESKWALLPAPER
```

同时写两个注册表值控制显示效果：

```
HKCU\Control Panel\Desktop
    WallpaperStyle  "10"=填充  "6"=适应  "2"=拉伸  "0"=居中/平铺  "22"=跨区
    TileWallpaper   "1"=平铺   "0"=不平铺
```

**一个容易踩的细节：** Windows 发现传入的壁纸路径字符串没变时会跳过刷新桌面。
所以被控端每次都把图片存成带 `task_id` 的唯一文件名，保证一定重新绘制。

---

## 九、运行测试

```bat
python test_loopback.py       :: 19 项端到端检查，结束时自动还原你原来的壁纸
python test_scan.py           :: 46 项网络发现检查（不动壁纸，随时可跑）
python test_sshcmd.py         :: 远程命令检查（用假 ssh 跑完整链路，不连任何真实机器：成功/拒绝访问/超时/GBK 中文回显/重启切断会话/首次连接答 yes/并发/中途停止/批处理纯 ASCII+CRLF/控制端命令行接线）
python test_agent_control.py  :: 61 项启停/自启/一键安装检查（真起进程、真读写注册表后恢复；含面板闸门两个方向）
python test_agent_auth.py     :: 51 项面板口令检查（哈希 / 存放优先级 / 闸门不卡死 / 面板隐藏时对话框可见 / 没密码不锁死 / --set-password 真跑一遍 / 真机上拦截 --stop）
python test_toast.py          :: 89 项通知检查（规格校验 / 停留时长 / 署名与自定义名字 / 内置模块释放 / 通知优先级 / 危险规格被拒 / 真实弹窗 + 图片下发，会弹 1~2 条通知）
python test_gui_smoke.py      :: 界面会被真正创建出来跑几帧再关闭，并检查有没有控件被裁掉
```

> 连着一个个跑时，中间**隔几秒**再跑下一个：`test_loopback` / `test_toast` 会真起被控端进程，
> 上一个还没退出干净时，`test_agent_control` 的「单实例」用例会读到上一个实例留下的状态
> （表现为"监听端口/日志文件"那两行对不上）。单独跑永远是准的。

`test_loopback.py` 在同一个进程里同时跑起控制端和被控端，验证完整链路：
广播 → 下载 → sha256 校验 → 调用 Win32 接口 → 注册表确认 → 回报控制端，
并在结束后恢复测试前的壁纸与契合度。

`test_scan.py` 专门验证「扫不到客户机」这一类问题，不需要第二台机器：
网卡枚举与有线/无线分类、`/16` 等非 /24 网段的广播地址、单播探测范围、
每个目标从哪张网卡发出（用假套接字断言，不真的发包）、被控端对单播 ping
的应答、以及「被控端主动报到 + 控制端深度扫描」的端到端发现。

`test_agent_control.py` 验证「静默后台关不掉」这一类问题：真的起一个被控端进程，
断言静默模式下窗口是隐藏的、重复启动不会跑出第二个、`--show` 能把窗口叫出来、
`--stop` 能让进程干净退出并清掉状态文件、日志文件里留下了完整的启动和退出记录。

`test_agent_auth.py` 验证口令闸门：哈希与校验（错密码/坏数据一律不通过）、
口令存放位置的优先级（注册表镜像 > 配置文件，机器级最优先）、
「只删配置文件绕不过去」，以及最要紧的一条 —— **无人值守环境没有输入时必须拒绝
且不卡住**（实测 0.1 秒返回），最后真起一个带密码的被控端，验证不给密码停不掉、
给了密码才停得掉。

另外两条是踩过坑之后补的回归用例：

* **面板隐藏时密码对话框必须可见**：被控端平时是隐藏着跑的（静默后台/自启动），
  以前密码框挂在隐藏窗口下（`transient`），Windows 上根本不显示（实测
  `viewable=False`、尺寸 1×1），用户看不到输入框 → 300 秒超时算"取消" →
  永远设不上密码、面板永远打不开。现在会真的起一个隐藏窗口，确认对话框是可见的、
  有正常尺寸。
* **没设过密码时面板不能被锁死**：取消设置密码、或者机器上根本没有可见输入界面，
  面板照样放行并提醒风险（否则就是"没密码 → 面板打不开 → 没入口设密码"的死循环，
  用户只会一直看到"请再运行一次本程序"）。

`test_toast.py` 验证通知功能：规格校验（15 个危险/越界用例必须全部被拒，
包括远程脚本块、非 http 按钮、路径穿越、进度条没写状态文字）、BurntToast 探测与**内置模块释放**
（exe 内部 → 释放到本机模块目录 → 失败时退回临时目录；exe 旁边 / 配置指定的要能盖过内置）、
**render 必须把找到的模块路径显式传给 PowerShell**（回归用例：以前只传配置值，
于是离线目录和内置模块都没被用上，报"未能加载指定模块"）、
**通知优先级**（真读写 HKCU 里的 `AllowUrgentNotifications`，只设一次、能关掉，测完原样还原）、
**真弹一条并核对通知中心里那条（按我们自己的 AppId 查）**、端到端（控制端广播 →
被控端拉图片 → 弹通知 → 回执），以及被控端**不盲信网络**：伪造一条带危险按钮的通知，
必须被拒绝并回报原因。

---

## 十、安全与合规说明

* 本工具**只做两件事**：接收广播后更换桌面壁纸；接收广播后弹一条 Windows 通知。
  不含屏幕查看、键鼠控制、文件浏览、进程管理、按键记录等任何远程控制功能。
* 通知功能**不能执行远程代码**：BurntToast 的 `ActivatedAction` / `DismissedAction`
  （点通知/关通知时跑的 PowerShell 脚本）**被明确拒绝**，规格里带了就整条拒收；
  通知按钮只允许 `http`/`https` 链接（外加系统自带的 dismiss/snooze），
  所以点按钮最多打开一个网址，不会在客户机上执行命令。这些规则在两端各校验一次
  （控制端发送前、被控端渲染前），被控端不盲信网络来的规格。
* 被控端**界面可见、进程可见**，会在自己的窗口里实时打印收到的每一次推送，
  不隐藏窗口、不隐藏进程、不注入任何系统进程。
* 面板有口令保护（第一次运行设置），打开面板 / 停止 / 卸载都要密码；
  口令只存加盐哈希。**它是防手欠级别的**，不是安全边界：本机管理员或任何
  本地用户仍然可以结束进程、删除程序目录 —— 详见 §2.5 的说明。
* 开机自启写在 `HKCU\Software\...\Run` 下，名字 `WinWallpaperAgent`，
  在「任务管理器 → 启动」里可见可关，随时用 `disable_autostart.bat` 移除。
* 协议**没有任何鉴权**，同一局域网内任何人都能伪造广播推送壁纸。
  请只在自己管理的网络里使用；如需鉴权，可以在 `protocol.py` 里加一个
  共享密钥字段并在 `ControllerCore.push` / `AgentCore._handle_announce`
  里校验。
* 部署到别人的机器前请先取得对方同意。
