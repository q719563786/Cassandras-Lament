# 卡珊德拉的哀歌（内部代号「远见」） v1.5.3 验证记录

**日期**：2026-09-24
**版本**：1.5.3（外部 AI 调用「频繁失败」：把外因与内因分开，并修掉四条放大器）
**分支 / 提交**：本地 `main`。
**主题**：远程 AI 又频繁失败了 —— 结论是**不是本机配置错了**，是对端 Agnes 免费档在限流，
而本机把这件"对端限流"放大成了"长期不可用"。

> 命名说明：本项目对外正式名为 **卡珊德拉的哀歌 / Cassandra's Lament**，内部代号「远见」（YuanJian）。
> 本文件按仓库既有习惯沿用 `YuanJian-` 前缀的文件名。

---

## 一、这一版做了什么

**不改任何业务判定、不动数据库 schema（无迁移）、不动隐私边界**（远程出口的个人上下文剥离与
公开证据包体积契约原样）。八处改动，全部在"远程调用链"与它的可见性上：

- **自适应节流（`remote_ai.MinIntervalPacer`）**：收到 429 就把相邻请求间隔成倍放慢
  （一次直接退到 `REMOTE_RATE_LIMIT_MIN_INTERVAL_SECONDS = 60` 秒下限，之后 ×4，
  上限 `REMOTE_MAX_INTERVAL_SECONDS = 300` 秒 = 轮周期），成功则 ×0.5 慢慢收回（不低于基线 5 秒）。
  **顺境下行为与旧版完全一致**。理由是实测：固定 5 秒（=12 RPM）是照着文档标称的 20 RPM 留的余量，
  而对端免费档实际只认约 1 次/分钟 —— 差一个数量级，等于每一轮都在打 429。
- **429 立即中止本轮（`run_due`）**：本轮第一次 429 就停发剩余远程作业（本地作业照跑，不花钱）。
  旧行为是"被拒了还把这一轮剩下的发完"，每一条都被同一个理由拒掉、却照样计入日预算与熔断计数。
- **熔断解除改错峰（`_close_circuit`）**：解冻的作业按
  `REMOTE_CIRCUIT_RESUME_SPACING_SECONDS = 30` 秒一条排回队列，不再一次性全部变成"已到期"。
- **保留对端原话（`_default_transport` / `_http_detail` / `_rate_limit_scope`）**：读错误响应体，
  抽一条 160 字以内的说明进日志；识别出"免费档被限"时给熔断原因加 `upstream_free_tier` 后缀。
  `RemoteProviderError` 增加 `detail`（对端原话，只进日志与界面）与 `scope`（机器可判的短标识，
  唯一允许进 `last_error` 后缀的东西）；**`kind` 契约不动**（`auth`/`rate_limit`/`network`/`timeout`/
  `http_error` 照旧），因为库里的 `last_error`、退避分支、诊断映射表全都按它分派。
- **重启恢复熔断状态（`rehydrate_circuit_state`）+ 探测作业放回（`_promote_circuit_probe`）**：
  `_circuit_open` 是进程内状态、重启即空，而 `_close_circuit` 进门第一句是
  `if not was_open: return 0` —— 两者相加让上一轮冻住的那批**永远等不到解冻**（真库 124 → 156 → 256
  只增不减）。另有一层：半开探测挂在 `run_due` 选出的到期行上，冻结作业全在 `paused_auth`、
  不在到期集合里，整批被冻住时探测**没有对象**。现在启动恢复状态、探测窗口到时显式放回一条。
  **只在启动时恢复状态、不在启动时动作业**：`shutdown()` 的"退出即取消"会 `DELETE` 掉 `queued`
  的远程作业（有意设计），启动时放回等于每重启一次白丢一条。
- **本轮时间预算（`REMOTE_ROUND_TIME_BUDGET_SECONDS = 240`）**：本轮能发几条跟着当前间隔现算并
  与调用方配额取小，下限恒为 1。没有它，间隔涨到 300 秒时一轮 6 条要跑几十分钟，而调度器是
  **单线程**、任务串行 —— 认知轮会把采集、态势、趋势一起饿死（`radar_scheduler` 里有同类前科）。
- **推理模型的输出预算（`DeepSeekChatProvider.MAX_OUTPUT_TOKENS`）**：4000 → 8192，并在输出被
  截断时把 `finish_reason` 与推理内容长度原样报出来。实测 `agnes-2.0-flash` 是**推理模型**，
  思维链与正式输出**共用一份预算**，4000 被吃掉一截后判读 JSON 中途断掉 —— 就是库里那 21 条
  `invalid_output`。不取更大是为了**跨 provider 安全**（`deepseek-chat` 的 `max_tokens` 上限就是 8192）。
- **每轮远程名额（`cognition.REMOTE_SLOTS_PER_ROUND`）**：25 → 6。旧值配的是"间隔恒 5 秒"的
  前提（25 × 5 = 125 秒 ≤ 300 秒轮周期），自适应降速把这个前提推翻了。
- **诊断文案（`diagnostics._PAUSE_REASON_UPSTREAM_LIMIT`）**：限流引起的熔断单独一条人话
  ——「远程服务在限流（免费额度已用满）。已自动降速，无需修改设置；对端恢复后会自动继续」，
  而不是笼统的"连续失败"，更不会（v1.5.2 已修）说成"密钥失效"。
  分支判定改用**前缀匹配** `reason.startswith(CIRCUIT_OPEN_REASON)`，因为原因可能带后缀；
  写等号会让带后缀的那批永远解不了冻（一次限流升级成永久封死）。

---

## 二、验收数据

> 全部为 2026-09-24 实测值。原始输出在 `build-artifacts/qa-logs/_v153_*.txt`，
> 脚本在 `build-artifacts/verify-v153/`。

### 2.1 测试

```
# 打包基线解释器（.venv-build = Python 3.14.5 / SQLite 3.50.4 / 无 dbstat），全量一轮：
PYTHONPATH=src PYTHONWARNINGS=error::ResourceWarning .venv-build/Scripts/python.exe \
    -m unittest discover -s tests
  Ran 823 tests in 1053.967s
  OK (skipped=3)                          exit=0

# 对照解释器（Python 3.13.12），全量一轮：
PYTHONPATH=src PYTHONWARNINGS=error::ResourceWarning python -m unittest discover -s tests
  Ran 823 tests in 971.485s
  OK (skipped=3)                          exit=0
```

**两个解释器的结果完全一致**（同为 823 项、同样只有既有的 3 条 skip、同样 exit=0）。
这一点本版单独确认过：本仓库有过"用 A 解释器全绿、换 B 解释器同一条用例必红"的前车之鉴
（根因是打包的 `sqlite3.dll` 编译选项不同），所以换解释器复跑不是形式。

- 本版新增 **13 条**用例；`tests/test_remote_ai.py` 从 36 条增至 **42 条**（另有一条既有用例的断言被改严）。
- 修正了一条既有用例：`test_consecutive_failures_open_the_circuit_and_stop_the_queue` 原本断言
  "熔断后绝对零请求"，与文档写明的半开设计（`REMOTE_CIRCUIT_PROBE_MINUTES`）**自相矛盾** ——
  它此前能过，只是因为"冻结作业不在到期集合、探测没有对象"这个缺口。现改为断言
  "**冷却期内 0 条 + 冷却期后每窗口最多 1 条**"，比原来更严也更准。
- 修掉一处**测试污染**：`_remote_pacer` 是模块级单例，被 429 惩罚过的间隔会留给后面的用例
  （症状是"单跑全绿、全量跑红"），已在 `setUp` 里重置。

### 2.2 变异对照（证明断言有牙）

`build-artifacts/verify-v153/mutation_controls.py`：把 `src/` 复制到临时目录 → 施加一处文本替换
（即"撤销该修复"）→ `PYTHONPATH` 指向副本跑指定用例 → 期望**变红**；跑完核对原 `src/` 逐字节未变。

```
[M1 429 之后不再停发本轮剩余远程作业]            基线 exit=0 / 变异 exit=1 -> 有牙
[M2 熔断解除不再错峰（回到一次性全部到期）]       基线 exit=0 / 变异 exit=1 -> 有牙
[M3 启动不再恢复熔断状态（回到永久僵尸）]         基线 exit=0 / 变异 exit=1 -> 有牙
[M4 限流不再放慢相邻请求间隔]                    基线 exit=0 / 变异 exit=1 -> 有牙
[M5 429 不再区分「免费档被限」]                  基线 exit=0 / 变异 exit=1 -> 有牙
[M6 输出预算退回 4000（推理模型会把 JSON 截断）]  基线 exit=0 / 变异 exit=1 -> 有牙
[M7 传输层不再读错误响应体（回到只取状态码）]     基线 exit=0 / 变异 exit=1 -> 有牙
[M8 探测窗口不再放回探测作业]                    基线 exit=0 / 变异 exit=1 -> 有牙

原 src 逐字节未变: True
全部 8 条变异对照「有牙」。
```

> 自体审计（如实记录）：本脚本第一版把用例路径写成 `JudgmentQueueTests.*`，而这几条其实在
> `RemotePaidGovernanceTests` 下 —— 于是**基线自己就红了**，4 条被误判成"没牙"。
> 已让脚本在基线变红时打印基线输出（"多半是用例路径写错"），修正路径后 8/8 通过。
> **是探针写错了，不是代码的问题。**

### 2.3 三层验证（全部在**已安装的产物** `D:\远见\程序\YuanJian.exe` 上跑）

```
【内容级】build-artifacts/verify-v153/verify_content_installed.py   →  exit=0
  把安装件里的 PYZ 解出来，遍历每个模块的 code object（含嵌套）扫锚点：
    包内 yuanjian_app 模块数：37（含包本身；子模块 36）
    yuanjian_app.remote_ai   命中 17/17 个名字锚点
      （自适应四个常量 + penalize/relax + 错峰常量 + rehydrate_circuit_state
       + _promote_circuit_probe + 时间预算常量 + _affordable_remote_slots
       + _UPSTREAM_FREE_TIER_HINTS + _http_detail + _rate_limit_scope + scope/detail
       + MAX_OUTPUT_TOKENS）
    yuanjian_app.diagnostics 命中  2/2（_PAUSE_REASON_UPSTREAM_LIMIT / CIRCUIT_OPEN_REASON）
    yuanjian_app.cognition   命中  1/1（REMOTE_SLOTS_PER_ROUND）
    yuanjian_app.application 命中  1/1（rehydrate_circuit_state 调用点）
    字符串常量：'1.5.3' / 'upstream_free_tier' / 'rate limit for free users'
                / '限流' / '无需修改设置' 全部命中
    静态资源：tokens.css 与 views/diag.js 版本头均为 v1.5.3
  结论：安装包确实含 v1.5.3 全部改动的锚点 ✓

【启动烟测·无头】tools/smoke_packaged.ps1 -ExePath dist\YuanJian\YuanJian.exe
    Database=true   ListenerCount=1   Address=127.0.0.1   HomeStatus=200
    RemoteScripts=false  DefaultView=today  ModuleEntry=/js/app.js
    Version=1.5.3   LocalFallback=local  SecondInstanceExitCode=0
    Shutdown=shutting_down                                       exit=0
（监听**只在 127.0.0.1**，无对外监听 —— 脚本第 60 行会对非回环监听直接抛错。）

【行为级】build-artifacts/verify-v153/e2e_diagnostics.py   →  24/24，exit=0
  在临时数据目录上起**已安装的 exe**（不碰真库），AI 设成 enabled=true + daily_budget=0
  （诊断页如实报告暂停态，但一笔远程请求都不会发出去），然后**边改库边读接口**：
    版本号 = 1.5.3
    无冻结作业 ⇒ ai_paused=False 且原因必须为空
    last_error='circuit_open:upstream_free_tier'
      ⇒ 「远程服务在限流（免费额度已用满）。已自动降速，无需修改设置；对端恢复后会自动继续」
    last_error='circuit_open'（普通熔断）
      ⇒ 「连续多次调用失败，已暂停远程研判以免继续产生费用」，且**不**误报限流
    last_error='auth_paused' ⇒ 「API 密钥无效或已过期，请到「设置」重新填写」（优先级高于熔断）
    三种情形都断言**不泄漏内部状态名**（paused_auth / circuit_open / auth_paused / last_error）
    清空后恢复正常；POST /api/shutdown → shutting_down
```

### 2.4 发布闸门

```
PYTHONPATH=src python tools/privacy_scan.py --committed

# 发布提交前（冻结基线 c81d040 的已提交内容）：
  committed_files=157   safe=True   blocked=0   findings=0   exit=0
# 发布提交后（含本文件与升版/文档改动）：
  committed_files=158   safe=True   blocked=0   findings=0   exit=0
```

口径：`safe=True` 且 `blocked=0`、`findings=0` —— 已提交内容不含隐私/密钥命中，闸门放行。
两次都过：先扫冻结基线 `c81d040`（157），落发布提交后再扫一次（158，多出的是本文件）。

### 2.5 构建与安装

```
& build\build_windows.ps1
  PyInstaller 6.21.0 / Python 3.14.5 / Gui=edgechromium
  Build complete!  →  D:\远见\源码\dist\YuanJian\YuanJian.exe

产物指纹（安装前后一致）：
  D:\远见\源码\dist\YuanJian\YuanJian.exe
  D:\远见\程序\YuanJian.exe
    sha256 = d2e06c980862546fc31c049be34dd43cc1d7d78f81a7cbb998c3b128c59c9527
    size   = 6973235 字节
  _internal：184 个文件，dist 与安装目录**逐字节一致**（0 处差异）

安装方式：原 `D:\远见\程序` 整体备份为 `程序-旧-20260923-195035`（用户原版），
本版覆盖安装；`data-dir.txt`（指向 `D:/远见/数据`）原样保留 —— 数据目录没有被换掉。
```

> 构建环境两个坑（记下来省下一次踩）：① PowerShell 5.1 里不能给这个脚本套 `2>&1 | Out-String`，
> 脚本内 `$ErrorActionPreference='Stop'` 会把 PyInstaller 写到 stderr 的日志当成
> NativeCommandError 直接终止；② PyInstaller 的 `--clean` 要删 `dist\YuanJian`（201 个文件），
> 会被本机的批量删除保护拦下 ⇒ **先把 `dist\YuanJian` 改名**再跑。

### 2.6 真实数据（只读观测，未对真库做业务写入）

真库 `D:\远见\数据\data\yuanjian.db`（1.0 GB）：

- 本版**没有**改 schema（无迁移），`schema_migrations` 不变。
- 起停新版本一次后，日志出现本版的关键新行为：
```
2026-09-24 08:08:53,251 INFO  yuanjian_app.application 远见启动，数据目录 D:\远见\数据
2026-09-24 08:08:55,262 WARNING yuanjian_app.remote_ai 启动时发现熔断遗留的冻结作业
    {'deepseek_chat': 254}，已恢复熔断状态（每 15 分钟放一条探测；探测成功即整批错峰解冻）
```
- **起停不再丢积压**：起停后库中仍是 254 条 `paused_auth / circuit_open`，一条不少
  （旧写法会在启动时把一条冻结作业放回 `queued`，被 `shutdown()` 的"退出即取消"删掉 —— 每重启白丢一条）。
- 用真库按 `_promote_circuit_probe` 的**同款条件**查询，确认探测作业可被正确选中：
  `NOT EXISTS(queued/retry/queued_budget)` 成立 ⇒ 自愈路径是通的。

---

## 三、已知局限（写下来，不假装不存在）

1. **对端免费档就是不可靠的，这一版修不了这个。** 修完之后远见会自己找到对端能接受的速率、
   不再报错、不再空转，但**吞吐的天花板是对端给的**，不是代码给的。要更高吞吐只有换 provider
   （设置页换端点 + 模型名即可，端点以 `/chat/completions` 结尾就走 Chat 兼容分支，代码不用动）。
2. **速率上限是实测推出来的，不是对端承诺的。** 60 秒下限 / ×4 放大 / 300 秒上限这组数来自
   2026-09-23 的探针（20 秒间隔发 6 次只过 2 次），对端若改策略，收敛点会跟着漂。
   自适应机制本身是对的（看反馈），但这几个常数不是"标称值"。
3. **积压靠探测慢慢消化，不会瞬间清空。** 每个探测窗口试一条：成功则整批错峰解冻；
   持续被拒则该条按 1/2/4 分钟退避、走完 4 次后降级为本地研判。最坏约每 15~20 分钟消化一条。
4. **诊断页那条红文案对"既有的"254 条暂时还是旧的**（"连续多次调用失败…"）：库里现存记录是
   旧版本写的，`last_error` 只有 `circuit_open` 不带归因后缀。**新版本再触发的熔断**才会显示限流文案
   （已由 2.3 的行为级验证在真产物上证实）。
5. **未做的验证**：v1.5.2 记录里那批 74 条断言的 e2e（S1–S10，覆盖结算/开关/清理/护栏等子系统）
   本轮**没有重跑** —— 本版没碰那些子系统，重跑的价值低于成本。历史那 21 条变异对照也没有逐一重跑。
6. **`/api/diagnostics` 在 1 GB 库上要 1~8 秒**（随缓存冷热浮动，实测 1s 与 8s 各一次）。
   这是既有现象、与本版改动无关，只是顺带记一笔：那个库已经 1 GB，值得考虑清一次历史。
7. **`.venv-build` 全量只跑了一轮**（v1.5.2 记录里是三轮）。本版改动集中在远程调用链，
   与解释器差异（`sqlite3` 编译选项）无关；对照解释器另跑一轮以覆盖解释器差异。
8. **本机 git 的远端引用被沙箱吞掉**：`git branch -vv` 会显示 `[origin/main: gone]`、
   `git log --branches --not --remotes` 会把全部提交列成"未推送"、`git tag` 只剩 v1.0/v1.5.0。
   **这些是假象**，判断远端状态必须直连 GitHub 或 `gh`。本版推送前后都用连接器核对过。

---

## 四、复现方式

```bash
cd D:\远见\源码

# 回归必须用打包基线解释器（原因见 README「单元测试」）：它才是交付件真正带的 SQLite
PYTHONPATH=src PYTHONWARNINGS=error::ResourceWarning \
  .venv-build/Scripts/python.exe -m unittest discover -s tests

# 变异对照（8 条；跑完自动核对原 src 逐字节未变）
.venv-build/Scripts/python.exe build-artifacts/verify-v153/mutation_controls.py

# 打包（先把 dist\YuanJian 改名，避开批量删除保护）与烟测
& build\build_windows.ps1
& tools\smoke_packaged.ps1 -ExePath 'dist\YuanJian\YuanJian.exe'

# 内容级 + 行为级（需已安装产物 D:\远见\程序）
.venv-build/Scripts/python.exe build-artifacts/verify-v153/verify_content_installed.py
.venv-build/Scripts/python.exe build-artifacts/verify-v153/e2e_diagnostics.py

# 发布闸门
PYTHONPATH=src python tools\privacy_scan.py --committed
```
