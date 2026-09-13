# 远见 v1.0

远见是一个只在 Windows 本机运行的外部认知雷达。它持续读取公开信息，把同一事件的多篇报道合并，区分单源线索与多源证据，生成结构化判断，再在本机映射到个人利益和候选预测。

## 运行环境

- 操作系统：Windows 10 或 Windows 11（x64）。
- 界面运行时：**Microsoft Edge WebView2 Runtime 必须已安装**。Windows 11 和已更新的 Windows 10 通常自带；缺失或损坏时程序会明确报错，不会静默退回默认浏览器。
- 源码运行需要 Python 3.13 或更高版本（打包基线为 3.14）。
- 第三方依赖（打包脚本会自动安装，版本固定）：
  - `pyinstaller==6.21.0`
  - `pywebview==6.2.1`
  - `pystray==0.19.5`
  - `Pillow==12.3.0`
- 无需联网即可使用本地研判、利益映射和预测账本；外部 AI 是可选增强项，默认关闭。
- 运行数据统一存放在 `%LOCALAPPDATA%\YuanJian`，不在源码目录或安装目录内。

## 使用方式

详细操作步骤见 [`使用说明.md`](使用说明.md)。三种启动方式：

**一、使用已打包的程序（推荐给普通使用）**

双击 `YuanJian.exe`。这是 onedir 打包，`YuanJian.exe` 和同级的 `_internal` 目录必须放在一起，不能只拷 exe。

**二、从源码运行（开发调试）**

```powershell
pip install pywebview==6.2.1 pystray==0.19.5 Pillow==12.3.0
$env:PYTHONPATH='src'
python -m yuanjian_app.application
```

**三、免安装绿色包**

[`launcher/启动远见.cmd`](launcher/启动远见.cmd) 以 `pythonw` 无控制台窗口启动，要求目录结构为 `<包根>\app\src`，并把 `PYTHONPATH` 指向它。

## 核心能力

- 三组主导航：行动首页、告诉远见和设置。个人输入一步可达，原始新闻不占据一级入口。
- 首次打开自动显示三步教程；设置中可以随时重新打开。
- 行动首页只呈现最多 3 条个人风险，先给大致行动建议，再给原因、期限和高/中/低风险。
- “告诉远见”允许用一句话记录发生的事或自己的行为，保存后直接返回行动建议和中文风险级别。
- L1/L2、未完成研判和未命中个人利益的外部信息留在后台，不把分析工作转交给用户。
- 新闻、因果链、反证和来源链接默认折叠在“为什么这样判断”中。
- 情报后台默认只显示处理量、来源覆盖和待核实数量；原始新闻、来源和规则按需加载。
- 事件、外部信息和通知仍使用后端分页，避免一次渲染全部历史。
- 历史与新增 RSS/网页标题、摘要统一转为可见纯文本，脚本、样式、标签和 HTML 实体不会直接出现在界面。
- 通知支持单条已读、全部已读和打开关联风险。

- RSS/Atom、GDELT JSON、公开网页列表采集，失败状态和15/30/60分钟退避可见。
- 72小时事件聚类；中文、英文、数字、金额、比例和日期参与相似度计算。
- 独立域名与本地标记的官方来源形成 E1—E4 证据等级，同域转载不冒充互证。
- 6小时、24小时、7天、30天趋势；历史不足或样本少时不制造“升温”结论。
- 断网可用的本地研判，输出事实、参与者、因果链、不确定性、时间窗和反证触发器。
- 可选 OpenAI Responses API 严格结构化输出；默认关闭，无密钥、认证失败、限流、超时或非法输出时退回本地。
- 私人利益只在本机映射。E1 无论多重要都不得超过 L3。
- 候选预测必须人工选择固定概率后，才能进入不可变预测账本。
- 单实例后台、登录启动选项、运行状态、6小时通知节流和本地通知中心。
- pywebview 原生桌面窗口与系统托盘；关闭窗口即安全退出（停后台监控、释放端口、退出进程），托盘菜单也可选择显示、运行认知、暂停监控或退出。
- “立即更新判断”显示实时忙碌状态、耗时以及本次处理、研判、利益影响和提醒数量。
- 所有保存、刷新和反馈操作使用可见的忙碌、成功和失败状态，不再依赖阻塞式提示框。
- 只读索引 Obsidian；不写回原文章库。

## 安全边界

程序只监听 `127.0.0.1`。运行数据保存在 `%LOCALAPPDATA%\YuanJian`，不放在源码或安装目录。

程序界面在 Microsoft Edge WebView2 中显示，不会再用默认浏览器打开。只有点击外部公开证据链接时，才会打开系统浏览器。WebView2 缺失或损坏时程序会明确报错，不会静默退回浏览器。

外部 AI 最多接收8个公开来源和12,000字符，只包含公开标题、摘要、网址、域名、时间和通用类别。精确地址、生日、家庭关系、医疗、债务、账户、Obsidian原文、私人利益图和预测历史不得外发。API 密钥使用 Windows 当前用户 DPAPI 加密，不写数据库、日志或源码。

软件不会自动借贷、投资、发送外部消息，或替用户作医疗、法律决定。采集不绕过 TLS、登录、付费墙或反爬。

## 构建、测试与自检

**打包**

```powershell
powershell -ExecutionPolicy Bypass -File build\build_windows.ps1
```

脚本会自行创建 `.venv-build` 虚拟环境并安装上表列出的固定版本依赖，然后按 [`build/yuanjian.spec`](build/yuanjian.spec) 打包，入口为 [`build/windows_entry.py`](build/windows_entry.py)。产物输出到 `dist\YuanJian\`（onedir，约 42 MB，`YuanJian.exe` 约 6.4 MB）。

**打包烟测**

```powershell
powershell -ExecutionPolicy Bypass -File tools\smoke_packaged.ps1 -ExePath 'dist\YuanJian\YuanJian.exe'
```

**单元测试**

```powershell
$env:PYTHONPATH='src'
$env:PYTHONWARNINGS='error::ResourceWarning'
python -m unittest discover -s tests -v
```

测试要求 `ResourceWarning` 视为错误。当前 220 个测试全部通过。当前版本的验证记录见
[`docs/releases/YuanJian-v1.0-verification.md`](docs/releases/YuanJian-v1.0-verification.md)，
按时间线的变更摘要见 [`CHANGELOG.md`](CHANGELOG.md)。

**发布前隐私自检**

```powershell
python tools\privacy_scan.py
```

在提交或打包前运行，确认没有数据库、日志、备份、密钥或本机绝对路径进入公开交付物。规则见 [`PRIVACY.md`](PRIVACY.md)。

## 变更记录要点

- 2026-09-13：修复 E1 证据等级的上限。此前 `_alert_level()` 只按分数分档，E1 可以算到 L4；而 `map_judgment()` 会对 L4 候选自动确认，等于单一来源线索未经人工选择概率就写入不可变预测账本，同时从待确认列表消失。现已在 `impacts.py` 强制 E1 不得超过 L3，与本文档和 [`PRIVACY.md`](PRIVACY.md) 的声明一致。完整测试套件 207 项全部通过。
