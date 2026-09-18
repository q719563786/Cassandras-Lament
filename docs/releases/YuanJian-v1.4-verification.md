# 卡珊德拉的哀歌（内部代号「远见」） v1.4 / v1.4.1 验证记录

**日期**：2026-09-18 ~ 2026-09-19
**版本**：1.4.0（S0→S3 一次性整改）→ 1.4.1（对外改名）
**依据**：《远见 v1.4 整改任务书》（三轮设计审计 + 对真实数据库的只读核查）

> 命名说明：本项目对外正式名为 **卡珊德拉的哀歌 / Cassandra's Lament**，内部代号「远见」（YuanJian）。
> 本文件按仓库既有习惯沿用 `YuanJian-` 前缀的文件名。

---

## 一、起点：真库只读核查

| 指标 | 实测 |
|------|------|
| 预测账本行数 | 8,607 |
| 已结算（`resolutions`） | **0**（账本自建立起从未结算过） |
| 逾期未结算 | 1,691 |
| 生成速度 | 约 391 条/天 |
| `base_rate` 非空 | 0 |
| `confirmed_by` 取值 | 全部 `'unknown'` |
| 卡片里 `observable_signals` 非空 | **0 条** |
| L4 的 145 条里 | 128 条挤在同一个分值 0.675 |

结论：闭环从未通电。而结算被设计成"逐条三次交互"，跟上生成速度需要每天 1,174 次交互 ——
**这是吞吐量问题，不是使用者的纪律问题。**

---

## 二、改了什么（S0→S3）

| 批次 | 编号 | 改动 |
|------|------|------|
| S0 | R-01 | 批量结算（按类别/到期区间/状态，一次事务、失败整批回滚；默认结果 `indeterminate`；新增 `resolved_by`） |
| S0 | R-02 | 到期自动归档（默认关闭，7–180 天可配） |
| S0 | R-03 | 校准面板如实标注；`excluded_total` 给出三类构成且可加总 |
| S1 | R-04 | 主指标与基准率只采 `confirmed_by='user'`，其余来源分列不合并 |
| S1 | R-05 | 关注度分数去掉与来源数共线的 `confidence`，阈值按真库分布重定 |
| S2 | R-06 | 删掉两项恒真检查（日期与判定依据此前在校验自己写下的模板文本） |
| S2 | R-07 | 禁用词只检查生成部分，新闻原标题单独落 `source_headline` |
| S2 | R-08 | 可观测信号必须事件级特化；两条入账路径同闸 |
| S2 | R-09 | 结算改为主信号 + 窗口期内首次观测；窗口前已成立的判为既成事实 |
| S3 | R-10 | 跨域同文只算一个独立声音，并真正影响 E 级 |
| S3 | R-11 | 趋势阈值按比较次数做多重比较校正；新增 rising 占比健康指标 |
| S3 | R-12 | 账本补 `no_delete` + `created_at`；`resolutions` 冗余存 `category`/`confirmed_by`；purge 由删父行改置 `void` |
| S3 | R-14 | 首页空状态口径统一为「关注度」 |
| S3 | R-15 | 诊断面板显示证据等级分布 + E3/E4 不可达的明示 |
| S3 | R-16 | 不再为 L1/L2 生成候选预测 |
| S3 | R-17 | 固化机构名匹配的过配行为 |
| — | R-13 | **未做**（孤儿版本行处置属产品决策，任务书要求本次不执行） |

**schema v6**：`forecasts.resolved_by` / `created_at`、`resolutions.category` / `confirmed_by`、
`event_clusters.source_domains`、`forecasts_no_delete` 触发器。历史行的 `created_at` **不回填**。

---

## 三、验收数据

### 3.1 测试

```
Ran 675 tests in ~160s      × 3 轮
OK (skipped=2)              （ResourceWarning 视为错误）
```

新增 5 个测试模块，68 项：

| 模块 | 项数 | 覆盖 |
|------|------|------|
| `test_forecast_batch.py` | 12 | R-01 / R-02（含原子性回滚、默认结果、审计留痕） |
| `test_calibration_honesty.py` | 12 | R-03 / R-04（含 calib.js 的 node 真跑文案断言） |
| `test_falsifiability_gate.py` | 16 | R-06 ~ R-09 |
| `test_attention_and_volume.py` | 6 | R-05 / R-16 |
| `test_fidelity_sources.py` | 22 | R-10 / R-11 / R-12 / R-14 / R-15 / R-17 |

### 3.2 变异对照（证明断言有牙）

`build-artifacts/v14_mutation_controls.py` —— 把 src 拷到临时目录、逐条把改动**改坏**、
用同一套测试跑，**必须变红**。

```
共 15 条变异，15 条证明断言有牙
```

两条关键对照：

| 变异 | 结果 |
|------|------|
| 批量默认结果改成 `not_occurred` | 断言变红；行为级探针实测污染：`brier_score` 被写成 0.4225、误报率被污染成 **1.0** |
| 把 `confidence` 加回 `base_score` 并还原旧阈值 | "关注度等级不再随来源数上升"的断言变红 |

变异对照顺带**修掉了两个探针缺陷**：R-04 的变异最初打错位置（被测逻辑在
`calibration_summary` 的 SELECT 里，不在 `base_rate_for_category`）；只拷 `src/` 的变异树
测不了"读源码文本"的断言（测试用 `__file__` 定位到真实仓库）。已改为连 `tests/` 一起复制。

### 3.3 三层验证（在**已安装的产物**上跑）

| 层 | 脚本 | 结果 |
|----|------|------|
| 内容级（解出 exe 里的 PYZ） | `build-artifacts/lead_verify_installed_v14.py` | 31 个 Python 标识符全在 + 26 项静态资源断言全 OK |
| 启动烟测（无窗口真跑，只读接口） | `build-artifacts/lead_smoke_v14.py` | 27 项全过；跑完 `POST /api/shutdown` 正常退出 |
| 行为级（临时库 + 从库里读回 + 变异对照） | `build-artifacts/lead_verify_v14_e2e.py` | 32 项全过 |

### 3.4 发布闸门

```
tools/privacy_scan.py --committed   →   safe=True  blocked=0  findings=0
```

### 3.5 构建与安装

| 项 | 值 |
|----|-----|
| 产物 | `dist/YuanJian/YuanJian.exe`（onedir） |
| 安装位置 | `D:\远见\程序` |
| `data-dir.txt` | `D:/远见/数据`（升级后保留） |
| 回滚点 | `D:\远见\程序-旧-<时间戳>`（保留最新一份） |

### 3.6 真实数据未被写入

升级前后逐项比对：

| 项 | 升级前 | 升级后 |
|----|--------|--------|
| `forecasts` | 8607 | 8607 |
| `resolutions` | 0 | 0 |
| `forecast_versions` | 19270 | 19270 |
| `created_at` 非空 | 0 | 0 |
| `resolved_by != 'unknown'` | 0 | 0 |

唯一写入是预期内的 schema v6 迁移（加列、加触发器、`schema_migrations` 多一行）。

---

## 四、已知局限（写下来，不假装不存在）

1. **R-13 未做**：10,663 条 `forecast_versions` 孤儿行（占该表 55%）仍在库里，
   受不可变触发器保护删不掉。新的孤儿已被 `no_delete` 堵住。
2. **老事件簇的来源数为未知**：`source_domains` 是新增列，迁移不回填，老簇会显示
   "来源数未知、同文转载未知"，等该簇下次有新条目进来时重算恢复。**不硬算、不编 0。**
3. **本地模板路径的候选会大面积停在"待补充"**（R-08 的直接后果）：模板信号不含本事件实体。
   这是设计意图（账本里不该装"分类平均命题"），但会让近期入账量显著下降。
4. **L4 变多**：换阈值后 L4 占比由 0.12% 升到约 2.2%，因此待人工确认的条目变多；
   与 R-16（不再生成 L1/L2）合并看总量是下降的。
5. 校准面板首次加载要做一次历史命题扫描（8,607 条），有可感延迟（本地读取，非阻塞）。

---

## 五、复现方式

```powershell
cd "D:\远见\源码"
$env:PYTHONPATH='src'; $env:PYTHONWARNINGS='error::ResourceWarning'
python -m unittest discover -s tests -v          # 675 项 × 3 轮
python build-artifacts\v14_mutation_controls.py  # 15 条变异对照（需 build-artifacts/，未入库）
```
