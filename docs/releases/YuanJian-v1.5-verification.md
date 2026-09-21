# 卡珊德拉的哀歌（内部代号「远见」） v1.5.0 验证记录

**日期**：2026-09-21
**版本**：1.5.0（密教主题 + L4 定级接入方法论 + 全球态势图层 + GDELT 退避与保留期）
**分支 / 提交**：`mystique-theme` @ `c4d9f70`
（`4b28fc3` 主体 +4001/−160　→　`3ee51a6` 本文档　→　`c4d9f70` 态势调度顺序修复 +160/−5）
**补记（v1.5.1 批次）**：本文档另含 2.7 / 2.8 两节 —— 「单表 > 500 MB」护栏**静默失效**的修复
与两个解释器的全量回归。该批次落在 `50ceb37` 及之后的工作树上，**未 push、未发 GitHub Release**。
**依据**：用户「同一个 L4 一直重复弹」的质问 → 通知层修复（v1.4.2）→ 判级层整改（本版）

> 命名说明：本项目对外正式名为 **卡珊德拉的哀歌 / Cassandra's Lament**，内部代号「远见」（YuanJian）。
> 本文件按仓库既有习惯沿用 `YuanJian-` 前缀的文件名。

---

## 一、这一版做了什么

| 批次 | 改动 | 关键锚点 |
|------|------|----------|
| ① 密教主题 | 新增 `static/css/theme-mystique.css`（仅外观，逐字节保真交付件）；13 图标换 10、emoji 换几何符 | 不动任何行为 |
| ② L4 定级接入方法论 | L4 增**结构闸**（`delay_risk ∈ {高,中}` 或命中风险信号才放行，否则降 L3 并记 `l4_downgraded_by`）；`importance` 由「预设 × 事件侧结构强度」决定（不再恒为 0.6）；远程研判缺 `power_structure` 时**本机读时补算**（标 `structure_source`）；存量 119,331 行一次性回填（迁移 v7） | `_has_structural_signal` / `_resolve_power_structure` |
| ③ 全球态势图层 | 新视图「全球态势」`#/atlas`；三源 USGS/EONET/GDACS（`geojson` 抓取类型，`config_json` 驱动零硬编码）；**独立表 `situation_events`（迁移 v8）**，与研判链物理隔离 | `parse_geojson` / `/api/situation/*` |
| ③′ 态势调度顺序修复 | 冷启动时 `refresh_due_sources` 要串行补抓 30+ 条常规源（真机 15~30 分钟），后台循环单线程按书写顺序串行，态势块原排在其后 → **整个补抓窗口里态势一次都轮不到**（三源恒 `never`、`situation_events` 恒 0、地图整片空白）。修法：把态势检查**提到采集之前**（`c4d9f70`） | `RadarScheduler._run` 循环体顺序 |
| ④ 退避 + 保留期 | 抓取失败退避 `15/30/60/120/240/360`（成功归零、封顶 360、不自动停用）；GDELT 预置周期 15→120；态势保留期 30 天接进 `RetentionService`（F 层） | `_failure_backoff_minutes` / `SITUATION_KEEP_DAYS` |
| ⑤（v1.5.1）单表护栏修复 | 交付环境无 `dbstat` → 「任一表 > 500 MB」分支**静默失效**。改为 `dbstat` 精确优先、缺失时用「沿 rowid 均匀开窗」估算并**如实标注来源**；退化记 warning，诊断面板新增 `table_size_source`；用例改为打桩覆盖两路径（不再与解释器耦合）。**主护栏 `db_bytes` vs 2048 MB 未动** | `_largest_table_bytes` / `_estimate_largest_table` |

**schema**：v7（存量 `personal_impacts` 定级回填，用户真库在升级前一日已跑完）+ v8（态势独立表，首次启动建表，纯建表建索引）。

---

## 二、验收数据

### 2.1 测试

主体三轮（提交 `4b28fc3` 的树）：

```
第 1 轮   Ran 723 tests in 180.4s   OK (skipped=3)     exit 0
第 2 轮   Ran 723 tests in 184.0s   FAILED (errors=1, skipped=3)   exit 1
第 3 轮   Ran 723 tests in 176.9s   OK (skipped=3)     exit 0
第 4 轮（补跑） Ran 723 tests in 183.7s   OK (skipped=3)   exit 0
```

- 环境：`PYTHONPATH=src`，`PYTHONWARNINGS=error::ResourceWarning`，`no_proxy=127.0.0.1,localhost`。
- **第 2 轮那 1 个 error 是环境抖动，不是产品缺陷**：`test_http_api.test_feedback_stays_in_local_personal_impacts`
  在 `sock.connect` 抛 `PermissionError: [WinError 10013]`（本机回环 socket 瞬时被拒），
  与产品代码无关。**单独复跑同一用例 8 次，8/8 全过**（`build-artifacts/qa_flake_probe_150.py`），
  第 1/3/4 轮也全绿。

调度顺序修复后（提交 `c4d9f70` 的树，新增 2 例）复跑两轮：

```
第 1 轮   Ran 725 tests in 181.7s   OK (skipped=3)     exit 0
第 2 轮   Ran 725 tests in 185.1s   OK (skipped=3)     exit 0
```

- 新增测试模块 2 个、共 37 项：

| 模块 | 项数 | 覆盖 |
|------|------|------|
| `tests/test_situation_layers.py` | 19 | GeoJSON 解析（USGS/EONET/GDACS 夹具、跳脏数据）＋幂等 upsert＋只读接口契约＋参数越界 400＋令牌强制；**`SituationSchedulerChainTests` 2 例**：驱动 `run_situation_once` 整条链路 + 用虚拟 monotonic 把一次采集拉长到 30 分钟、钉住「态势先于采集」顺序契约（`test_radar_scheduler.py` 此前对态势零断言，正是这个盲区放跑了调度故障） |
| `tests/test_atlas_view.py` | 18 | atlas.js **node 真跑**（投影/反投影/命中测试/量级口径/新鲜度）＋真读底图（127 多边形）＋`render()` 记录型 canvas 对账＋CSS 类契约（53 类齐全） |

> 上表两轮的 `Ran 725 … OK` 是在**另一套（编译了 `dbstat` 的）SQLite** 上测的，本机不可复现；
> 根因与修复见 **2.7 / 2.8**，以及三-7 的 `c4d9f70` vs `50ceb37` A/B 对照。
> 修复后：**两个解释器各 763 项全绿（skipped=3）**。

### 2.2 变异对照（证明断言有牙）

`build-artifacts/lead_verify_v15_e2e.py`（在临时库上、同一进程内改坏再跑）：

```
【默认】分数够 L4（score=1.0）但无结构性抓手 → alert=L3，l4_downgraded_by=no_structural_signal
【变异】把 _has_structural_signal 强制返回 True → 同一条事件变回 L4
```

同一脚本另含：`delay=高 → L4 / delay=低 → L3 / 有风险信号 → L4`；
退避序列 `[15,30,60,120,240,360]` 与落库实测（连续失败 2 次 → 重试间隔 30 分钟，成功即归零）；
GDELT 预置同步「只动 `user_managed=0`、用户改过的行一个字节不动」；保留期「窗口外删、窗口内留」；
**调度顺序**（场景 6）：把一次采集拉长到 30 分钟，实测调度顺序 `order = ['situation','external']` —— 态势先被服务、且这一趟真的落库（4 行）。

### 2.3 三层验证（全部在**已安装的产物**上跑）

| 层 | 脚本 | 结果 |
|----|------|------|
| 内容级（解 exe 里的 PYZ + 读打包静态资源） | `build-artifacts/lead_verify_installed_v15.py` | 34 个 Python 标识符全在（含 4 个批次 + `run_situation_once`/`run_external_once`）＋ 14 项静态资源断言全 OK；主题 CSS 与底图 GeoJSON **逐字节一致** |
| 启动烟测（无窗口真跑，只读接口） | `build-artifacts/lead_smoke_v15.py` | 版本自报 1.5.0；态势 `/layers`、`/points`、`/geo/ne_110m_land.geojson`（200 / `application/geo+json` / 127 多边形）；5 个越界参数全 400；v8 建表、v7 未重跑；跑完 `POST /api/shutdown` 正常退出。**冷启动实测：态势图层 6 个、612 个事件点（`wildfire` 314 / `quake` 243 / `flood` 23 / `storm` 19 / `drought` 7 / `volcano` 6），`/points` 24h 内 216 条** —— 修复前此处恒为 0。**烟测唯一未过项：`/api/risk-dashboard` 冷启动超时**（既有行为，见三-6；写突发结束后实测该接口 2.9–4.7s 内 200，与冷启动同一次会话内） |
| 行为级（临时库 + 从库里读回 + 变异对照 + 调度顺序） | `build-artifacts/lead_verify_v15_e2e.py` | 39 项全过（见 2.2） |

### 2.4 发布闸门

```
tools/privacy_scan.py --committed   →   committed_files=150  safe=True  blocked=0  findings=0  (exit 0)
```

### 2.5 构建与安装

| 项 | 值 |
|----|-----|
| 产物 | `dist/YuanJian/YuanJian.exe`（onedir），6,940,643 B |
| exe sha256 | `5DC512B4906831181E93A8228488D030BD97DDD23AED972EEE8D0EFE2A960ADF` |
| 主题 CSS | `17DA4778…A44943`（53519 B，交付件逐字节一致） |
| 底图 GeoJSON | `9E0729EE…2835D9`（138160 B，交付件逐字节一致） |
| 安装位置 | `D:\远见\程序` |
| 回滚点 | `D:\远见\程序-旧-20260921-145135`（v1.4.x，`CFDB9ECF…`）与 `…-154755`（首个 v1.5.0 构建，`2754BBFD…`），均保留不删 |
| `data-dir.txt` | `D:/远见/数据`（升级后保留） |
| 首次启动耗时 | **4.76 s**（进程拉起 → 版本接口可用；含 v8 建表） |

### 2.6 真实数据未被写入

对照基准 = 用户升级前备份 `yuanjian-20260920T114041Z.db`（`build-artifacts/qa_postinstall_150.py`，连 WAL 一起读）：

| 项 | 升级前（备份） | 装后（现库） | 一致 |
|----|----------------|--------------|------|
| `forecasts` | 8707 | 8707 | ✓ |
| `resolutions` | 0 | 0 | ✓ |
| `forecast_versions` | 19370 | 19370 | ✓ |
| `personal_impacts` | 119331 | 119331 | ✓ |
| `resolved_by != 'unknown'` | 0 | 0 | ✓ |

- 现库 `schema_migrations = [1,2,3,4,5,6,7,8]`：**v7 未重跑**（版本行仍只有一条），v8 已落库。
- 唯一写入是预期内的 v8 迁移（建 `situation_events` 表 + 两索引 + 一条版本行）。
- `external_items` / `external_runs` 会随**常规源正常采集**增长（`+99` 等）—— 那是既有采集链的日常，不在本表账本比对范围内。
- `situation_events` 首次启动后即出现 612 行：见 2.3 —— 这是 ③′ 修复后的应有结果（修复前恒为 0）。

### 2.7 「单表 > 500 MB」护栏修复（护栏此前**静默失效**）

**问题**：`RetentionService._largest_table_bytes()` 依赖 SQLite 的 `dbstat` 虚表；该虚表需要
`SQLITE_ENABLE_DBSTAT_VTAB` 编译开关。**交付环境没有**（打包的 `_internal\sqlite3.dll` 3.50.4
不含该选项，`sqlite_dbpage` 同样没有），于是查询抛错被 `except sqlite3.Error` 吞掉、返回 `("",0)`，
「任一表 > 500 MB」这条分支**永远不会触发** —— 一个静默失效的护栏等于没有护栏。
它同时暴露成一条"与解释器耦合"的用例：换一台有 `dbstat` 的机器跑就绿、本机必红。

**实测候选来源**（`build-artifacts/qa_tablesize_probe.py` / `qa_estimate_probe.py` / `qa_bias_probe.py` / `qa_spread_probe.py` / `qa_scheme2_probe.py`，真库 ≈1.05 GB / 26 表）：

| 来源 | 可用性 | 耗时 | 误差 |
|------|--------|------|------|
| `dbstat` 虚表 | ✗ 无 | — | — |
| `sqlite_dbpage` 虚表 | ✗ 无 | — | — |
| `PRAGMA page_count`×`page_size` | ✓ | ~0 ms | 只给**整库**，给不了逐表 |
| `sqlite_master.rootpage` | ✓ | 0.2 ms | 只是 B 树根页号，读不了页数（无页读取通道） |
| 逐表 `COUNT(*)` | ✓ | 106 ms | 只排行数，不是字节 |
| 逐表 `COUNT(*)` × **前 N 行**平均行宽 | ✓ | ~2000 ms | 偏差 **0.32x ~ 5.55x** 且方向不定（`judgments` 低 3 倍、`external_items` 高 5 倍）→ 不可用 |
| 逐表 `COUNT(*)` × **全表**平均行宽 | ✓ | 0.8 ~ 16.6 s | 准（1.00x），但太贵 |
| 逐表 `COUNT(*)` × **沿 rowid 均匀开窗**平均行宽 | ✓ | 全库 ≈3.7 s | **0.97x ~ 1.11x**，合成库真值 −7.7%/−11.1%/−17.8%，真库对照 `judgments` 245.8 MB vs dbstat 时代实测 281 MB（−12.5%） |

**采用**：`dbstat` 精确优先；缺失时用「沿 rowid 均匀开窗」估算（24 窗 × 60 行，行宽取
`LENGTH(CAST(col AS BLOB))` 的**字节**——按字符会低估 CJK 60%+），并乘保守系数 `1.15`
（实测系统性低估 ≈12%，宁早触发）。该检查**每小时一次**，3.7 s 代价可接受。

**非静默**：退化时 `retention` 记一条 `warning`；`should_run_by_threshold()` 返回值新增
`largest_table_source`（`dbstat` / `estimate` / `unavailable`），并随
`radar_scheduler.run_retention_if_threshold()` 的结果一并带出；诊断面板新增 `table_size_source`
字段（`dbstat`=精确 / `estimate`=估算）—— 它是 ~1 ms 的**能力探测**（`SELECT 1 FROM dbstat`），
所以"护栏可不可信"任何时候都看得见，而不用等某次清理真的跑过。
连估算都失败时标 `unavailable` 并明确记 warning，**绝不静默返回 0 冒充"没有大表"**。
（对应用例：`tests/test_cleanup_layers.py::ThresholdTests` 三条路径、
`tests/test_retention_guards.py::ThresholdGuardVisibilityTests` 两条、
`tests/test_ops_capabilities.py::test_table_size_source_*` 两条。）

**用例去解释器耦合**（`tests/test_cleanup_layers.py::ThresholdTests`）：改为**打桩覆盖两条路径**，
各自断言明确行为，不再依赖本机是否编译了 `dbstat`、也不静默 skip：

- `test_table_size_rule_uses_dbstat_when_available`：打桩 `dbstat` → 来源 `dbstat`、reason 不含「估算值」；
- `test_table_size_rule_falls_back_to_estimate_without_dbstat`：打桩 `dbstat` 抛错 → 来源 `estimate`、
  **仍能触发该分支**、reason 含「估算值」、且必须留下 warning；
- `test_table_size_rule_reports_unavailable_when_nothing_works`：两路都抛错 → 来源 `unavailable`、
  不强行触发、必须记 warning。

**变异对照**（`build-artifacts/qa_mutation_dbstat.py`）：删掉「退化为估算」那支 → **3 条用例变红**
（`small_database` / `falls_back_to_estimate` / `reports_unavailable`），随后**逐字节还原**
（sha256 前后一致 `1aee46a1…f6244d`）。

**主护栏未动**：`db_bytes` vs `threshold_mb`（默认 2048 MB）逻辑与阈值一个字节没改。

### 2.8 两个解释器各跑一次全量

| 解释器 | 结果 |
|--------|------|
| `D:\远见\源码\.venv-build\Scripts\python.exe`（**打包基线**） | `Ran 767 tests in 214.1s` → `OK (skipped=3)`　exit 0 |
| `c:\python314\python.exe`（系统 / PATH） | `Ran 767 tests in 183.2s` → `OK (skipped=3)`　exit 0 |

原始输出（未删改）：`build-artifacts/qa-logs/full_A_venvbuild.log`、`full_B_python314.log`。
3 条 skip 均为**既有占位**（趋势 `sampling_shift` 护栏未落地、L3 硬编码夹具无法区分、另 1 条同属占位），
与本批无关。（本机四个解释器 —— `.venv-build` / `pythoncore-3.14-64` / `c:\python314` / `Python\bin` ——
实测**完全一致**：Python 3.14.5、SQLite 3.50.4、**均无 `dbstat`**；故两份输出同为全绿。
计数从 725 起：`c4d9f70` +2（态势调度）＝727，其后团队其他成员批次并入至 761，
本批把 1 条耦合用例拆成 3 条（+2）、再加护栏可见性 2 条与诊断能力探测 2 条（+4）＝**767**。）

---

## 三、已知局限（写下来，不假装不存在）

1. **回填口径偏乐观**（迁移 v7 的一次性选择，已记录）：
   - 历史行会因后续报道使证据等级上升而被重算，故 9/8–9/16 那几天的 L4 从个位数升到几十条（**只影响历史回看**）；
   - `personal_relevance` 缺失的 **7,346 行**按「最相关 1.0」处理。
2. **结构信号"未知"占 52.7%**：按中性（不罚）处理，因此这部分**进不了 L4**（信息不足不进最高档），也**不因缺失降分**。这是刻意的口径，不是漏算。
3. **态势层不做跨源同一事件合并**：`event_id` 带源前缀，不同源的同一场地震会各占一个点。地图层暂不需要去重。
4. **AARO / 天外实体源不可采**（记录）：`aaro.mil` 对机房出口 IP 返回 Akamai 403、脱该出口则在 DNS 层被拒，且 AARO 无 RSS/JSON/API —— 这类只能走第三方源。
5. **GDELT 源仍可能抓不到**：退避把重试成本指数拉开并封顶 360 分钟，但**不自动停用**（网络恢复会自己回来）；已实测成功率仅 4.2% 且产出为 0。
6. **冷启动写突发期间 `/api/risk-dashboard` 会短时不响应**（**既有行为，非 ③′ 引入**；已用 `/api/cognition/status` 逐任务取到铁证）：
   隔夜/长时间未开后，后台**单线程**循环在首个迭代里要**串行**跑完一串长任务，期间重读接口取不到 DB 锁。
   真机实测（本版安装产物，2026-09-21 冷启动，DB ≈1.05 GB）首迭代时序：
   - `task.situation` 07:55:05（**0.1s，态势先服务** —— ③′ 的收益）；
   - `task.external` 07:55:05 → 08:09:56（≈15 min，常规源补抓，result=15）；
   - `task.cognition` 08:09:56 → 08:33:12（`elapsed_ms=1,395,356` ≈23 min；`provider=deepseek_chat`，
     `backfill.processed=168`、`judgments 28成/24败`、`queued=100`）；
   - `task.backup` 08:33:17 起：`source.backup` 复制出 1.05 GB `.tmp`（08:36:32）后，`PRAGMA integrity_check`
     又跑了十余分钟 —— 冷启动首迭代总时长 **≈45 min 起**。
   期间读接口表现：`/api/app/version`、`/api/situation/layers` 秒回；`/api/risk-dashboard` 在写突发中 30s/40s 超时，
   **写突发一结束（WAL 3.8 MB→0）即恢复为 HTTP 200、耗时 2.9–4.7 s**（连续采样坐实是"瞬时争用"而非"坏了"）。
   采集链、认知链、备份链在 ③′ 中**均未改动**（`c4d9f70` 只把态势检查块上移 3 行），故**不是回归**；
   建议后续单开一项（补抓/认知/备份分批提交或让出锁、读接口加短重试），降低冷启动可感度。
   **A/B 对照（同一到期条件，直接坐实非回归）**：在同一 1.05 GB 真库上，分别冷启动
   旧构建（无 ③′，`2754BBFD…`，启动时 31 条常规源到期）与新构建（含 ③′，`5DC512B4…`，29 条到期）：
   两者 `/api/app/version`（0.2s / 0.0s）与 `/api/situation/layers`（0.2s / 0.0s）均秒回，
   `/api/risk-dashboard` **两者都 90s 超时** —— 该超时只随"到期源写突发"出现，与 ③′ 无关。
7. **「测试全绿」曾与解释器耦合，现已解开（如实记录）**：
   本文件 2.1 里的 `Ran 725 … OK` 是**在另一套 SQLite 上**测的（那套编译了 `dbstat`）。
   在当前机器上，**同一个提交 `c4d9f70` 与当前 `50ceb37` 跑全量都恰好只有 1 个失败**
   ——`test_cleanup_layers.ThresholdTests.test_table_size_rule_is_evaluated`，
   断言与耗时一致。这条 A/B 一次性回答了「是不是我们引入的」：**都不是**，
   根因是那条用例与 `dbstat` 编译选项耦合（见 2.7）。修复后两个解释器各 763 全绿。
   （对照脚本：在 `c4d9f70` 隔离工作树里跑全量 → `Ran 725 … FAILED (failures=1, skipped=3)`。）
8. **估算类护栏存在固有误差**（这是刻意的取舍，不是缺陷）：交付环境无 `dbstat`，
   「单表 > 500 MB」只能用估算（实测 −8% ~ −18%，已用保守系数把方向掰向**偏高**）。
   即真实 ~435 MB 的表就可能触发该分支 —— 方向安全（清理幂等、且有 6 小时节流），
   但**它不再是精确阈值**；诊断面板的 `table_size_source` 会如实说明当前是估算还是精确。
9. **后台任务失败日志含本机绝对路径**（**本轮不改，仅记录**）：`radar_scheduler._execute`
   在任务失败时按 `exc_info=True` 把完整堆栈写进 `logs\yuanjian.log`，堆栈里会有
   `D:\远见\源码\...` 这类路径。**诊断面板的状态表不含路径**（`error_message` 已脱敏截断、
   只多一个与路径无关的 `error_location="函数:行号"`）；日志是**本机**文件、不上传，
   且由 `RotatingFileHandler(maxBytes=1MB, backupCount=5)` 封顶在约 6 MB。

---

## 四、复现方式

```powershell
cd "D:\远见\源码"
$env:PYTHONPATH='src'; $env:PYTHONWARNINGS='error::ResourceWarning'

# 回归必须用打包基线解释器（原因见 README「单元测试」）：它才是交付件真正带的 SQLite
.\.venv-build\Scripts\python.exe -m unittest discover -s tests   # 763 项；本批新增护栏三条路径 3 例
c:\python314\python.exe       -m unittest discover -s tests      # 第二个解释器，各跑一次并留原始输出

# 护栏专项（无 dbstat 环境）
python build-artifacts\qa_scheme2_probe.py      # 估算误差 / 耗时 / 边界（合成真值 + 真库）
python build-artifacts\qa_mutation_dbstat.py    # 变异对照：删掉估算兜底 → 必须变红

# 三层验证（需已安装产物 D:\远见\程序）
python build-artifacts\lead_verify_installed_v15.py     # 内容级（需 PyInstaller 的 archive readers）
python build-artifacts\lead_smoke_v15.py                # 启动烟测
python build-artifacts\lead_verify_v15_e2e.py           # 行为级（须带 PYTHONPATH=src）
python build-artifacts\qa_postinstall_150.py            # 装后与备份逐表比对
```

> 构建期脚本（`build-artifacts/`、`build/` 下的临时文件）未入库，与仓库既有惯例一致。
