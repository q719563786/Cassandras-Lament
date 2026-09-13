# 变更记录

面向使用者的重要变更。每个版本的验收数据（测试结果、产物哈希、未覆盖范围）见 `docs/releases/`。

## v1.0 · 2026-09-13

首个带完整验收记录的正式版本。验收数据见 [`docs/releases/YuanJian-v1.0-verification.md`](docs/releases/YuanJian-v1.0-verification.md)。

**安全边界修正**

- E1（单一来源线索）现在被强制限制在 L3 以内。此前 `_alert_level()` 只按分数分档，E1 可以算到 L4；而 L4 候选会被 `map_judgment()` 自动确认，等于未经人工选择概率就写入不可变预测账本，同时从待确认列表消失。这修正了代码与 `README.md`、`PRIVACY.md` 声明之间的偏差。

**隐私**

- 公开源码中不再包含本机绝对路径。公开历史已改写并重建仓库，旧提交哈希不再可用。
- `PRIVACY.md` 明确「本仓库是公开仓库」为既定前提，并补充禁止本机路径、用户名、主机名入库。
- 新增「会话令牌的边界」，写明该令牌防的是本机以外与浏览器跨源，不防本机其他程序。

**安全加固**

- 所有响应统一附带 `Content-Security-Policy`（含 `object-src` / `base-uri` / `form-action` / `frame-ancestors`）、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Cross-Origin-Opener-Policy`、`Cross-Origin-Resource-Policy`。
- 会话令牌改用定时安全比较，并在前端读到后立即从地址栏抹除。
- 隐私闸门 `tools/privacy_scan.py` 修正了真实漏过一次的盲点（只匹配反斜杠形式的绝对路径），新增 `--committed` 模式，发布检查变成一条命令。

**性能与健壮性**

- 聚类的文本特征提取加入缓存，不再对同一簇重复分词；候选簇查询收窄到实际使用的列。
- 调度器下次到期改从任务结束时刻起算，修复任务超时后连续补跑的问题。
- 任务失败除异常类型外同时记录清洗并脱敏后的消息，便于现场排障。
- 新增滚动日志文件（`%LOCALAPPDATA%\YuanJian\logs`）；此前打包后无控制台，日志无落点。

**工程流程**

- 新增 CI（`.github/workflows/tests.yml`）：push 与 PR 时运行完整测试套件与隐私闸门。
- 新增依赖清单 `requirements.txt` / `requirements-build.txt`，构建脚本改为读取它们，版本只有一处真相。
- 新增 `.gitattributes` 统一行尾。
- 测试从 207 项增加到 220 项。

## v0.9 · 2026-08-12

行动引导界面：三步首次教程、行动首页只呈现最多 3 条个人风险、「告诉远见」一句话输入。验收数据见 [`docs/releases/YuanJian-v0.9-verification.md`](docs/releases/YuanJian-v0.9-verification.md)。

## v0.8 · 2026-08-12

风险驾驶舱。验收数据见 [`docs/releases/YuanJian-v0.8-verification.md`](docs/releases/YuanJian-v0.8-verification.md)。

## v0.7 · 2026-08-12

界面重做。验收数据见 [`docs/releases/YuanJian-v0.7-verification.md`](docs/releases/YuanJian-v0.7-verification.md)。

## v0.6 · 2026-08-11

桌面外壳与 pywebview 原生窗口。验收数据见 [`docs/releases/YuanJian-v0.6-verification.md`](docs/releases/YuanJian-v0.6-verification.md)。

## v0.5 及更早

早期数据库 schema 迁移与本地研判骨架，无独立验收记录。schema 迁移版本见 `tools/migrate_private_v05.py`。
