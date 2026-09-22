# 卡珊德拉的哀歌（内部代号「远见」） v1.5.1 验证记录

**日期**：2026-09-21
**版本**：1.5.1（静默失败治理 + 远程付费账目纠错 + 交付环境里真能工作的单表护栏 + 首页条数回承诺 + 前端重复提交 + 图标改版）
**分支 / 提交**：本地 `main`，顶为 `ba5fa4b`（**7 个提交均未推送**）
```
90526c3  fix(v1.5.1): 图标改版 + 抓取层 gzip/SSL 修复 + 首页上限回归承诺
8039395  refactor(v1.5.1): 移除 gzip 重构后的死代码
50ceb37  fix(v1.5.1): 时间列护栏统一 + 静默失败治理 + 关机不再可能悄悄花钱
6922136  fix(v1.5.1): 让「单表 > 500MB」保留期护栏在交付环境真的能工作
ea246ee  docs(v1.5.1): 使用说明升版 + README 补变异对照说明
d467006  fix(v1.5.1): 修前端监听器泄漏导致的重复提交 + 转义 + 诊断页显示远程真实状态
ba5fa4b  fix(v1.5.1): 远程付费三条 —— 预算漏算重试、默认上限 200、加连续失败熔断
```
- 「未推送」的核实口径：本地**没有任何** remote-tracking 引用（`refs/remotes` 为空），
  `main` 的配置项显示 `[origin/main: gone]` ⇒ **不是"落后于远端"，而是远端根本没有这些提交**。
  故第二段 push 需用 `git push -u origin main`（远端仓库为
  `https://github.com/q719563786/Cassandras-Lament.git`，与 v1.4.1 改名后一致）。
- 另一本地分支 `mystique-theme`（`3d8f1ec`）是 v1.5.0 的验证提交，**是 `main` 的祖先**、未推送、保留不删。
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
| ⑦ 单表 500MB 护栏 | `dbstat` 精确优先 → 缺失则**估算** → 都失败标 `unavailable`；新增 `largest_table_source`；退化/不可用各记 warning；诊断面板新增 `table_size_source` | 交付件 `sqlite3.dll` **未编译 `dbstat`** ⇒ 该护栏在用户手上**静默失效**（既有问题，非本版引入）。见 2.7 实测表 | `retention.py:_largest_table_bytes` |
| ⑧ 前端重复提交 | 卡片动作绑定改为「先按引用 `removeEventListener` 再 add」；`title` 属性转义；诊断页新增**远程真实状态条** | 今日页每 45 秒自刷 ⇒ 监听器**叠加**，点一次确认会重复渲染 + 重复 POST。状态条：此前鉴权失效后程序全程本机跑，界面却看着一切正常 | `ui_core.js` / `views/today.js` / `views/cluster.js` / `views/diag.js` |
| ⑨ 远程付费三条 | 预算改**按次记账**（5 个出口各计一次）；默认上限 **2000→200**；新增**连续失败熔断**（阈值 5）+ **半开探测**（每 15 分钟放行一条） | 预算只统计 `finished_at IS NOT NULL`，而重试分支不写该字段 ⇒ **重试完全不计入预算**，一天可发约 **2400 次**付费调用（预算写 2000）。2000 是照**免费**端点定的，对付费端点太宽松 | `remote_ai.py` |
| ⑩ 文档 | 使用说明升版 + 「主功能导航」「通知节流」；README 钉死回归解释器并澄清变异对照不在 `tests/` 下 | 见 2.4 | `使用说明.md` / `README.md` |

**schema**：本版**无 schema 迁移**（无新表、无新列、无迁移版本）。
**外观**：`theme-mystique.css` **逐字节未动**（它是交付件）。

---

## 二、验收数据

> ⏳ **本节待第二段（代码冻结后）填入实测值。** 计划见「四、复现方式」。

### 2.1 测试

```
（待填：`.venv-build`（打包基线，3.14.5 / SQLite 3.50.4 / 无 dbstat）全量三轮，逐轮原始输出）
（待填：第二个解释器 `c:\python314` 的一轮原始输出）
```

- 计数沿革（供第二段核对）：`734`（`90526c3`）→ `761`（`50ceb37`）→ `767`（`6922136`）→ `785`（`ba5fa4b`）
  → **待实测**（`d467006` 新增 3 个真跑 JS 模块）。

### 2.2 变异对照（证明断言有牙）

```
（待填：逐条列出本版新增/沿用的变异对照，每条写明"改坏什么 → 哪条用例变红 → 还原后 SHA256 逐字节一致"）
```

- 已知：`50ceb37` 记录为 10/10；`ba5fa4b` 记录为 **17 条（原 11 + 新 6）**；`6922136` 新增 1 条（删掉估算兜底 → 3 条变红）。

### 2.3 三层验证（全部在**已安装的产物**上跑）

```
（待填：内容级 / 启动烟测 / 行为级，脚本与结果）
```

### 2.4 发布闸门

```
（待填：tools/privacy_scan.py --committed 的 committed_files / safe / blocked / findings / exit）
```

### 2.5 构建与安装

```
（待填：exe 路径与字节数 / exe sha256 / theme-mystique.css sha256（须与 v1.5.0 逐字节一致）/ 安装位置 / 回滚点 / 首次启动耗时）
```

### 2.6 真实数据未被写入

```
（待填：与升级前备份逐表比对；本版无 schema 迁移，预期"除常规采集增长外无写入"）
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
12. **`settings.js` 的输入框兜底仍写死 `?? 2000`**：与新默认 200 不一致（前端，另派）。
13. **真库那台 `daily_budget` 仍是 2000**：它显式存过该值且端点是免费的 Agnes，**按设计不覆盖用户值**。
    要连"已存过"的一起收紧需要一次写库迁移，属产品决策，本轮不做。
14. **首页上限改动是"回文档"而非"改文档"**：把代码从 50 改回 3。若将来产品上确实想要更多条，
    应改文档 + 改断言，而不是再让代码漂走。

---

## 四、复现方式

```powershell
cd "D:\远见\源码"
$env:PYTHONPATH='src'; $env:PYTHONWARNINGS='error::ResourceWarning'

# 回归必须用打包基线解释器（原因见 README「单元测试」）：它才是交付件真正带的 SQLite
.\.venv-build\Scripts\python.exe -m unittest discover -s tests -v     # 第一段留空，第二段填实测
c:\python314\python.exe       -m unittest discover -s tests -v        # 第二个解释器，各跑一次留原始输出

# 发布闸门
python tools\privacy_scan.py --committed

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
> 因此**从克隆仓库直接跑 `tests/` 能自动跑到的变异对照为 0** —— 它们是交付流程里的人工步骤。
