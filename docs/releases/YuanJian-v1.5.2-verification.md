# 卡珊德拉的哀歌（内部代号「远见」） v1.5.2 验证记录

**日期**：2026-09-23
**版本**：1.5.2（把"安静地坏掉"摆到台面上 + 采集每轮时间预算）
**分支 / 提交**：本地 `main`。**冻结基线 `d1449dd`** —— v1.5.2 的四个代码提交：
```
8df21f1  fix(v1.5.2): 设置页「利益对象」区块失败不再拖垮整页（const 重赋值）
c3c2c2c  feat(v1.5.2): 诊断页补齐远程状态四字段 + 采集每轮时间预算 + WITHOUT ROWID 退化补测
ae879b4  fix(v1.5.2): 诊断页不再把暂停原因硬说成「密钥失效」
d1449dd  fix(v1.5.2): 远程状态按「是否启用」门控——主动关闭不再被报成故障暂停
```
- 其后有一个**发布提交**（升版 21 个文件 + `CHANGELOG.md` + `README.md` + 本文档），
  在 team-lead 视觉验收通过后与其一并推送。
- **⚠️ 本记录写就时：`push` 与 GitHub Release 尚未执行**（等 team-lead 的视觉验收）。
  远端口径与 v1.5.1 一致（`git push -u origin main`，**切勿 `--force`**；
  远端仓库 `https://github.com/q719563786/Cassandras-Lament.git`）。
- 本机沙箱会把 `.git/refs/remotes/**` 的写入吞掉 ⇒ 本地看不到 remote-tracking 引用，
  **这是沙箱现象，不代表远端为空**（与 v1.5.1 同一现象）。

> 命名说明：本项目对外正式名为 **卡珊德拉的哀歌 / Cassandra's Lament**，内部代号「远见」（YuanJian）。
> 本文件按仓库既有习惯沿用 `YuanJian-` 前缀的文件名。

---

## 一、这一版做了什么

**主题：把"安静地坏掉"摆到台面上。** v1.5.1 让失败**留下痕迹**（日志、审计、来源标注）；
这一版修的是"痕迹有了但**没人看得到**"——四处改动，**全都不改业务判定**。

| 批次 | 改动 | 为什么 / 关键锚点 |
|------|------|-------------------|
| ① 设置页不再白屏 | `settings.js` 的「个人利益」区块：`paintInterests` 与提交处理对 `const interests` 重赋值（`interests = {...}` / `interests.objects = []`）。接口 `/api/interests` 失败时 `interests === null` ⇒ 抛 `Assignment to constant variable` ⇒ `render` 中断 ⇒ **设置页整页白屏** | 改法**不是**把 `const` 写成 `let`：用**局部变量**读取，绝不重赋值 const；并把「接口失败（`null`）」与「成功但空（`{}`）」两种占位**分文案**（失败态提示"稍后重试"） | `views/settings.js` |
| ② 诊断四字段（主线） | 后端此前**一个都没暴露** `ai_paused`/`ai_pause_reason`/`ai_fallback_local`/`ai_fallback_reason`，前端状态条恒为 `undefined` ⇒ 「已暂停」「已回退本机」**两态永远不显示**。补四字段，**只用既有状态**（`paused_auth` 行 + `remote_error_fallback_local`），**不新增状态机** | 远程失效时应用会**静默降级到本机研判**、界面看起来一切正常——这正是"安静地坏掉"。回退判定**绑在"最近一次 cognition 轮"的窗口**上（不绑窗口的告警等于噪声：几天前一次超时永远挂在界面上） | `diagnostics.py:_read_remote_health/_fallback_window_start` |
| ③ 暂停原因用中立措辞 | 暂停有两种成因：**密钥失效**（说对了）与**连续失败熔断**（说"密钥失效"**是错的**，会让用户去改一个没坏的密钥）。前端**只原样显示**后端给的 `ai_pause_reason`（已是人话），**不做二次翻译、不拼后缀** | 熔断≠鉴权：前者只需等对端恢复，后者才要用户动作。措辞必须**分开** | `diagnostics.py:_PAUSE_REASON_*` / `views/diag.js` |
| ④ 远程状态按「是否启用」门控 | 用户**主动关掉**远程研判时，库里的历史 `paused_auth` 残留会让诊断页报「已暂停」。门控取自**同一份快照**的 `ai_enabled`（避免"两个真相"）：`enabled=False` ⇒ 四字段一律 `False/""` | 用户自己关的，报成"故障暂停"是**误导**，会把他推去修一个他故意关掉的东西 | `diagnostics.py:_read_remote_health(enabled)` |
| ⑤ 采集每轮时间预算 | `refresh_due_sources` 此前在**一次调用里**把**所有**到期源抓完；冷启动时几乎全部到期，叠加对端大面积 TLS/连接超时 ⇒ 一轮可跑到 **≈35 分钟**，**独占**后台单线程循环（期间认知/备份轮不到，首页只读接口也拿不到响应）。引入 `COLLECT_PASS_BUDGET_SECONDS = 120`：每轮**先看表再动手**，到点**不再开始**新源（保证至少开一个），剩余到期源**原样留下**（`next_fetch_at` 不动）留到下一轮 | 取 120s 的依据：真库近 7 天 `external_runs`（只读）单源 median 2.4s / p90 5.1s / max 68.9s，35 个到期源 ⇒ 常态 ≈88s（**仍一轮抓完，不给健康网络加延迟**）、病态最坏 ≈35min ⇒ 压到 ≈2min（**17 倍**） | `external_radar.py` |
| ⑥ 两处补测 | `WITHOUT ROWID` 表的估算退化路径（真库该类表为 0 ⇒ **从未被跑过**）；设置页那处 `const` 重赋值的**树内变异对照** | 退化路径一旦被踩到：`SELECT MIN(rowid)` 抛 `no such column: rowid`，没兜底 ⇒ **整张表**估算失败 ⇒ `unavailable` ⇒ 「单表>500MB」护栏对这张表失效（与上一版"静默失效"同病） | `tests/test_retention_guards.py` / `tests/test_settings_render.py` |

**schema**：本版**无 schema 迁移**（无新表、无新列、无迁移版本；`schema_migrations` 保持 `[1..8]`）。
**外观**：`theme-mystique.css` **逐字节未动**（它是交付件，升版刻意跳过它的文件头）。

**这一版同时销掉 v1.5.1 记录里的两条挂账**：
- v1.5.1 三-11「诊断页暂停/回退两态尚未生效」→ **本版生效**（后端已补四字段；真机实测见 2.3）。
- v1.5.1 三-15「`settings.js:462` 的 `const` 重赋值」→ **本版 `8df21f1` 修复**（附树内变异对照）。

---

## 二、验收数据

> ✅ **本节为第二段（代码冻结后）实测值**，采集于 2026-09-23，冻结基线 `d1449dd`。复现见「四、复现方式」。

### 2.1 测试

```
# 打包基线解释器（.venv-build = 3.14.5 / SQLite 3.50.4 / 无 dbstat），全量三轮：
.\.venv-build\Scripts\python.exe -m unittest discover -s tests
  run1   Ran 810 tests in 195.770s   OK (skipped=3)   exit=0
  run2   Ran 810 tests in 195.938s   OK (skipped=3)   exit=0
  run3   Ran 810 tests in 193.177s   OK (skipped=3)   exit=0

# 第二个解释器（对照），各跑一次：
c:\python314\python.exe -m unittest discover -s tests
  run4   Ran 810 tests in 195.475s   OK (skipped=3)   exit=0

（原始输出：build-artifacts/qa-logs/_sg4_run1.txt / _sg4_run2.txt / _sg4_run3.txt / _sg4_run314.txt）
```

- 四轮一致：**810 项，skipped=3，exit=0**（skipped 三条均为既有 `BLOCKED · 架构师未落地` 留白，非本版引入）。
- 提示：以 `PYTHONWARNINGS=error::ResourceWarning` 运行时，解释器退出阶段会打印
  `Exception ignored ... ResourceWarning: Implicitly cleaning up <HTTPError 404: 'Not Found'>`
  —— 它来自某个调试用探针用例的**析构器**，**不影响用例结果、不改变 exit code（仍为 0）**，如实记录。
- 计数沿革（实测收口）：v1.5.1 收在 `790` ⇒ 本版 `c3c2c2c`（诊断四字段 + 预算 + WITHOUT ROWID）、
  `8df21f1`（设置页）、`ae879b4`/`d1449dd`（诊断措辞与门控）之后收在 **`810`**（四轮一致）。

### 2.2 变异对照（证明断言有牙）

```
本版**随 `tests/` 一起跑**、属于 810 项之内的变异对照 = 3 条（全部绿；去掉对应修复即必须变红）：
  · tests/test_settings_render.py::test_mutation_control_reassign_const_must_go_red   ← ①：改回对 const 重赋值
  · tests/test_diag_render.py::test_mutation_control_hardcoded_suffix_must_go_red     ← ③：改回硬编码「（密钥失效）」
  · tests/test_settings_render.py::test_mutation_control_fallback_2000_must_go_red    ← 沿用 v1.5.1
```

- **口径变化（重要）**：**v1.5.1 的变异对照**放在 `build-artifacts/`（**不进版本库** ⇒ 克隆仓库**不可复现**）；
  **v1.5.2 把新增的变异对照固化成 `tests/` 内的用例** ⇒ 随那 810 项一起跑，**克隆仓库后自动可复现**。
  即：本版在"守得住"这件事上比上一版更进一步（不再依赖交付包里的手工脚本）。
- 其余四字段 / 门控 / 采集预算 / `WITHOUT ROWID` 的变异为**开发期手跑**（见各提交信息逐条留痕），
  本版**未**固化为独立脚本，故**不计入**上面的条数。
- 沿用 v1.5.1 的 `build-artifacts/` 脚本（`qa_mutation_dbstat.py` 等 3 个、共 21 条）**未变**。

### 2.3 三层验证（全部在**已安装的产物**上跑）

```
【内容级】build-artifacts/lead_verify_installed_v152.py   →  exit=0
  扫描 37 个 PYZ 模块，命中 v1.5.2 全部锚点：
    A   _read_remote_health / _fallback_window_start / _parse_iso / read_ai_setting
        _PAUSE_REASON_AUTH / _PAUSE_REASON_CIRCUIT / _FALLBACK_REASONS / REMOTE_FALLBACK_RECENT_MINUTES
    C   COLLECT_PASS_BUDGET_SECONDS / pass_budget_seconds
    沿用 v1.5.1：_iso_shaped / _estimate_largest_table / largest_table_source /
        circuit_open / REMOTE_CIRCUIT_PROBE_MINUTES / CIRCUIT_OPEN_REASON /
        _decompress_gzip / _decode_body / MAX_DECOMPRESSED_BYTES / paused_shutdown
    沿用 v1.5.0：parse_geojson / SituationPoint / _failure_backoff_minutes / SITUATION_KEEP_DAYS / structure_source
  静态资源：theme-mystique.css 53519B 逐字节一致=True；ne_110m_land.geojson 138160B 逐字节一致=True；
    版本号(JS/CSS/诊断+设置视图 JS) 在包内=True；旧 v1.5.1 文案已消失；
    主题样式表引用=True；导航入口「全球态势」=True；
    诊断页「暂停态原文显示」=True、硬编码「（密钥失效）」旧后缀已消失；
    设置页「兴趣区块失败降级」=True、对 const 重赋值的旧 bug 已消失
  结论：安装包确实含 v1.5.2 全部改动 ✓

【启动烟测·无头】YUANJIAN_HEADLESS=1 + YUANJIAN_BACKGROUND=1 + --background
  build-artifacts/lead_smoke_v152.py   →  25/25，exit=0
    [1] 版本号=1.5.2（进程拉起 → /api/app/version 可用：3.35s）
    [2] /api/risk-dashboard 200，条目 3 条（≤3 为承诺）
    [3] /api/situation/layers 200（6 图层：quake661/wildfire324/flood24/storm22/drought12/volcano6；3 源 S-GDACS/S-NASA-EONET/S-USGS-QUAKE）；
        /api/situation/points 200 且约束完整（count=271 hours=24 limit=500）；越界参数一律 400
        （hours=0 / hours=999 / limit=0 / layer=nope / bbox 反转）；同源底图 200（无令牌可取）FeatureCollection 127 面 138,160B
    [4] schema_migrations=[1..8]；v8 已落库；v7 **未被重跑**（版本行只有一条）
    [5] 诊断面板 table_size_source='estimate'；**v1.5.2 四字段 + ai_enabled 一个不缺**，
        且「标志↔原因」不变式成立 —— 真机当前 `ai_paused=True`、原因=熔断人话文案、`ai_fallback_local=False`
        （**本版头号活证据**：真机此刻正处于熔断暂停，旧版界面上"看不出"，本版如实报出。
         **这是观测到的事实、不是本版引入的故障**：该熔断由远程**连续调用失败**触发，是既有运行状态，
         本版只把它从"看不见"变成"看得见"；对用户而言，远程研判此刻确实正在退回本机。）
    [6] POST /api/shutdown → 200 {'status':'shutting_down'}；进程退出（exit code 0），无残留

【行为级·临时库 + 本机回环】build-artifacts/lead_verify_v152_e2e.py   →  74/74，exit=0
    （所有断言都从库里读回；临时库 = 不碰真库）
    S1 结算：创建 → 单条 resolve → 批量 batch-resolve → 从库读回（状态、结案记录、resolved_by 可溯源）
    S2 开关：PUT settings/learning（关）已落库；PUT settings/backup hour=5 → 重读仍 hour=5
    S3 清理：F 层按 SITUATION_KEEP_DAYS=30 清 situation_events（窗口外被删、窗口内留下、计数如实）
    S4 时间列护栏：_iso_shaped → 非 ISO 形状一律不删（仅 ISO 那条进入删除候选）
    S5 单表 500MB 护栏：无 dbstat ⇒ estimate
    S6 远程付费治理常量：DAILY_REMOTE_BUDGET=200 / CIRCUIT_THRESHOLD=5 / PROBE_MIN=15 / REASON='circuit_open'
    S7 **诊断四字段 × 五情形**：正常 / 鉴权暂停（原因含「密钥」）/ 熔断（原因含「费用」且 ≠ 鉴权原因）/
        本轮有回退（原因含「超时」）/ 上一轮旧回退不粘住 / 未知 kind 仍给非空原因 / 已关闭+历史残留 → 四字段清空
    S8 **ai_enabled 门控**：关闭态四字段全 False/空；重新打开后同一份残留立刻恢复上报（门控而非查询失效）
    S9 **采集每轮预算**：120s/单源 5s ⇒ 本轮 24 个且剩余 16 个仍到期；多轮恰好抓满 40 个（不重、不丢、2 轮）；
        单源慢于预算仍保证推进一个（不空转）；显式注入预算 10s/单源 2s ⇒ 5 个
    S10 **WITHOUT ROWID 估算**：前提核实该表确无 rowid；来源=estimate（≠unavailable、绝不伪装 dbstat）；
        认出 big_wr；估算值落在原始字节 ±50% 内；无 rowid 表采样退化返回 >0（不会把估算算成 0 字节）

（原始输出：build-artifacts/qa-logs/_sg4_verify_content.txt / _sg4_smoke.txt / _sg4_e2e.txt）
```

> 说明：S1/S2 的三接口在**临时库**上跑（自建回环服务），**未**对正在运行的真库实例发任何写请求。
> 真库的账目/判定表在全程保持逐表未变（见 2.6）。
> 自体审计（如实记录）：内容级脚本第一版把 `diag.js` 的**注释**（说明"旧代码曾在此硬编码"的那两句）
> 误当成"硬编码还在"，一度判红；收紧为"旧模板字面量是否仍在"后转绿。**是脚本的探针太粗，不是代码的问题。**

### 2.4 发布闸门

```
PYTHONPATH=src python tools/privacy_scan.py --committed

# 发布提交前（冻结基线 d1449dd 的已提交内容）：
  committed_files=156   safe=True   blocked=0   findings=0   exit=0
# 发布提交后（含本文件与升版/文档改动）：
  committed_files=157   safe=True   blocked=0   findings=0   exit=0

（原始输出：build-artifacts/qa-logs/_sg4_privacy.txt / _sg4_privacy2.txt）
```

- 口径：`safe=True` 且 `blocked=0`、`findings=0` —— 已提交内容不含隐私/密钥命中，闸门放行。
- 两次都过：先扫冻结基线（156），落发布提交后再扫一次（157，多出的是本文件）。

### 2.5 构建与安装

```
exe：D:\远见\程序\YuanJian.exe   6,962,565 B
     sha256=98D94CB6CF3B1AB986D2798DB77E2CF64E1B28AA9E12F67B36F44FA645039A6C
theme-mystique.css：53519 B  sha256=17DA4778A53733206D36A024DEEBFF98D72002DECD240938BB452B1B93A44943
     （与 v1.5.1 逐字节一致 —— 升版刻意跳过它的文件头）
ne_110m_land.geojson：138160 B  sha256=9E0729EE253CA7D7A5C4AE9395FB1902264C5377C52E224D13DD85010E2835D9
     （与交付件逐字节一致）
安装位置：D:\远见\程序（旧版改名保留为回滚点 D:\远见\程序-旧-20260923-134042）
data-dir.txt：已恢复为 D:/远见/数据
首次启动耗时：3.35s（进程拉起 → /api/app/version 可用）
构建：.venv-build + PyInstaller（build/build_windows.ps1 的 spec），耗时 67.2s；旧 dist 挪走不删（dist-旧-20260923-133658）
安装脚本：build-artifacts/lead_install_round11.py（构建产物与安装后逐字节校验 + 回滚点保留，exit=0）

（原始输出：build-artifacts/qa-logs/_sg4_newbuild.txt / _sg4_install.txt / _sg4_hashes.txt）
```

### 2.6 真实数据未被业务写入

```
只读快照（真库 D:\远见\数据\data\yuanjian.db，约 1.06 GB）：
  forecasts=8737  resolutions=0  forecast_versions=19400  personal_impacts=119339
  judgments=79117  notification_log=81352  audit_log=19527
  external_items=111586  situation_events=1049  event_clusters=82386
  schema_migrations = [1,2,3,4,5,6,7,8]

与 v1.5.1 验收记录（2.6）逐表对照：
· 账目 / 判定类表 **稳定**：forecasts / resolutions / forecast_versions / personal_impacts /
  notification_log 逐表**完全一致**；judgments +1、audit_log +1
  —— 系两版之间后台调度器跑过一次（**不是** v1.5.2 升级写入：本版无写迁移，
  升级只做文件替换、烟测只调只读接口，见 2.3）。
· 仅**采集类**表增长（后台正常补抓所致，非升级写入）：
  external_items  110673 → 111586（+913）
  situation_events    981 →   1049（+68）
  event_clusters    81624 →  82386（+762）
· 库文件 1,055,752,192 → 1,057,787,904 B（+2,035,712 B，WAL 落盘 + 采集增长）。

结论：与预期一致 —— **v1.5.2 不在真库上做任何业务写**；除常规采集增长与一次后台调度事件外，
账目与判定表未被写入。

（原始输出：build-artifacts/qa-logs/_sg4_db_post.txt）
```

> 口径说明（如实）：本版**没有**像 v1.5.1 那样在"升级前/后"各取一次同会话快照，上面是与
> **v1.5.1 记录值**的跨版对照；两处 +1 已解释，方向不改变结论。

---

## 三、已知局限（写下来，不假装不存在）

### 本版新增 / 收窄

1. **采集 120s 预算是「按源」不是「按条目」**：单个源一旦**开始**，它的**条目处理仍会整批跑完**
   （单条目 ≈2.7s）。所以"上界 = 预算 + 单源上界"，**单轮仍可能较长**（真机实测：
   采集段 143s 只抓了 2 个源 —— 长耗时主要在**在途源**里）。本版只修了"新源不再无限开"这一半，
   "把单源整批切成按条目"留给 **v1.5.3（候选③）**。
2. **首页读路径本身偏慢，本版未处理**：热页缓存（`.pytest_cache` 级别之外）实测
   **p50 3.4–5.3s**（库 1GB：`event_clusters`≈82k × `personal_impacts`≈119k × `judgments`≈79k 三表 join +
   相关子查询）。这是**读路径本身**的成本，不是采集造成的。列为 **v1.5.3 候选①**（索引/查询重写）。
   旁证：离线只读复现里，空闲无采集时首页读 p50=3.23s / p95=4.45s，采集期 p50=3.35~3.70s /
   p95=4.32~8.12s，**读失败 0 次** —— 采集**没有**把读锁死。
3. **冷启动"卡在哪一阶段"仍未在真机坐实（本版门禁判据 A）**：早期观测到的"冷启动期间首页读 30/40/90s 超时"
   是**既有、非回归**行为（v1.5.0 起已用真机 A/B 坐实），**不设绝对阈值** —— 门禁只看
   "**写突发结束后是否恢复 200**"（上一版 2.6 口径）。把它**拆成"哪个阶段"**需要真实冷启动自记录阶段，
   离线只读复现够不到（热页缓存 + 远程用替身）⇒ 列为 **v1.5.3 候选②**（让真实冷启动自记阶段）。
4. **暂停分支缺少显式变异对照**：④ 的门控已由 `tests/test_diagnostics.py` 覆盖（关闭+残留 / 重开即上报），
   但"暂停分支是否有牙"目前靠手跑 ⇒ 列为 **v1.5.3 候选④**。
5. **`WITHOUT ROWID` 估算路径已补测（本版销账）**：v1.5.1 三-2 的"未实测覆盖"在本版
   `test_retention_guards.py::WithoutRowidEstimateTests` 与 e2e S10 两处落地。

### 沿用（未变，逐项保留）

6. **交付版「单表 > 500MB」护栏是估算值，不是精确阈值**：交付件无 `dbstat`，用
   「逐表行数 × 均匀开窗采样平均行宽」估算（实测 −8%~−18%，保守系数把方向掰向**偏高**）。
   真实约 **435MB** 的表就可能触发该分支 —— 方向安全（清理幂等 + 6 小时节流），但**不再精确**；
   `table_size_source` 会如实说明是精确还是估算。**主护栏（库文件 vs 2048MB）不受影响。**
7. **后台任务失败日志含本机绝对路径**（未改）：`radar_scheduler._execute` 失败按 `exc_info=True`
   写完整堆栈进 `logs\yuanjian.log`。**诊断面板的状态表不含路径**；日志本机文件、不上传，
   由 `RotatingFileHandler(maxBytes=1MB, backupCount=5)` 封顶约 6MB。
8. **`signals.create_candidate` 仍收空串**（未改）：它把该值当**相对日期的参照点**，改 `now` 会动告警定级，
   故只修写入端、不动这里。
9. **`audit_log` 里并存两套时间助手**（未统一，纯历史包袱，无行为影响）。
10. **真库存量异常时间形状不回写**：护栏只保证"不误删"，**不做 backfill**（回写等于伪造发布时间）。
11. **态势层不做跨源同一事件合并**：`event_id` 带源前缀，不同源的同一场地震各占一个点。
12. **AARO / 天外实体源不可采**；**GDELT 源产出为 0**（退避把重试成本指数拉开，网络恢复会自己回来）。
13. **冷启动写突发期首页接口短时不响应**（既有行为，v1.5.0 起 A/B 坐实非回归）：写突发一结束即恢复 200
    （2.9~4.7 s）。本版**用 ⑤ 缩窄了"突发"的成因**（新源不再无限开），但**未改读路径**（见上 2）。
14. **真库那台 `daily_budget` 仍是 2000**：显式存过该值、端点免费，**按设计不覆盖用户值**（产品决策）。
15. **首页上限改动是"回文档"而非"改文档"**（沿用 v1.5.1）：若将来产品确要更多条，应改文档+改断言。

---

## 四、复现方式

```powershell
cd "D:\远见\源码"
$env:PYTHONPATH='src'; $env:PYTHONWARNINGS='error::ResourceWarning'

# 回归必须用打包基线解释器（原因见 README「单元测试」）：它才是交付件真正带的 SQLite
.\.venv-build\Scripts\python.exe -m unittest discover -s tests        # 全量三轮：本版四轮一致 810 OK（skipped=3）
c:\python314\python.exe       -m unittest discover -s tests -v        # 第二个解释器，各跑一次留原始输出

# 发布闸门
python tools\privacy_scan.py --committed

# 三层验证（需已安装产物 D:\远见\程序）
python build-artifacts\lead_verify_installed_v152.py    # 内容级（需 PyInstaller 的 archive readers）
python build-artifacts\lead_smoke_v152.py               # 启动烟测（无头：YUANJIAN_HEADLESS=1 + YUANJIAN_BACKGROUND=1 + --background）
python build-artifacts\lead_verify_v152_e2e.py          # 行为级（临时库 + 回环，须带 PYTHONPATH=src）
python build-artifacts\lead_install_round11.py          # 安装/回滚 + 装后与构建产物逐字节校验
```

> 构建期脚本（`build-artifacts/`、`build/` 下的临时文件）未入库，与仓库既有惯例一致；
> 但**本版新增的变异对照已移入 `tests/`**（见 2.2），克隆仓库后随 `tests/` 自动可跑。
