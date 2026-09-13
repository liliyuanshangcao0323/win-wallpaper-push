# 贡献指南

感谢您对 Win 壁纸推送项目的关注！我们欢迎各种形式的贡献。

## 如何贡献

### 报告问题

1. 在 GitHub Issues 中搜索是否已有类似问题
2. 如果没有，请创建新的 Issue，包含：
   - 清晰的问题描述
   - 复现步骤
   - 期望行为与实际行为
   - 操作系统版本和 Python 版本

### 提交代码

1. Fork 本仓库
2. 创建你的特性分支：`git checkout -b feature/AmazingFeature`
3. 提交你的更改：`git commit -m 'Add some AmazingFeature'`
4. 推送到分支：`git push origin feature/AmazingFeature`
5. 创建一个 Pull Request

### 代码规范

- 遵循 PEP 8 代码风格
- 为新功能添加必要的文档
- 确保所有测试通过
- 添加相关测试用例

## 开发环境设置

```bash
# 克隆仓库
git clone https://github.com/your-username/WinWallpaperPush.git
cd WinWallpaperPush

# 创建虚拟环境
python -m venv venv
venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 运行测试
python -m pytest
```

## 项目结构

```
WinWallpaperPush/
├── agent.py              # 被控端主程序
├── controller.py         # 控制端主程序
├── wallpaper.py          # 壁纸设置接口
├── netutil.py            # 网络工具函数
├── protocol.py           # 通信协议定义
├── toast.py              # Windows 通知功能
├── toastspec.py          # 通知规范定义
├── toastui.py            # 通知界面
├── ui.py                 # 用户界面
├── winipc.py             # Windows IPC 通信
├── test_*.py             # 测试文件
├── installer/            # 安装程序相关
├── screenshots/          # 界面截图
├── BurntToast/           # 通知组件
└── README.md             # 项目说明
```

## 功能开发指南

### 新增壁纸样式

1. 在 `protocol.py` 的 `STYLES` 字典中添加新样式
2. 在 `wallpaper.py` 中添加对应的注册表设置
3. 在控制端界面中添加选项

### 新增网络功能

1. 遵循现有的 UDP/TCP 通信模式
2. 确保兼容性
3. 添加相应的测试用例

### 通知功能

1. 遵循 `toastspec.py` 中的规范
2. 确保在 Windows 10/11 上正常工作
3. 添加错误处理和日志记录

## 测试

```bash
# 运行所有测试
python -m pytest

# 运行特定测试
python test_agent_auth.py
python test_agent_control.py
python test_gui_smoke.py
```

## 文档

- 更新 README.md 添加新功能说明
- 为公共函数添加文档字符串
- 更新相关的用户指南

## 提交信息规范

使用以下格式：

```
<type>(<scope>): <subject>

<body>

<footer>
```

类型：
- feat: 新功能
- fix: 修复 bug
- docs: 文档更新
- style: 代码格式（不影响代码运行的变动）
- refactor: 重构（既不是修复 bug 也不是添加功能）
- test: 增加测试
- chore: 构建过程或辅助工具的变动

## 行为准则

- 尊重所有贡献者
- 接受建设性批评
- 专注于对社区最有利的事情
- 对其他社区成员表示同理心

## 许可证

贡献的代码将采用 MIT 许可证。
