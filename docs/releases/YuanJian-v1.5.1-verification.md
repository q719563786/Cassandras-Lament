# 卡珊德拉的哀歌（内部代号「远见」） v1.5.1 验证记录

**日期**：2026-09-23
**版本**：1.5.1（静默失败治理 + 远程付费账目纠错 + 交付环境里真能工作的单表护栏 + 首页条数回承诺 + 前端重复提交 + 图标改版）
**分支 / 提交**：本地 `main`，顶为 `02a2a55`（v1.5.1 八个代码提交 `90526c3`…`fcc6471` + 两份文档提交 `af4a1f3`/`02a2a55`，**领先远端 10 个提交**）
```
90526c3  fix(v1.5.1): 图标改版 + 抓取层 gzip/SSL 修复 + 首页上限回归承诺
8039395  refactor(v1.5.1): 移除 gzip 重构后的死代码
50ceb37  fix(v1.5.1): 时间列护栏统一 + 静默失败治理 + 关机不再可能悄悄花钱
6922136  fix(v1.5.1): 让「单表 > 500MB」保留期护栏在交付环境真的能工作
ea246ee  docs(v1.5.1): 使用说明升版 + README 补变异对照说明
d467006  fix(v1.5.1): 修前端监听器泄漏导致的重复提交 + 转义 + 诊断页显示远程真实状态
ba5fa4b  fix(v1.5.1): 远程付费三条 —— 预算漏算重试、默认上限 200、加连续失败熔断
af4a1f3  docs(v1.5.1): 新增变更记录与验收文档（验收数据留空位）
fcc6471  fix(v1.5.1): 设置页预算兜底同步后端默认 + cluster 视图补监听器泄漏测试
02a2a55  docs(v1.5.1): 更正版本日期与分支口径
```
- 与远端的核实口径（2026-09-23 实测）：远端 `main` = `3d8f1ec`（`git ls-remote origin refs/heads/main` 实测），
  即 **v1.5.0 那个提交确实已经推上去了，GitHub Release v1.5.0 也已建成**。
  本地 `main` 现到 `02a2a55`，**领先远端 10 个提交**，是一条**干净的快进线**。
  ⚠️ 本机沙箱会把 `.git/refs/remotes/**` 的写入吞掉 ⇒ 本地看不到 remote-tracking 引用、
  `main` 的配置项显示 `[origin/main: gone]`，**这是沙箱现象，不代表远端为空**
  （早期据此下的「远端根本没有这些提交」判断有误，已纠正）。
  故第二段 push 用 `git push -u origin main` 即可（`-u` 建立跟踪），**切勿 `--force`**
  （远端仓库为 `https://github.com/q719563786/Cassandras-Lament.git`，与 v1.4.1 改名后一致）。
- 另一本地分支 `mystique-theme`（`3d8f1ec`，即远端 `main` 当前指向的提交）是 v1.5.0 的验证提交，
  **是 `main` 的祖先**、保留不删。
**依据**：2026-09-21 两轮夜审（审计项见 `build-artifacts/`）+ 用户的付费端点风险

> 命名说明：本项目对外正式名为 **卡珊德拉的哀歌 / Cassandra's Lament**，内部代号「远见」（YuanJian）。
> 本文件按仓库既有习惯沿用 `YuanJian-` 前缀的文件名。

---

## 一、这一版做了什么

**主题：让失败留下痕迹。** v1.5.0 修的是「判级」，这一版修的是另一类更隐蔽的问题 ——
**程序正在悄悄失败，而界面上一切看起来正常**。七个提交共十项改动（① 所在的提交含三项），
其中**只有一处改业务判定**（首页条数），其余全是「把静默失败改成可观测的失败」。

| 批次 | 改动 | 为什么 / 关键锚点 |
|------|------|-------------------|
| ① 抓取层 gzip/SSL | 两条路径都声明 `Accept-Encoding: identity`；防御性解压（解不开**明确报错**）；逐块解压卡 **4×上限（20MB）**防炸弹；`fetch_json` 补齐 SSL 降级回退 | `urllib` **不自动解压** ⇒ 源回 gzip 时解析必失败、失败计数涨到源被禁用，**全程无显式报错**。同一站点"走 RSS 能通、走 JSON 失败"也一并修掉 | `_read_raw` / `_decode_body` / `_decompress_gzip` |
| ② 死代码清理 | 删 `_GZIP_CHUNK` 常量与 `fetch_json` 末尾不可达的 `return data` | 重构（①）后的残留。注：这两行曾被误判为"并发写入损坏"回滚过一次，属误判，此处重新落盘 | — |
| ③ 图标改版（14 枚） | 13 枚按「**版画式印章**」语言重画 + 新增 `ic_atlas`（古式浑天环） | 此前「今日远见」与「全球态势」共用 `ic_radar`，侧栏两个一级导航**撞图**。**键名不变、调用方零改动** | `static/js/icons.js` |
| ④ 首页上限 | `/api/risk-dashboard` 由 `limit=50` 改回 **3**，并补端到端断言 | README 与使用说明都承诺「首页最多 3 条」，代码不是 ⇒ **改代码让文档成立**。此前无人守，才漂到 50 | `http_api.py:_get_risk_dashboard` |
| ⑤ 时间列护栏统一 | A/F 两层的「仅接受 `YYYY-MM-DD T…` 形状」护栏照搬到 B/C/D/E 四层；**非标准形状一律不删** | 清理判据按**字符串字典序**比时间，而写入端有多套形状（`Z`/`+00:00`/空格/空串）；位置 10 处 `' ' < 'T'` ⇒ 该留的被删、该删的删不掉。**纯防御** | `retention.py:_iso_shaped` |
| ⑥ 静默失败治理（主线） | purge 孤儿引用改为**写时清除**+异常入审计；关机清队列失败冻结为 `paused_shutdown`；调度器失败记**完整堆栈**；另四处静默失败可观测；修掉"两条关机用例从未被收集"的测试盲区 | 关机清队列失败会让**排队付费作业活过关机**继续花钱，而该函数的文档承诺"确保不会有新请求发出"；孤儿引用是**落库事实**，渲染时过滤会让同一行在不同页面解释不同。**均不改变业务判定** | `impacts.py` / `remote_ai.py` / `radar_scheduler.py` / `cognition.py` / `database.py` |
| ⑦ 单表 500MB 护栏 | `dbstat` 精确优先 → 缺失则**估算** → 都失败标 `unavailable`；新增 `largest_table_source`；退化/不可用各记 warning；诊断面板新增 `table_size_source` | 交付件 `sqlite3.dll` **未编译 `dbstat`** ⇒ 该护栏在用户手上**静默失效**（既有问题，非本版引入）。估算误差实测见「三、已知局限」第 1 条 | `retention.py:_largest_table_bytes` |
| ⑧ 前端重复提交 | 卡片动作绑定改为「先按引用 `removeEventListener` 再 add」；`title` 属性转义；诊断页新增**远程真实状态条** | 今日页每 45 秒自刷 ⇒ 监听器**叠加**，点一次确认会重复渲染 + 重复 POST。状态条：此前鉴权失效后程序全程本机跑，界面却看着一切正常 | `ui_core.js` / `views/today.js` / `views/cluster.js` / `views/diag.js` |
| ⑨ 远程付费三条 | 预算改**按次记账**（5 个出口各计一次）；默认上限 **2000→200**；新增**连续失败熔断**（阈值 5）+ **半开探测**（每 15 分钟放行一条） | 预算只统计 `finished_at IS NOT NULL`，而重试分支不写该字段 ⇒ **重试完全不计入预算**，一天可发约 **2400 次**付费调用（预算写 2000）。2000 是照**免费**端点定的，对付费端点太宽松 | `remote_ai.py` |
| ⑩ 文档 | 使用说明升版 + 「主功能导航」「通知节流」；README 钉死回归解释器并澄清变异对照不在 `tests/` 下 | 见 2.4 | `使用说明.md` / `README.md` |

**schema**：本版**无 schema 迁移**（无新表、无新列、无迁移版本）。
**外观**：`theme-mystique.css` **逐字节未动**（它是交付件）。

---

## 二、验收数据

> ✅ **本节为第二段（代码冻结后）实测值**，采集于 2026-09-23，冻结基线 `02a2a55`。复现见「四、复现方式」。

### 2.1 测试

```
# 打包基线解释器（.venv-build = 3.14.5 / SQLite 3.50.4 / 无 dbstat），全量三轮：
.\.venv-build\Scripts\python.exe -m unittest discover -s tests
  run1   Ran 790 tests in 194.109s   OK (skipped=3)   exit=0
  run2   Ran 790 tests in 193.042s   OK (skipped=3)   exit=0
  run3   Ran 790 tests in 190.004s   OK (skipped=3)   exit=0

# 第二个解释器（对照），各跑一次：
c:\python314\python.exe -m unittest discover -s tests
  run4   Ran 790 tests in 191.167s   OK (skipped=3)   exit=0

（原始输出：build-artifacts/qa-logs/_sg2_run1.txt / _sg2_run2.txt / _sg2_run3.txt / _sg2_run314.txt）
```

- 四轮一致：**790 项，skipped=3，exit=0**（skipped 三条均为既有 `BLOCKED · 架构师未落地` 留白，非本版引入）。
- 提示：以 `PYTHONWARNINGS=error::ResourceWarning` 运行时，解释器退出阶段会打印
  `Exception ignored ... ResourceWarning: Implicitly cleaning up <HTTPError 404: 'Not Found'>`
  —— 它来自某个调试用 404 回显用例的**析构器**，**不影响用例结果、不改变 exit code（仍为 0）**，如实记录。

- 计数沿革（实测收口）：`734`（`90526c3`）→ `761`（`50ceb37`）→ `767`（`6922136`）→ `785`（`ba5fa4b`）
  → **`790`**（`d467006` 新增真跑 JS 模块 + `fcc6471` 补监听器泄漏测试，实测四轮一致）。

### 2.2 变异对照（证明断言有牙）

```
本版**实测能跑**的变异对照 = 3 个脚本、共 21 条，全部「基线绿 → 改坏变红 → 逐字节还原」：
  · qa_mutation_dbstat.py                              1 条（删掉单表护栏的估算兜底 → 3 条用例变红）
  · scratch-yj-20260921/mutation_retention_guard.py    4 条（B/C/D/E 四层各去掉护栏 → 只有该层变红）
  · scratch-yj-20260921/mutation_batch2.py            16 条（#23–#26 + B1/B2a/B2b/B3a/B3b/B3c）
全部 restored_byte_identical=True，相关源码 SHA256 与改前一致。
原始输出：build-artifacts/qa-logs/mutation_dbstat.txt、
build-artifacts/scratch-yj-20260921/mutation_retention_guard.txt、mutation_batch2.txt。
```

- **口径不调和历史数字**：历史记录里 `50ceb37` 记 10/10、`ba5fa4b` 记 17（原 11 + 新 6）、README 曾写「15 + 1」——
  口径不一，本版一律以**实测能跑的条数**为准：**21**。另有 gzip 一项当时是**内联跑过**（无独立脚本）、
  `qa_app_double_mutation.py` 写死了已不存在的旧解释器路径 ⇒ **不可复跑**，均不计入。
- **不入库**：这些脚本在 `build-artifacts/` 下、不进版本库；**克隆仓库后随 `tests/` 自动可跑到的变异对照为 0**。

### 2.3 三层验证（全部在**已安装的产物**上跑）

```
【内容级】build-artifacts/lead_verify_installed_v151.py   →  exit=0
  扫描 37 个 PYZ 模块，命中 v1.5.1 全部锚点：
    A   _decompress_gzip / _decode_body / MAX_DECOMPRESSED_BYTES   ← external_sources
    C   _iso_shaped                                                ← retention
    D   _estimate_largest_table / largest_table_source              ← retention / radar_scheduler
    E   circuit_open / REMOTE_CIRCUIT_PROBE_MINUTES / CIRCUIT_OPEN_REASON   ← remote_ai
    ⑥   paused_shutdown                                             ← remote_ai
    沿用 v1.5.0：parse_geojson / SituationPoint / _failure_backoff_minutes / SITUATION_KEEP_DAYS / structure_source
  静态资源：theme-mystique.css 53519B 逐字节一致=True；ne_110m_land.geojson 138160B 逐字节一致=True；
    版本号(JS/CSS/视图 JS) 在包内=True；旧 v1.5.0 文案已消失；主题样式表引用=True；导航入口「全球态势」=True
  结论：安装包确实含 v1.5.1 全部改动 ✓

【启动烟测·无头】YUANJIAN_HEADLESS=1 + YUANJIAN_BACKGROUND=1 + --background
  build-artifacts/lead_smoke_v151.py   →  20/20，exit=0
    [1] 版本号=1.5.1（进程拉起 → /api/app/version 可用：3.60s）
    [2] /api/risk-dashboard 200，条目 3 条（≤3 为承诺）
    [3] /api/situation/layers 200（6 图层：quake593/wildfire324/flood24/storm22/drought12/volcano6；3 源 S-GDACS/S-NASA-EONET/S-USGS-QUAKE）；
        /api/situation/points 200 且约束完整；越界参数一律 400（hours=0 / hours=999 / limit=0 / layer=nope / bbox 反转）；
        同源底图 /geo/ne_110m_land.geojson 200（无令牌可取），FeatureCollection，127 个面要素，138,160B
    [4] schema_migrations=[1..8]；v8 已落库（situation_events 存在）；v7 **未被重跑**（版本行只有一条）
    [5] 诊断面板 table_size_source='estimate'
    [6] POST /api/shutdown → 200 {'status':'shutting_down'}；进程退出（exit code 0），无残留

【行为级·临时库 + 本机回环】build-artifacts/lead_verify_v151_e2e.py   →  22/22，exit=0
    （所有断言都从库里读回，不靠接口自述；临时库 = 不碰真库）
    S1 结算：创建 → 单条 resolve → 批量 batch-resolve → 从库读回
        （状态非 open、出现结案记录、resolved_by 可溯源、批量结案同样落不可撤销记录）
    S2 开关：PUT settings/learning（关）→ runtime_state 已落为关；
             PUT settings/backup hour=5 → 重读仍 hour=5（已持久化）
    S3 清理：F 层按 SITUATION_KEEP_DAYS=30 清 situation_events（窗口外被删、窗口内留下、计数如实）
    S4 时间列护栏：_iso_shaped → 非 ISO 形状一律不删（仅 ISO 形状那条进入删除候选）
    S5 单表 500MB 护栏：无 dbstat 环境走估算，largest_table_source='estimate'
    S6 远程付费三条常量：DAILY_REMOTE_BUDGET=200 / CIRCUIT_THRESHOLD=5 / PROBE_MIN=15 / REASON='circuit_open'

（原始输出：build-artifacts/qa-logs/_sg2_verify_content.txt / _sg2_smoke.txt / _sg2_e2e_src.txt）
```

> 说明：S1 的「结算」三接口在**临时库**上跑（自建回环服务），**未**对正在运行的真库实例发任何写请求；
> 真库的账目/判定表在全程保持逐表未变（见 2.6）。

### 2.4 发布闸门

```
PYTHONPATH=src python tools/privacy_scan.py --committed
  committed_files=155   safe=True   blocked=0   findings=0   exit=0
（原始输出：build-artifacts/qa-logs/_sg2_privacy.txt）
```

- 口径：`safe=True` 且 `blocked=0`、`findings=0` —— 已提交内容不含隐私/密钥命中，闸门放行。

### 2.5 构建与安装

```
exe：D:\远见\程序\YuanJian.exe   6,957,232 B
     sha256=F004DD63161DE7D3C3727B7B31036A8B3E19BEA947B2D5EDE8BB928D8EB20053
theme-mystique.css：53519 B  sha256=17DA4778A53733206D36A024DEEBFF98D72002DECD240938BB452B1B93A44943
     （与 v1.5.0 逐字节一致 —— 升版刻意跳过它的文件头）
ne_110m_land.geojson：138160 B  sha256=9E0729EE253CA7D7A5C4AE9395FB1902264C5377C52E224D13DD85010E2835D9
安装位置：D:\远见\程序（旧版改名保留为回滚点 D:\远见\程序-旧-20260923-085256）
data-dir.txt：已恢复为 D:/远见/数据
首次启动耗时：3.60s（进程拉起 → /api/app/version 可用）
构建：.venv-build + PyInstaller（build/build_windows.ps1 的 spec），旧 dist 挪走不删（dist-旧-20260923-084205）
```

### 2.6 真实数据未被写入

```
与升级前逐表比对（真库 D:\远见\数据，库文件约 1.05 GB；烟测前后各取一次）

· 账目 / 判定类表 —— 逐表**完全未变**（升级与烟测都没往里写，也没有触发认知）：
    forecasts=8737   resolutions=0   forecast_versions=19400   personal_impacts=119339
    judgments=79116  notification_log=81352   audit_log=19526
· 仅**采集类**表增长（烟测时后台单线程正常补抓所致，非升级写入）：
    external_items     110588 → 110673   (+85)
    situation_events        687 →    981   (+294)
    event_clusters        81605 →  81624   (+19)
· schema_migrations 前后均为 [1,2,3,4,5,6,7,8]：本版无迁移、v7 **未被重跑**、v8 已在位。
· 库文件 1,055,211,520 → 1,055,752,192 B（+540,672 B，WAL 落盘 + 采集增长）。

结论：与预期一致 —— **除常规采集增长外，账目与判定表未被写入**；v1.5.1 不在真库上做任何写迁移。

（原始输出：build-artifacts/qa-logs/_sg2_db_pre.txt / _sg2_db_post.txt）
```

---

## 三、已知局限（写下来，不假装不存在）

1. **交付版「单表 > 500MB」护栏是估算值，不是精确阈值**：交付件没有 `dbstat`，只能用
   「逐表行数 × 沿 rowid 均匀开窗采样平均行宽」估算（实测 −8%~−18%，已用保守系数把方向掰向**偏高**）。
   即真实约 **435MB** 的表就可能触发该分支 —— 方向安全（清理幂等 + 6 小时节流），但**它不再精确**。
   诊断面板的 `table_size_source` 会如实说明当前是精确还是估算。**主护栏（库文件 vs 2048MB）不受影响。**
2. **`WITHOUT ROWID` 表的退化路径未实测覆盖**：真库该类表为 0，代码保留了"退化到前 N 行采样"的路径，
   但**没有任何测试或真数据走到过它**。
3. **后台任务失败日志含本机绝对路径**（本轮未改）：`radar_scheduler._execute` 失败时按 `exc_info=True`
   把完整堆栈写进 `logs\yuanjian.log`，堆栈里会出现 `D:\远见\源码\...` 这类路径。
   **诊断面板的状态表不含路径**（`error_message` 已脱敏截断、只多一个与路径无关的
   `error_location="函数:行号"`）；日志是本机文件、不上传，且由
   `RotatingFileHandler(maxBytes=1MB, backupCount=5)` 封顶在约 6MB。
4. **`signals.create_candidate` 仍收空串**：它把该值当**相对日期的参照点**用，改 `now` 会动告警定级的
   业务判定，故只修了写入端（`occurred_at` 落空串）而不动这里。
5. **`audit_log` 里并存两套时间助手**：未统一（纯历史包袱，无行为影响）。
6. **真库存量异常时间形状不回写**：`external_items.published_at` 的 537 条 RFC 2822、
   `personal_impacts.updated_at` 的 4 条空格分隔，护栏只保证"不误删"，**不做 backfill**——
   回写等于伪造发布时间，不做。代价是这些行会"焊死"在表里（只多占空间）。
7. **态势层不做跨源同一事件合并**：`event_id` 带源前缀，不同源的同一场地震会各占一个点。
8. **AARO / 天外实体源不可采**：`aaro.mil` 对机房出口 IP 返回 Akamai 403、脱该出口则在 DNS 层被拒，
   且 AARO 无 RSS/JSON/API。
9. **GDELT 源产出为 0**：退避（15/30/60/120/240/360，封顶 360、不自动停用）把重试成本指数拉开，
   但实测成功率仅 4.2% 且产出为 0。网络恢复它会自己回来。
10. **冷启动写突发期首页接口短时不响应**（**既有行为，v1.5.0 起已用 A/B 坐实非回归**）：
    隔夜/长时间未开后，后台**单线程**循环首迭代要串行跑完常规源补抓（≈15 分钟）→ 认知
    （≈23 分钟）→ 备份复制 + `integrity_check`（≈16 分钟）；期间重读接口取不到 DB 锁。
    `/api/app/version`、`/api/situation/layers` 秒回，`/api/risk-dashboard` 超时；
    **写突发一结束（WAL 归零）即恢复 200（2.9~4.7 s）**。详见
    [`YuanJian-v1.5-verification.md`](YuanJian-v1.5-verification.md) 三-6。
11. **诊断页「暂停 / 已回退本机」两态尚未生效**：前端逻辑已就绪，但后端尚未暴露
    `ai_paused` / `ai_pause_reason` / `ai_fallback_local` / `ai_fallback_reason` 四个字段，
    故这两态要等后端补字段后才会自动出现（当前只会显示正常 / 限流退避两态）。
12. **~~`settings.js` 的输入框兜底仍写死 `?? 2000`~~ 已修复**：在 `fcc6471` 里，
    三处（AI 表单输入框默认值 `ai-daily-budget` 的 value、同处「默认 X」提示文案、提交时
    `budgetRaw` 为空的兜底）全部改为「**优先读后端返回值、读不到才用 200**」
    （`ai?.daily_budget ?? 200`），并在函数头以注释钉住「改后端默认必须同步此三处」。
    现文件中唯一出现 `2000` 的地方是一条**说明性注释**（注明该数以前照免费端点定过）。
13. **真库那台 `daily_budget` 仍是 2000**：它显式存过该值且端点是免费的 Agnes，**按设计不覆盖用户值**。
    要连"已存过"的一起收紧需要一次写库迁移，属产品决策，本轮不做。
14. **首页上限改动是"回文档"而非"改文档"**：把代码从 50 改回 3。若将来产品上确实想要更多条，
    应改文档 + 改断言，而不是再让代码漂走。
15. **`settings.js:462` 的 `const` 重赋值（记 v1.5.2）**：`interests` 在第 81 行由
    `const [backup, retention, learning, archive, ai, interests] = await Promise.all([...])` 声明，
    而 `paintInterests` 里写着 `if (!interests) interests = {objects: [], links: []}` ——
    对一个 `const` 赋值，**当 `/api/interests` 读取失败（该行 `catch(() => null)` 返回 `null`）时会抛
    `TypeError`**，可能导致整张设置页渲染中断。ES 模块为严格模式，故必抛。修法：改为局部变量或 `let`。

---

## 四、复现方式

```powershell
cd "D:\远见\源码"
$env:PYTHONPATH='src'; $env:PYTHONWARNINGS='error::ResourceWarning'

# 回归必须用打包基线解释器（原因见 README「单元测试」）：它才是交付件真正带的 SQLite
.\.venv-build\Scripts\python.exe -m unittest discover -s tests        # 全量三轮：本版四轮一致 790 OK（skipped=3）
c:\python314\python.exe       -m unittest discover -s tests -v        # 第二个解释器，各跑一次留原始输出

# 发布闸门
python tools\privacy_scan.py --committed

# 护栏专项（无 dbstat 环境）
python build-artifacts\qa_scheme2_probe.py      # 估算误差 / 耗时 / 边界（合成真值 + 真库）
python build-artifacts\qa_mutation_dbstat.py    # 变异对照：删掉估算兜底 → 必须变红

# 三层验证（需已安装产物 D:\远见\程序）
python build-artifacts\lead_verify_installed_v151.py    # 内容级（需 PyInstaller 的 archive readers）
python build-artifacts\lead_smoke_v151.py               # 启动烟测（无头：YUANJIAN_HEADLESS=1 + YUANJIAN_BACKGROUND=1 + --background）
python build-artifacts\lead_verify_v151_e2e.py          # 行为级（临时库 + 回环，须带 PYTHONPATH=src）
python build-artifacts\lead_install_round10.py          # 安装/回滚 + 装后与构建产物逐字节校验
```

> 构建期脚本（`build-artifacts/`、`build/` 下的临时文件）未入库，与仓库既有惯例一致。
> 因此**从克隆仓库直接跑 `tests/` 能自动跑到的变异对照为 0** —— 它们是交付流程里的人工步骤。
