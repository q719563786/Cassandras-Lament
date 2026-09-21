# 卡珊德拉的哀歌（内部代号「远见」） v1.5.0 验证记录

**日期**：2026-09-21
**版本**：1.5.0（密教主题 + L4 定级接入方法论 + 全球态势图层 + GDELT 退避与保留期）
**分支 / 提交**：`mystique-theme` @ `c4d9f70`
（`4b28fc3` 主体 +4001/−160　→　`3ee51a6` 本文档　→　`c4d9f70` 态势调度顺序修复 +160/−5）
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

---

## 四、复现方式

```powershell
cd "D:\远见\源码"
$env:PYTHONPATH='src'; $env:PYTHONWARNINGS='error::ResourceWarning'
python -m unittest discover -s tests          # 725 项（含调度顺序 2 例）；噪声用例单独重跑见 2.1

# 三层验证（需已安装产物 D:\远见\程序）
python build-artifacts\lead_verify_installed_v15.py     # 内容级（需 PyInstaller 的 archive readers）
python build-artifacts\lead_smoke_v15.py                # 启动烟测
python build-artifacts\lead_verify_v15_e2e.py           # 行为级（须带 PYTHONPATH=src）
python build-artifacts\qa_postinstall_150.py            # 装后与备份逐表比对
```

> 构建期脚本（`build-artifacts/`、`build/` 下的临时文件）未入库，与仓库既有惯例一致。
