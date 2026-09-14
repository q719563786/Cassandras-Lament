# 变更记录

面向使用者的重要变更。每个版本的验收数据（测试结果、产物哈希、未覆盖范围）见 `docs/releases/`。

## 未发布（v1.0 之后）

**工程度量**

- 新增 `tools/coverage_baseline.py`：零依赖的覆盖率基线工具（用标准库 trace，不引入 coverage 包）。
  基线（269 项测试）：**总体 80.4%**，可执行行 6948 / 已执行 5589，
  低于 80% 的模块 8 个。覆盖最低的五个：`secret_store` 48.0%、`external_sources` 64.5%、
  `remote_ai` 66.2%、`application` 68.5%、`http_api` 71.1%。

**速度**

- 补 5 条热路径索引，消除大表全表扫描。在你的真实数据库（894 MB）上实测：
  通知去重 55.7 ms → 0.01 ms、条目归属簇 26.5 ms → 0.01 ms、
  按来源查条目 110.4 ms → 0.23 ms、远程预算计数 36.8 ms → 0.23 ms。
  另有 1 条候选索引实测只快 1.3 倍，主动放弃。
  代价：13.7 MB 空间，首次启动多等约 4.6 秒（仅一次）。

**数据体积**

- 新增「结论明细保留天数」（默认 180 天，可设 30–730），在设置页可调。
  过期事件簇的派生明细（个人利益影响、通知记录、研判任务、实体、簇成员）会被清理。
- **事件簇与研判永久保留**，这一点由数据库触发器强制执行、不可绕过；
  不可变预测账本与趋势快照同样不动。清理**不影响任何历史准确率统计**。
- 诚实说明：`judgments` 实测 281 MB（占全库约 31%、占每日增量约一半）受上述保护无法清理，
  因此本改动把日增从约 26 MB 压到约 14 MB 并收敛约 41% 的体积，
  但无法让总体积完全停止增长。彻底解决需要改动「判读不可变」这条产品承诺。
- 删行后 SQLite 会复用空闲页，文件不再增长；不会自动变小，需要时手动 VACUUM
  可回收（实测 894 MB → 866 MB）。

**清理**

- 移除 `/api/dashboard` 接口。它界面从未调用、无测试覆盖，却会把全部预测正文读出来
  再在 Python 里筛选，实测返回 **17.5 MB**；功能与界面在用的
  `/api/risk-dashboard`（141 KB）重叠，因此整体删除，含随之失效的 `SignalsService.high_alerts()`。
- 修正两处文档与实现不符：`retention.py` 声称有「趋势降采样」、设置页一处说明也照抄了
  这句话，但代码里并无实现，均已改正。

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
