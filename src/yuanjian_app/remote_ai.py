"""Optional OpenAI Responses adapter and a privacy-safe judgment job queue."""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from dataclasses import replace as _replace

from .judgments import (
    ALLOWED_IMPACT_CATEGORIES,
    InvalidJudgmentError,
    JudgmentResult,
    LocalHeuristicProvider,
    MAX_BUNDLE_CHARACTERS,
    MAX_EVIDENCE_SOURCES,
    repair_judgment,
    validate_judgment,
)
from .retention import read_retention_setting

_logger = logging.getLogger(__name__)


DEFAULT_ENDPOINT = "https://api.openai.com/v1/responses"

DAILY_REMOTE_BUDGET = 200
"""每日远程研判上限的**默认值**（2026-09-15 由 100 提到 2000；2026-09-21 降到 200）。

定 2000 的依据（**已经不成立，留作史料**）：Agnes AI 免费版对文本模型限的是
**20 RPM**（每分钟请求数），**没有每日配额**；所以当时认为真正的稀缺资源是
"每天总共做多少件"，不是"峰值多快"——旧值 100 天/天把用户卡在了每天精确 100 条
（真库 judgment_jobs 9/5–9/8 每天停在 100），而配额其实是白给的。

**2026-09-21 为什么降到 200**：2000 是照着"免费 Agnes"定的，一旦用户把端点换成
**付费** provider（如 `deepseek_chat`），同一个数字就从"白给"变成"一天最多
几千次真实扣费"。默认值必须按**最坏情况**（付费）来定，免费额度宽松的用户自己在
设置页调高即可（0~100000，0 = 关闭远程）。

⚠ **只改默认，不覆盖用户已显式存过的值**：`read_ai_setting()` 只在
`ai_settings` 里**没有** `daily_budget` 这个键时才用本常量；真库已显式存了 2000，
且 2000 在 `MIN_DAILY_BUDGET..MAX_DAILY_BUDGET` 区间内 ⇒ 本机用户改动前后**都是
2000**（实测见 `build-artifacts/scratch-yj-20260921/probe_remote_budget.txt`）。
要连"已存过"的一起收紧，需要一次写库迁移 —— 那是产品决策，本轮不做。

**2026-09-16 的安装后真库数据推翻了这里原来的另一半推断**（原文写"顺序请求、
单次十来秒，结构上到不了 20 RPM"）：15 小时里 succeeded 152 / rate_limit 23，
**13% 的请求被 429 打回**，且那 100 个作业是 6 秒内排进队列、由 `run_due` 一轮
里背靠背连发出去的。顺序请求 ≠ 低瞬时速率——只要单次响应快（`agnes-2.0-flash`
就是秒级），一轮 25 个就能在几秒内把 20 RPM 冲破。所以"每日总量"之外还必须有
**瞬时速率闸**：`REMOTE_MIN_INTERVAL_SECONDS`。两道闸管的不是同一件事，
**不要合并，也不要拿一个去替代另一个**（详见该常量的说明）。
"""

MIN_DAILY_BUDGET = 0
MAX_DAILY_BUDGET = 100000
"""每日上限的可设置区间。0 = 用户明确关闭远程（不再发起任何请求）。
上限给到 10 万是因为真瓶颈是 RPM 与用户自己的判断，不必在软件里再设一道墙。"""

#: 今日远程调用**次数**的落点（`runtime_state`）。**按次**记账 —— 失败重试也算，
#: 因为每次重试都是一次真实付费调用（详见 `_remote_used_today` 的说明）。
REMOTE_USAGE_STATE_KEY = "ai_remote_usage"

#: 连续失败熔断阈值：同一 provider 连续这么多次**非鉴权**失败后，把该 provider
#: 的排队作业统一冻结。复用既有 `paused_auth` 机制，**不另起一套状态**（另起一套
#: 就要同时改 shutdown 的清理范围、requeue 的终态表、诊断面板，那是第二套）。
REMOTE_CIRCUIT_THRESHOLD = 5

#: 熔断打开后每隔这么久放行**一条**探测作业（半开状态）。
#:
#: 没有这条，熔断就等于把 provider **永久封死**：打开时所有排队作业被冻结成
#: `paused_auth`（不在到期集合里），而"成功一次即解除"又要求有一次调用成功 ——
#: 两条加起来的结果是**永远不会有那一次成功**，只能等重启或人工。教科书里的
#: 半开（half-open）就是为这个缝设计的：冷却期过后放一条进去试，成功就整批解冻，
#: 失败就继续冻着并重新计时。成本上界明确：最坏 **每 15 分钟 1 次**调用。
REMOTE_CIRCUIT_PROBE_MINUTES = 15

#: 熔断冻结时写进 `last_error` 的原因串 —— 与 `auth_paused` 区分开：
#: 解除时只解冻这一批，绝不误放行"认证失败"冻结的那批。
#:
#: 2026-09-23 起可能带后缀（`circuit_open:upstream_free_tier`），用来把
#: "对端限流"与"网络不通"在诊断页分开说。**匹配一律用前缀**
#: （`last_error LIKE 'circuit_open%'`），不要写等号 —— 写等号会让带后缀的
#: 那批作业永远解不了冻，把一次限流升级成永久封死。
CIRCUIT_OPEN_REASON = "circuit_open"

REMOTE_CIRCUIT_RESUME_SPACING_SECONDS = 30.0
"""熔断解除时，把解冻的作业按这个间隔**错峰**排回队列（秒）。

没有这一条，解冻就是一次惊群：`_close_circuit` 只把状态改回 `queued`，而
`next_attempt_at` 还停在冻结之前 —— 于是几百条作业**同时**变成"已到期"，下一轮
`run_due` 立刻按 `remote_limit` 成批打出去。

真库证据（2026-09-23）：10:41:26Z 有一次调用侥幸成功 ⇒ 熔断解除 ⇒ 256 条同时
到期 ⇒ 35 秒内 5 条被 429 打回 ⇒ 10:42:05 熔断**再次**打开。冻结的 4 条作业
`next_attempt_at` 恰为成功时刻 +1 分钟（429 专用退避），把这条路径钉死了。
"偶发一次成功 → 惊群 → 立刻再熔断"就是用户看到的"频繁出问题"。

30 秒是按"一轮最多消化多少条"定的：`REMOTE_SLOTS_PER_ROUND`（6）条 × 30 秒
= 180 秒，落在认知轮周期（300 秒）以内，不会一轮压一轮。
"""

REMOTE_ROUND_TIME_BUDGET_SECONDS = 240.0
"""单轮认知扫描里，远程请求**总时长**的预算（秒）。

自适应降速之后，"本轮发几条"必须跟着间隔一起缩，否则间隔涨到 300 秒时一轮
6 条要跑满 30 分钟 —— 调度器是**单线程**、任务串行，认知轮会把采集、态势、
趋势一起饿死（这条在 `radar_scheduler` 里已经有前科：冷启动补抓把态势块饿到
`last_status='never'`）。

实际条数由 `run_due` 现算 `max(1, 预算 / 当前间隔)` 并**与调用方给的配额取小**：

    · 基线 5 秒 → 48 条，**高于任何调用方会传的值**（`cognition` 传 6、
      `run_due` 默认 3），所以顺境下这道闸完全不咬合 —— 它只在自适应降速
      **之后**才起作用，不改变既有的配额语义；
    · 40 秒     → 6 条（`cognition` 的配额，等于刚好不削）；
    · 60 秒     → 4 条；
    · 300 秒    → 1 条。

取 240 而不是更小的值：轮周期 300 秒，留 60 秒给 bundle 构造、本地研判与其它
调度任务。这个数**只在降速后才真的生效**，所以宁可给宽一点、别误伤顺境。

**下限永远是 1**：再慢也得让一条出去，否则熔断的半开探测就永远等不到那"一次
成功"，provider 被永久封死。
"""


def _usage_day(now) -> str:
    """UTC 日期键（`YYYY-MM-DD`）。预算按 UTC 日切，与 `_remote_used_today` 一致。"""
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")

# Agnes AI（免费 OpenAI 兼容接口）默认配置
AGNES_AI_BASE_URL = "https://apihub.agnes-ai.com/v1"
AGNES_AI_CHAT_ENDPOINT = f"{AGNES_AI_BASE_URL}/chat/completions"
AGNES_AI_DEFAULT_MODEL = "agnes-2.0-flash"
AGNES_AI_RPM_LIMIT = 20  # 免费版实际可执行 RPM

REMOTE_MIN_INTERVAL_SECONDS = 5.0
"""远程请求的**最小间隔**（秒）= 12 次/分钟的瞬时速率闸。

**为什么是 12 而不是 20**：Agnes 上限是 20 RPM，取六成是给三类"计划外"请求留余量：
① 429 之后的短退避重试（见 `REMOTE_RATE_LIMIT_BACKOFF_MINUTES`）；
② 用户刚改完设置就触发的下一轮；
③ 同一个 key 上可能存在的其它调用方。
贴着 20 走等于把余量吃成 0，一有抖动就又是 429 —— 而一次 429 的代价（浪费一次
尝试、还要退避）比"每个请求多等几秒"高得多。

**为什么是"补差"而不是固定 sleep 一个值**：语义是 `max(0, 间隔 - 距上次请求已过
的时间)`。单次调用本来就慢（比如耗时 12 秒）时间隔已自然满足，补差为 0，不会再
白等；只有请求发得太密时才真的等待。

**一轮的时间预算（下一个人调大 `cognition.REMOTE_SLOTS_PER_ROUND` 前必须重算）**：

    槽位数 × 最小间隔 ≤ 轮周期
    25 × 5 秒 = 125 秒 ≤ 300 秒（`radar_scheduler` 的 5 分钟轮询）

余下约 175 秒留给 bundle 构造、单次调用本身的开销与本地任务。**若把槽位调到 60 以上
（60 × 5 = 300 秒），一轮就会顶满轮周期**，到时要么调小间隔、要么改轮周期，
否则认知扫描会开始一轮压一轮。

**它与 `daily_budget` 是两个正交的闸，不能互相替代**：这一道管**多快**（每分钟
最多发几次），`daily_budget` 管**多少**（一天最多发几次）。
"""


# ── 自适应降速（2026-09-23）───────────────────────────────────────────────
#
# 上面那个 5 秒是**基线**，不是"对端真正允许的速率"。2026-09-23 对 Agnes AI
# 免费档实测（探针脚本，非推算）：
#
#   · 单发一次，静置两分钟后 → 200，但**耗时 11~23 秒**（对端在排队）；
#   · 紧接着的第二、第三次 → 429，响应体明写
#     "You've reached the API rate limit for free users. Upgrade to a Token Plan"；
#   · 间隔 20 秒单发 6 次（= 3 RPM，只有文档标称 20 RPM 的 1/7）→ 仅 2 次 200；
#   · 并发 4 同时发出 → 1 个 200 + 3 个 429。
#
# 结论：**免费档的实际放行速率约 1 次/分钟，远低于文档标称的 20 RPM**，而且
# 拒绝是"入门即拒"（0.8~1.3 秒返回 429，不是等超时）。固定 5 秒的基线在这种
# 对端上等于**每轮都在打 429**，于是：5 条失败 → 熔断 → 15 分钟后探一条 → 又
# 失败 → …… 真库一天下来 12 次尝试只有 2 次成功，而每次**侥幸成功**又会触发
# 一次惊群重放（见 `_close_circuit`）。这就是"频繁出问题"的机制本身。
#
# 硬编码一个更慢的基线是错的方向：对端空闲时白白牺牲吞吐，对端更严时又不够。
# 所以改成**自适应**：429 就成倍放慢并立刻停发本轮，成功就慢慢收回来。参数在
# 下面四条常量里，由 `MinIntervalPacer.penalize()` / `.relax()` 消费。
REMOTE_RATE_LIMIT_MIN_INTERVAL_SECONDS = 60.0
"""收到 429 后，相邻两次请求的间隔**至少**退到这个值（秒）。

60 秒来自实测：20 秒间隔仍有 2/3 被拒，而单发一次（静置两分钟）能过。取"能过
的那个量级"做下限，比从 5 秒开始一分一分地试要少烧十几个 429。"""

REMOTE_MAX_INTERVAL_SECONDS = 300.0
"""自适应退化的间隔**上限**（秒）= 认知轮周期。

退化到这一步意味着"对端基本不可用"，此时熔断会接手（连扫 5 轮才够阈值，
5 × 300 秒 = 25 分钟），中间不再有任何流量。上限与轮周期取同一个数，是为了让
"一轮最多发 1 条"成为明确的兜底语义，而不是一个没人算过的中间值。"""

REMOTE_INTERVAL_ESCALATION = 4.0
"""每次 429 把间隔放大的倍数。

取 4 而不是 2：从基线 5 秒出发，2 倍要 5 轮（约 25 分钟）才爬到能过的量级，
期间每轮都烧掉一个 429；4 倍只需 3 轮（5 → 60 → 240 → 300），约 15 分钟。
收敛快比收敛平滑更重要——每多一轮就是一次真实被拒的请求。"""

REMOTE_INTERVAL_RECOVERY = 0.5
"""每成功一次，把间隔往回收一半（下限是基线）。

与放大的不对称（×4 放慢 / ×0.5 收回）是刻意的：被拒的代价是一次白烧的请求
+ 一次熔断计数，成功的代价只是慢一点，所以"惩罚要快、奖励要慢"——否则一次
侥幸成功就把间隔打回 5 秒，下一轮又是 429，形成来回振荡。"""


REMOTE_RATE_LIMIT_BACKOFF_MINUTES = (1, 2, 4)
"""HTTP 429（限流）专用的退避序列，按第几次尝试取值（分钟）。

**为什么要与普通失败分开**：429 的语义是"你发太快了"，不是"请求本身有问题"。
普通失败（network / timeout / http_error）用 15/30/60/120 分钟是合理的——那是在等
对端恢复；但对 429，长退避恰恰在**加重**问题：我们本来就是发太快才被限，被推到
15 分钟后又和积压的作业一起到期，下一轮就是一次新的突发。真库里 23 个 rate_limit
作业正是这样被推到 +15 分钟，净吞吐反而被压住。

**为什么是 1/2/4 而不是立刻重试**：① 立刻重试多半还在被限的那个 60 秒窗口里，
只是把一次 429 变成另一次 429，白烧一次尝试（`used` 会 +1，占日预算）；
② 仍然指数增长，配合上面的最小间隔（重试请求同样受 5 秒间距约束），
即使整批作业同时被限也不会形成新的突发；③ 外层 `MAX_REMOTE_RETRIES = 4` 保证
最迟第 4 次失败就降级本地，不会变成忙等——整条链路合计约 7 分钟收敛，
而旧序列要走满 105 分钟。
"""


class RateLimiter:
    """滑动窗口频率限制器：确保60秒窗口内不超过 rpm 个请求。

    用于 Agnes AI 等有明确 RPM 限制的免费接口，超出时阻塞等待而非丢弃。
    线程安全，可被多个 provider 共享。
    """

    def __init__(self, rpm: int = 20):
        self.rpm = max(1, int(rpm))
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def acquire(self):
        """获取一个请求许可；若当前窗口已满，阻塞到最老请求过期。"""
        while True:
            with self._lock:
                now = time.monotonic()
                self._timestamps = [t for t in self._timestamps if now - t < 60.0]
                if len(self._timestamps) < self.rpm:
                    self._timestamps.append(now)
                    return
                wait = 60.0 - (now - self._timestamps[0]) + 0.05
            if wait > 0:
                time.sleep(min(wait, 5.0))


# 模块级共享限流器：Agnes AI 免费版 20 RPM
_agnes_ai_limiter = RateLimiter(AGNES_AI_RPM_LIMIT)


class MinIntervalPacer:
    """补差式最小间隔：保证相邻两次调用之间至少隔 `interval` 秒。

    与 `RateLimiter` 的分工：`RateLimiter` 约束**60 秒窗口内的总次数**，允许前 20 次
    一拥而上、之后空等；这个类约束的是**相邻两次的间距**，从第一个请求起就把突发
    摊平。真库里的 429 不是"一小时内发多了"，而是"几秒内连着发"——所以要的是后者。

    时钟与 sleep 都取模块级 `time`，因此测试可以用替身时钟整体替换
    （`mock.patch.object(remote_ai, "time", fake)`），不会真睡。

    **自适应部分（2026-09-23 新增）**：`interval` 不再恒等于构造时给的基线，
    而是介乎 `base` 与 `ceiling` 之间的一个可变量。对端回 429 时调
    `penalize()` 成倍放慢，调用成功时调 `relax()` 慢慢收回。理由与实测见
    `REMOTE_RATE_LIMIT_MIN_INTERVAL_SECONDS` 那一段。
    """

    def __init__(
        self,
        interval: float = 0.0,
        *,
        ceiling: float = 0.0,
        escalation: float = 1.0,
        recovery: float = 1.0,
        limit_floor: float = 0.0,
    ):
        self.base = max(0.0, float(interval))
        self.interval = self.base
        #: 退化上限。不传（0）时等于基线 ⇒ 自适应整体关闭，行为与旧版一致
        #: （这正是既有 `MinIntervalPacer(5)` 类测试仍然有效的保证）。
        self.ceiling = max(self.base, float(ceiling))
        self.escalation = max(1.0, float(escalation))
        self.recovery = min(1.0, max(0.0, float(recovery)))
        #: 429 后的间隔**下限**：一次限流就至少退到这里，不从基线一分一分地试。
        self.limit_floor = min(self.ceiling, max(self.base, float(limit_floor)))
        self._last_at = None
        self._lock = threading.Lock()

    def wait(self):
        """距上次调用不足 `interval` 时补足差值；首次调用不等待，`interval<=0` 时不做任何事。"""
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if self._last_at is not None:
                need = self.interval - (now - self._last_at)
                if need > 0:
                    time.sleep(need)
                    now = time.monotonic()
            self._last_at = now

    def penalize(self) -> float:
        """对端回 429：把间隔成倍放大（并至少退到 `limit_floor`），返回新间隔。

        只放大不重置 `_last_at`：刚被拒的那一次本身就是"最后一次请求"，下次
        该等多久要从它算起，否则等于把惩罚白抹掉。
        """
        with self._lock:
            target = max(self.interval * self.escalation, self.limit_floor)
            self.interval = min(self.ceiling, target)
            return self.interval

    def relax(self) -> float:
        """有一次调用成功：把间隔往回收（下限是基线），返回新间隔。"""
        with self._lock:
            if self.interval > self.base:
                self.interval = max(self.base, self.interval * self.recovery)
            return self.interval


# 模块级共享节流器：**所有**远程 HTTP 请求共用一个节奏（不限 agnes 主机）。
#
# 为什么挂在传输层（这里）而不是 `run_due` 的循环里：
#   ① 这里才是真正的网络出口。将来不管多出什么调用方（单条重试、探活按钮），
#      只要它是真的 HTTP 请求就必然经过这里，绕不过去；
#   ② `run_due` 是"要不要发请求"的决策层，测试会给它注入假 transport。节流放在
#      那里会让每个构造 N 个远程作业的用例都真睡 5N 秒（现有一套就多 5 分钟），
#      测试为了跑得快只能把节流关掉——那等于把这道闸的验证一起关掉了。
#
# 本地研判（`LocalHeuristicProvider`）不发 HTTP，天然不受影响。
#
# 自适应参数（2026-09-23）：基线 5 秒只是起点，真区间由对端反馈决定 —— 429 就
# 成倍放慢到最多 `REMOTE_MAX_INTERVAL_SECONDS`，成功就慢慢收回基线。见
# `REMOTE_RATE_LIMIT_MIN_INTERVAL_SECONDS` 的实测依据。
#
# **全局单例，不按 host 分桶**：本机同一时刻只会有一个远程 provider 在用
# （`AiSettingsService.create_remote_provider` 只返回一个），所以不存在
# "把慢档的惩罚误加到快档上"的场景。将来真要多 provider 并行，这里必须改成
# 按 host 分桶，否则一个被限流的会被另一个的健康流量拖慢。
_remote_pacer = MinIntervalPacer(
    REMOTE_MIN_INTERVAL_SECONDS,
    ceiling=REMOTE_MAX_INTERVAL_SECONDS,
    escalation=REMOTE_INTERVAL_ESCALATION,
    recovery=REMOTE_INTERVAL_RECOVERY,
    limit_floor=REMOTE_RATE_LIMIT_MIN_INTERVAL_SECONDS,
)


def _affordable_remote_slots(remote_limit, interval=None) -> int:
    """本轮实际能发的远程条数 = min(配额, 时间预算 / 当前间隔)，**下限恒为 1**。

    见 `REMOTE_ROUND_TIME_BUDGET_SECONDS`：自适应降速把间隔拉到 300 秒之后，
    "一轮 25 条"会变成 125 分钟的轮次，把单线程调度器里排在后面的采集与态势
    任务饿死。下限取 1 而不是 0 —— 归零会让熔断的半开探测一起失效，对端恢复
    了也永远试不出来。`interval` 可显式传入，测试不依赖模块级状态。
    """
    slots = max(1, int(remote_limit))
    if interval is None:
        interval = _remote_pacer.interval
    try:
        interval = float(interval)
    except (TypeError, ValueError):
        return slots
    if interval > 0:
        affordable = int(REMOTE_ROUND_TIME_BUDGET_SECONDS / interval)
        slots = min(slots, max(1, affordable))
    return slots


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _strip_personal_context(bundle):
    """P0-1：远程出口的硬闸——抹掉 bundle 里的个人上下文。

    `PRIVACY.md` 承诺「外部 AI 的唯一输入是 build_public_bundle() 生成的公开
    证据包」。所以远程分支连"本可以被注入"的机会都不留：只要 bundle 上带了
    `personal_context`，这里一律剥成 None 再交给 provider。

    为什么不依赖"上游不注入"：`bundle_loader` 是构造时注入的，只要它被换成一个
    会塞个人数据的实现，请求体就会重新变脏。把闸门放在最靠近出口的地方，才是
    与调用方实现无关的保证。

    本地分支**不走这里** —— 本机个性化正是要用 `personal_context`。
    """
    if bundle is None or not getattr(bundle, "personal_context", None):
        return bundle
    return _replace(bundle, personal_context=None)


def _public_payload_for_request(bundle):
    """远程请求体的唯一准备出口：先硬剥离个人上下文，再校验体积契约。

    这两件事都放在这里、而不是各 provider 自己的 `_request_body` 里，是因为
    它们**必须在所有 provider 上生效**：

    - **剥离**：`PRIVACY.md` 承诺外部 AI 的唯一输入就是公开证据包。之前远程
      分支会把利益地图 / 历史预测 / 用户主动记录的个人近况拼进请求体，而远程
      AI 只贡献 1.11% 的研判（803/72,253），交换比不成立。
    - **体积**：`MAX_BUNDLE_CHARACTERS` / `MAX_EVIDENCE_SOURCES` 此前只有
      `OpenAIResponsesProvider._request_body` 校验，`DeepSeekChatProvider`
      完全没有校验，实测 Chat 分支曾实发 18,985 字符。

    越限抛 `InvalidJudgmentError` 而**不是**裸 `ValueError`：调用方 `run_due`
    对 `InvalidJudgmentError` 的处理是「立即降级 local、不重试」，而裸
    ValueError 会冲出 `run_due` 打断整轮认知，只能被记成 task error。
    """
    public = bundle.to_public_dict()
    # 硬闸：无论调用方怎么构造 bundle，远程请求都不含个人上下文。
    # 用"剥离"而不是 `assert`：assert 在 -O 下会被整个删掉，剥离不会，
    # 而且剥离后后续任何代码都不可能再把它拼回去。
    public.pop("personal_context", None)
    if len(public.get("evidence") or ()) > MAX_EVIDENCE_SOURCES:
        raise InvalidJudgmentError("公开证据包来源超过上限")
    if len(json.dumps(public, ensure_ascii=False)) > MAX_BUNDLE_CHARACTERS:
        raise InvalidJudgmentError("公开证据包字符超过上限")
    return public


def _validate_endpoint(endpoint):
    parts = urlsplit(str(endpoint))
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("AI地址必须是HTTPS公网地址")
    host = parts.hostname.casefold()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("AI地址不能指向本机")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("AI地址不能指向私有网络")
    return str(endpoint)


def _http_detail(status, body_text) -> str:
    """从错误响应体里抽出一条**可诊断**的原因（短串，绝不含密钥）。

    2026-09-23 之前这里根本没有：`_default_transport` 只取 `error.code`，把
    **对端到底说了什么整个丢掉**。后果是 401 与 429 在库里都只剩一个词
    （`auth` / `rate_limit`），用户只能看到"连续多次调用失败"这种话——而真正
    的答案（"You've reached the API rate limit for free users"）就在被丢掉的那
    段字节里。**这才是这个问题拖了这么久没被定位的原因。**

    只取 `error.message`（没有就取 `message`，再没有就取原文），压成单行、
    截断到 160 字。不做任何"猜"——对端怎么说就怎么记。
    """
    text = str(body_text or "").strip()
    if not text:
        return ""
    message = ""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        elif isinstance(error, str):
            message = error
        if not message:
            message = str(payload.get("message") or "")
    if not message:
        message = text
    return " ".join(message.split())[:160]


#: 429 里专指"免费档被限"的措辞（Agnes AI 实测响应体）。
#: 命中的话熔断原因会带上这一层，把"对端限流"与"网络不通"在诊断页分开说 ——
#: 前者不需要用户改任何配置，后者才需要。
_UPSTREAM_FREE_TIER_HINTS = ("rate limit for free users", "token plan")


def _rate_limit_scope(message) -> str:
    """429 的细化归因：免费档被限 → 一个短标识；认不出来 → 空串。"""
    text = str(message or "").casefold()
    if any(hint in text for hint in _UPSTREAM_FREE_TIER_HINTS):
        return "upstream_free_tier"
    return ""


class RemoteProviderError(RuntimeError):
    """远程调用失败。三个字段各管一件事，**不要合并**：

    · `kind` —— 分派用（既有契约）：库里的 `last_error`、退避分支、诊断映射表
      全都按它走。**细化的归因不得改动它。**
    · `detail` —— 对端原话（`_http_detail` 抽出来的，已压成单行、截断 160 字）。
      只进日志与界面，**不进任何被等值匹配的列**。默认空串（网络类失败没有对端
      响应体可说）。绝不包含密钥：只取响应体，不碰请求头。
    · `scope` —— 机器可判的**短标识**（目前只有 `upstream_free_tier`）。它是唯一
      允许写进 `judgment_jobs.last_error` 后缀的东西：那里要的是稳定、可前缀
      匹配的字符串，把对端原话塞进去会让匹配规则变成"看情况"。
    """

    def __init__(self, kind, status=None, detail="", scope=""):
        self.kind = str(kind)
        self.status = status
        self.detail = str(detail or "")
        self.scope = str(scope or "")
        super().__init__(f"{self.kind}:{self.scope}" if self.scope else self.kind)

    @classmethod
    def from_http(cls, status, body_text=""):
        message = _http_detail(status, body_text)
        if status in {401, 403}:
            return cls("auth", status, message)
        if status == 429:
            return cls("rate_limit", status, message, _rate_limit_scope(message))
        return cls("http_error", status, message)


def _result_schema():
    string_array = {"type": "array", "items": {"type": "string"}}
    properties = {
        "fact_summary": {"type": "string"},
        "actors": string_array,
        "causal_chain": string_array,
        "uncertainties": string_array,
        "horizons": string_array,
        "probability_low": {"type": "number", "minimum": 0, "maximum": 1},
        "probability_high": {"type": "number", "minimum": 0, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "supporting_source_ids": string_array,
        "counter_source_ids": string_array,
        "up_triggers": string_array,
        "down_triggers": string_array,
        "impact_categories": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(ALLOWED_IMPACT_CATEGORIES)},
        },
        # GYW framework (《登高望远》): require the provider to emit the
        # five structured legacy fields so the home page can show real
        # stakeholder / constraint / least-resistance / counter-evidence /
        # leading-indicator analysis instead of UI fallback templates.
        # 稿C v2: four new keys carry structured stakeholder/indicator data —
        # beneficiaries / cost_bearers (with evidence_refs for anti-hallucination),
        # historical_parallel (nullable), observable_signals (array of strings).
        "gyw": {
            "type": "object",
            "properties": {
                "stakeholders": {"type": "string"},
                "constraints": {"type": "string"},
                "least_resistance_path": {"type": "string"},
                "counter_evidence": {"type": "string"},
                "leading_indicators": {"type": "string"},
                "beneficiaries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string"},
                            "gain": {"type": "string"},
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["subject", "gain", "evidence_refs"],
                        "additionalProperties": False,
                    },
                },
                "cost_bearers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string"},
                            "cost": {"type": "string"},
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["subject", "cost", "evidence_refs"],
                        "additionalProperties": False,
                    },
                },
                "historical_parallel": {"type": ["string", "null"]},
                "observable_signals": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "stakeholders",
                "constraints",
                "least_resistance_path",
                "counter_evidence",
                "leading_indicators",
                "beneficiaries",
                "cost_bearers",
                "historical_parallel",
                "observable_signals",
            ],
            "additionalProperties": False,
        },
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _default_transport(url, headers, body, timeout):
    host = urlsplit(url).hostname
    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        }
    except OSError as error:
        raise RemoteProviderError("network") from error
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise RemoteProviderError("unsafe_endpoint")
    # 瞬时速率闸：所有远程 HTTP 请求按最小间隔摊平（详见 REMOTE_MIN_INTERVAL_SECONDS）。
    # 与下面那条 Agnes 专用许可一样放在地址闸**之后**：端点还没验过就先等满 5 秒，
    # 是把配额和时间一起浪费掉。
    _remote_pacer.wait()
    # Agnes AI 免费版有 20 RPM 限制，DNS/SSRF校验通过后再获取许可，避免无效端点浪费配额
    if host and "agnes-ai.com" in host.casefold():
        _agnes_ai_limiter.acquire()
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(2_000_000)
    except urllib.error.HTTPError as error:
        # 读错误响应体：这是**唯一**能说明"对端为什么拒"的地方，之前整段丢掉，
        # 导致库里只剩 `rate_limit` 这种没有信息量的词（见 `_http_detail`）。
        # 读失败也不能让原始错误被顶掉，所以整段包在 try 里。
        try:
            body_text = error.read(2048).decode("utf-8", "replace")
        except Exception:
            body_text = ""
        failure = RemoteProviderError.from_http(error.code, body_text)
        _logger.warning(
            "远程请求被拒：HTTP %s kind=%s host=%s 对端说明=%s",
            error.code,
            failure.kind,
            host,
            failure.detail or "(未提供)",
        )
        raise failure from error
    except (urllib.error.URLError, TimeoutError, socket.timeout) as error:
        kind = "timeout" if isinstance(error, (TimeoutError, socket.timeout)) else "network"
        raise RemoteProviderError(kind) from error
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidJudgmentError("远程响应不是有效JSON") from error


class OpenAIResponsesProvider:
    name = "openai_responses"

    def __init__(
        self,
        *,
        model: str,
        token_loader,
        endpoint: str = DEFAULT_ENDPOINT,
        transport=None,
        timeout: int = 60,
    ):
        self.model = str(model or "").strip()
        if not self.model:
            raise ValueError("启用远程AI时必须明确填写模型编号")
        self.endpoint = _validate_endpoint(endpoint)
        self.token_loader = token_loader
        self.transport = transport or _default_transport
        self.timeout = int(timeout)

    def _request_body(self, bundle):
        # 出口闸：体积契约 + 个人上下文剥离。两件事都在共用助手里做，
        # 保证任何 provider（含将来新增的）都逃不掉。
        public = _public_payload_for_request(bundle)
        system_instruction = public.pop("system_instruction")
        return {
            "model": self.model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_instruction}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(public, ensure_ascii=False),
                        }
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "yuanjian_judgment",
                    "strict": True,
                    "schema": _result_schema(),
                }
            },
        }

    @staticmethod
    def _output_text(response):
        if isinstance(response, dict) and isinstance(response.get("output_text"), str):
            return response["output_text"]
        for output in response.get("output", ()) if isinstance(response, dict) else ():
            for content in output.get("content", ()) if isinstance(output, dict) else ():
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    return content["text"]
        raise InvalidJudgmentError("远程响应缺少结构化输出文本")

    def analyze(self, bundle) -> JudgmentResult:
        token = str(self.token_loader() or "").strip()
        if not token:
            raise RemoteProviderError("auth")
        body = self._request_body(bundle)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            response = self.transport(self.endpoint, headers, body, self.timeout)
        except RemoteProviderError:
            raise
        except (TimeoutError, socket.timeout) as error:
            raise RemoteProviderError("timeout") from error
        try:
            decoded = json.loads(self._output_text(response))
        except json.JSONDecodeError as error:
            raise InvalidJudgmentError("远程输出不是有效的研判JSON") from error
        try:
            return validate_judgment(decoded, set(bundle.allowed_source_ids))
        except InvalidJudgmentError:
            # 稿D：严格校验失败时尝试宽松修复（缺字段/类型错/数组长度不对），
            # 修复成功则用修复后的远程结果，失败才抛出由调用方降级 local。
            repaired = repair_judgment(decoded, set(bundle.allowed_source_ids))
            if repaired is not None:
                return repaired
            raise


class DeepSeekChatProvider:
    """DeepSeek Chat Completions 格式 provider（/chat/completions + messages + response_format）。

    DeepSeek 不兼容 OpenAI Responses API（/v1/responses），只支持 Chat Completions。
    JSON Output 通过 response_format={"type":"json_object"} + prompt 内字段说明实现。

    **名字是历史包袱**：它同时承担所有 OpenAI 兼容的 Chat 端点（DeepSeek、
    Agnes AI、SiliconFlow……），`AiSettingsService.create_remote_provider` 按
    端点是不是以 `/chat/completions` 结尾来选它。
    """

    name = "deepseek_chat"

    #: 单次回复的 token 上限。**不要退回 4000**：2026-09-23 实测
    #: `agnes-2.0-flash` 是**推理模型** —— 给它 `max_tokens=8` 时返回
    #: `finish_reason="length"`、`content` 为空串、8 个 token 全部落在
    #: `reasoning_content` 里。也就是说**思维链和正式输出共用这一份预算**：
    #: 4000 被思维链吃掉一截之后，判读 JSON 就在中途断掉，
    #: 表现成 `invalid_output`（真库 21 条：18 条"输出不是有效的研判JSON"
    #: + 3 条"响应缺少文本内容"）。
    #:
    #: 取 8192 而不是更大，是为了**跨 provider 安全**：`deepseek-chat` 的
    #: `max_tokens` 上限就是 8192，写 16000 会在换成 DeepSeek 官方端点时直接
    #: 400。判读 JSON 本身约 1.5~2k token，8192 留了 4 倍余量，够思维链用；
    #: 真在某家端点上还截断，改这一个数即可（错误信息会明说"输出被截断"）。
    MAX_OUTPUT_TOKENS = 8192

    def __init__(
        self,
        *,
        model: str,
        token_loader,
        endpoint: str = "https://api.deepseek.com/chat/completions",
        transport=None,
        timeout: int = 60,
    ):
        self.model = str(model or "").strip()
        if not self.model:
            raise ValueError("启用远程AI时必须明确填写模型编号")
        self.endpoint = _validate_endpoint(endpoint)
        self.token_loader = token_loader
        self.transport = transport or _default_transport
        self.timeout = int(timeout)

    def _request_body(self, bundle):
        # 出口闸：体积契约 + 个人上下文剥离。DeepSeek 分支历史上**完全没有**
        # 校验上限（实测曾实发 18,985 字符），现在与 OpenAI 分支共用同一道闸。
        public = _public_payload_for_request(bundle)
        system_instruction = public.pop("system_instruction")
        # 注意：这里**没有** personal_context 可 pop —— 闸门已在
        # _public_payload_for_request 里把它剥掉，远程请求体永远不含它。
        # DeepSeek 的 json_object 只保证输出合法 JSON，不保证字段齐全，
        # 必须在 prompt 内明确列出所有字段，再靠 repair_judgment 兜底。
        system_with_schema = (
            system_instruction
            + "\n\n输出要求：严格返回一个JSON对象，字段包括 fact_summary(str)、"
            "actors(string[])、causal_chain(string[])、uncertainties(string[])、"
            "horizons(string[])、probability_low(number 0-1)、probability_high(number 0-1)、"
            "confidence(number 0-1)、supporting_source_ids(string[])、counter_source_ids(string[])、"
            "up_triggers(string[])、down_triggers(string[])、impact_categories(string[])、"
            "personal_action(string，必填，80-300字)、"
            "gyw(object，含 stakeholders/constraints/least_resistance_path/counter_evidence/"
            "leading_indicators/beneficiaries/cost_bearers/historical_parallel/observable_signals)。"
            "不要输出JSON以外的任何文字。"
        )
        # P0-1：这里原本会按 `personal_context` 拼一段「用户个人上下文」进
        # system prompt，并要求 AI 结合用户近况（收入、工作、所在地）写
        # personal_action —— 那正是把个人画像推给远程服务的根因。已整段删除。
        # 远程产出的 personal_action 只基于事件本身；个性化行动建议由本机
        # （impacts.map_judgment 与本机模型）生成。
        system_with_schema += (
            "\n\npersonal_action 字段要求：基于事件本身给出应对方向与一个可执行动作；"
            "信息不足以判断与具体个人关系时，说明这是通用层面影响、暂不需要个人操作。"
            "禁止出现'请你自行核实/查清是否在适用范围/建议你关注'这类把判断推回给用户的写法。"
        )
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_with_schema},
                {"role": "user", "content": json.dumps(public, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.MAX_OUTPUT_TOKENS,
            "stream": False,
        }

    @staticmethod
    def _output_text(response):
        if isinstance(response, dict):
            choices = response.get("choices", [])
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message", {})
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
                # 空 content 不能笼统报"缺少文本内容" —— 那会把人引向"模型不听话"，
                # 而真实原因几乎总是**预算被思维链吃光**（见 MAX_OUTPUT_TOKENS）。
                # 把 finish_reason 与推理长度原样带出去，下次看一眼就懂。
                finish = str(choices[0].get("finish_reason") or "")
                reasoning = msg.get("reasoning_content")
                if finish == "length" or (isinstance(reasoning, str) and reasoning.strip()):
                    raise InvalidJudgmentError(
                        "远程输出被截断：token 预算被推理过程占满"
                        f"（finish_reason={finish or '未知'}，"
                        f"推理内容 {len(reasoning or '')} 字，"
                        f"max_tokens={DeepSeekChatProvider.MAX_OUTPUT_TOKENS}）"
                    )
        raise InvalidJudgmentError("DeepSeek响应缺少文本内容")

    @staticmethod
    def _extract_json(text: str) -> str:
        """从 DeepSeek 输出中提取 JSON 部分，处理 markdown 代码块等常见格式问题。"""
        if not text:
            return text
        text = text.strip()
        # 去掉 markdown 代码块标记 ```json ... ```
        if text.startswith("```"):
            # 找到第一个 { 或 [
            start = min(
                (text.find(c) for c in "{[" if text.find(c) >= 0),
                default=0
            )
            end = max(
                (text.rfind(c) for c in "}]" if text.rfind(c) >= 0),
                default=len(text)
            )
            if end > start:
                text = text[start:end+1]
        # 去掉前后多余的文字（如果 JSON 在中间）
        first_brace = text.find("{")
        first_bracket = text.find("[")
        starts = [x for x in [first_brace, first_bracket] if x >= 0]
        if starts:
            start = min(starts)
            # 找到匹配的结束符
            end_brace = text.rfind("}")
            end_bracket = text.rfind("]")
            ends = [x for x in [end_brace, end_bracket] if x >= 0]
            if ends:
                end = max(ends)
                if end > start:
                    text = text[start:end+1]
        return text.strip()

    def analyze(self, bundle) -> JudgmentResult:
        token = str(self.token_loader() or "").strip()
        if not token:
            raise RemoteProviderError("auth")
        body = self._request_body(bundle)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            response = self.transport(self.endpoint, headers, body, self.timeout)
        except RemoteProviderError:
            raise
        except (TimeoutError, socket.timeout) as error:
            raise RemoteProviderError("timeout") from error
        raw_text = self._output_text(response)
        # 先尝试直接解析
        try:
            decoded = json.loads(raw_text)
        except json.JSONDecodeError:
            # 提取 JSON 部分后重试
            extracted = self._extract_json(raw_text)
            try:
                decoded = json.loads(extracted)
            except json.JSONDecodeError:
                # 仅修复 trailing commas（安全，不会破坏字符串内容）；
                # 不做单引号全局替换——会破坏含撇号的字符串值（如 don't）。
                try:
                    import re
                    fixed = re.sub(r",\s*([}\]])", r"\1", extracted)
                    decoded = json.loads(fixed)
                except (json.JSONDecodeError, Exception):
                    raise InvalidJudgmentError("DeepSeek输出不是有效的研判JSON")
        try:
            return validate_judgment(decoded, set(bundle.allowed_source_ids))
        except InvalidJudgmentError:
            repaired = repair_judgment(decoded, set(bundle.allowed_source_ids))
            if repaired is not None:
                return repaired
            raise


AI_SETTINGS_STATE_KEY = "ai_settings"


def _clamp_daily_budget(raw) -> int:
    """每日上限的读侧归一化：非法/越界一律回退默认，**不抛异常**（读侧约定）。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DAILY_REMOTE_BUDGET
    if not MIN_DAILY_BUDGET <= value <= MAX_DAILY_BUDGET:
        return DAILY_REMOTE_BUDGET
    return value


def read_ai_setting(database) -> dict:
    """读取远程 AI 设置（runtime_state.ai_settings）。读侧非法值回退默认。

    `AiSettingsService._stored()` 与 `JudgmentQueue` **共用本函数**，
    所以"设置页存了新上限、队列却还在用旧的常量"这类漂移不会发生——
    这正是 `daily_budget` 从常量变成设置项时必须先解决的事。
    """
    with database.connect() as connection:
        row = connection.execute(
            "SELECT value_json FROM runtime_state WHERE state_key=?",
            (AI_SETTINGS_STATE_KEY,),
        ).fetchone()
    value = {}
    if row:
        try:
            loaded = json.loads(row["value_json"])
            if isinstance(loaded, dict):
                value = loaded
        except (ValueError, TypeError):
            value = {}
    frequency = str(value.get("frequency", "medium")).strip().lower()
    if frequency not in ("low", "medium", "high"):
        frequency = "medium"
    return {
        "enabled": bool(value.get("enabled", False)),
        "endpoint": str(value.get("endpoint") or DEFAULT_ENDPOINT),
        "model": str(value.get("model") or ""),
        "frequency": frequency,
        "daily_budget": _clamp_daily_budget(
            value.get("daily_budget", DAILY_REMOTE_BUDGET)
        ),
    }


class AiSettingsService:
    """Persist non-secret AI settings while keeping the token in DPAPI storage."""

    STATE_KEY = AI_SETTINGS_STATE_KEY

    def __init__(self, database, secret_store):
        self.database = database
        self.secret_store = secret_store

    def _stored(self):
        return read_ai_setting(self.database)

    def get(self):
        value = self._stored()
        try:
            configured = bool(self.secret_store.load())
        except (OSError, ValueError, RuntimeError):
            configured = False
        return {**value, "configured": configured}

    def save(self, payload):
        current = self._stored()
        enabled = payload.get("enabled", current["enabled"])
        if not isinstance(enabled, bool):
            raise ValueError("AI启用状态无效")
        endpoint = _validate_endpoint(payload.get("endpoint", current["endpoint"]))
        model = str(payload.get("model", current["model"])).strip()
        frequency = str(payload.get("frequency", current["frequency"])).strip().lower()
        if frequency not in ("low", "medium", "high"):
            frequency = "medium"
        # 写侧越界抛 ValueError（与 hour / days 一致），缺失则沿用当前值。
        raw_budget = payload.get("daily_budget", current["daily_budget"])
        try:
            daily_budget = int(raw_budget)
        except (TypeError, ValueError):
            raise ValueError("远程AI每日上限无效")
        if not MIN_DAILY_BUDGET <= daily_budget <= MAX_DAILY_BUDGET:
            raise ValueError(
                f"远程AI每日上限需在 {MIN_DAILY_BUDGET}-{MAX_DAILY_BUDGET} 之间"
            )
        if "token" in payload:
            self.secret_store.save(str(payload.get("token") or ""))
        configured = bool(self.secret_store.load())
        if enabled and (not model or not configured):
            raise ValueError("启用远程AI前必须填写模型编号和API密钥")
        value = {
            "enabled": enabled,
            "endpoint": endpoint,
            "model": model,
            "frequency": frequency,
            "daily_budget": daily_budget,
        }
        now = _iso(datetime.now(timezone.utc))
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_state(state_key,value_json,updated_at)
                VALUES (?,?,?)
                ON CONFLICT(state_key) DO UPDATE SET
                    value_json=excluded.value_json,updated_at=excluded.updated_at
                """,
                (self.STATE_KEY, json.dumps(value, sort_keys=True), now),
            )
        return {**value, "configured": configured}

    def create_remote_provider(self):
        settings = self.get()
        if not settings["enabled"] or not settings["configured"]:
            return None
        endpoint = settings["endpoint"]
        model = settings["model"]
        # 判断是否为 Chat Completions 格式端点（/chat/completions），
        # 包括 DeepSeek、Agnes AI 等 OpenAI 兼容接口。
        # 这类接口不支持 OpenAI Responses API（/v1/responses），必须用 Chat 格式。
        is_chat_endpoint = endpoint.rstrip("/").endswith("/chat/completions")
        is_deepseek = "deepseek.com" in endpoint.casefold()
        if is_deepseek and endpoint.rstrip("/").endswith("/v1/responses"):
            endpoint = "https://api.deepseek.com/chat/completions"
            is_chat_endpoint = True
        if is_chat_endpoint or is_deepseek:
            return DeepSeekChatProvider(
                endpoint=endpoint,
                model=model,
                token_loader=self.secret_store.load,
            )
        return OpenAIResponsesProvider(
            endpoint=endpoint,
            model=model,
            token_loader=self.secret_store.load,
        )


class JudgmentQueue:
    #: 关闭兜底态：不在 `run_due` 选取的到期集合
    #: （`queued`/`retry`/`queued_budget`）里，故**进程内不会被执行、跨重启也不会
    #: 被自动拾起**；显式 `requeue_for_upgrade` 才会把它放回 `queued`。
    #: 与 `paused_auth` 是同一套路（认证失败批量冻结）。
    PAUSED_SHUTDOWN_STATUS = "paused_shutdown"

    def __init__(
        self,
        database,
        *,
        providers,
        bundle_loader,
        local_provider=None,
        now=lambda: datetime.now(timezone.utc),
        daily_budget=None,
        personal_context_loader=None,
    ):
        self.database = database
        self.providers = dict(providers)
        self.bundle_loader = bundle_loader
        self.local_provider = local_provider or LocalHeuristicProvider()
        self.now = now
        #: `None`（默认）= 每次 `run_due` 现读设置，用户改完即时生效；
        #: 传具体数值 = 固定上限（测试与嵌入式用法用这条路径）。
        self.daily_budget = None if daily_budget is None else int(daily_budget)
        # P2: 远程研判时注入个人利益地图与历史预测的回调（cluster_id -> dict | None）。
        # 本地研判永不调用，保持"local never sees personal interests"隐私边界。
        self.personal_context_loader = personal_context_loader
        #: 连续失败熔断：provider -> 连续非鉴权失败次数（进程内计数）。
        #: 达 `REMOTE_CIRCUIT_THRESHOLD` 就冻结该 provider 的排队作业；
        #: 任一作业成功即清零并解除（见 `run_due`）。
        self._failure_streak = {}
        #: 当前处于"熔断打开"状态的 provider 集合（`_failure_streak` 达过阈值、
        #: 且还没靠一次成功解除）。用于避免重复记日志、并驱动解除时的解冻。
        self._circuit_open = set()
        #: 熔断打开的 provider -> 下一次**允许放行的探测时刻**（半开状态）。
        #: 见 `REMOTE_CIRCUIT_PROBE_MINUTES`：不留这条缝，熔断就再也等不到
        #: 那"一次成功"，等于把 provider 永久封死。
        self._circuit_probe_at = {}
        # 关闭标志：退出时设置，立即中断任务处理，防止继续调用API
        self._shutdown = threading.Event()

    def shutdown(self):
        """安全关闭：设置关闭标志，清空所有待处理的远程任务。

        退出时必须先调用此方法，确保不会有新的 API 请求发出。

        **两段式**：

        1. 正常路径 —— 直接 `DELETE` 掉所有排队中的远程任务。
        2. **兜底路径** —— `DELETE` 失败时（库被占锁、磁盘只读……）绝不能再
           `except: pass`：那些 `queued` 行会原样留在库里，用户以为"退出即取消"，
           下次启动 `run_due` 却照常把它们发出去 —— 这就是**实打实的意外花钱**。
           所以失败先 `_logger.warning(..., exc_info=True)` 留痕，再退一步把这些行
           **冻结**成 `PAUSED_SHUTDOWN_STATUS`：它不在 `run_due` 选取的
           `status IN ('queued','retry','queued_budget')` 里，因此进程内不会被
           执行，**跨重启也不会被自动拾起**；只有显式 `requeue_for_upgrade`
           才会把它放回 `queued`。冻结再失败才 `_logger.error(..., exc_info=True)`。

        冻结而非"保持 queued 把 next_attempt_at 推到很远的将来"：后者是在撒谎
        （状态写着"排队中、将来会跑"），而且任何将来按 `created_at` 重算
        `next_attempt_at` 的代码都会把它复活。
        """
        self._shutdown.set()
        scope = "provider!='local' AND status IN ('queued','retry','queued_budget')"
        try:
            with self.database.connect() as connection:
                removed = connection.execute(
                    f"DELETE FROM judgment_jobs WHERE {scope}"
                ).rowcount
            if removed:
                _logger.info("关闭时清空了 %d 条待处理的远程研判作业", removed)
            return
        except Exception:
            _logger.warning(
                "关闭时清空待处理的远程研判作业失败，改用冻结兜底"
                "（防止下次启动误发请求）",
                exc_info=True,
            )
        try:
            with self.database.connect() as connection:
                frozen = connection.execute(
                    "UPDATE judgment_jobs SET status=?,last_error='shutdown'"
                    f" WHERE {scope}",
                    (self.PAUSED_SHUTDOWN_STATUS,),
                ).rowcount
            _logger.warning(
                "已冻结 %d 条待处理的远程研判作业（status=%s，不会被自动执行）",
                frozen,
                self.PAUSED_SHUTDOWN_STATUS,
            )
        except Exception:
            _logger.error(
                "兜底冻结远程研判作业也失败：队列里仍有排队中的远程作业，"
                "下次启动可能被自动执行 —— 请手动检查 judgment_jobs",
                exc_info=True,
            )

    def enqueue(self, cluster_id: str, evidence_hash: str, provider: str) -> str:
        if provider not in self.providers:
            raise KeyError(provider)
        created = _iso(self.now())
        job_id = "Q-" + uuid.uuid4().hex
        model = str(getattr(self.providers[provider], "model", "local"))
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO judgment_jobs(
                    job_id,cluster_id,evidence_hash,provider,model,status,
                    attempts,request_chars,created_at,next_attempt_at,last_error
                ) VALUES (?,?,?,?,?,'queued',0,0,?,?,'')
                """,
                (job_id, cluster_id, evidence_hash, provider, model, created, created),
            )
            row = connection.execute(
                """
                SELECT job_id FROM judgment_jobs
                WHERE cluster_id=? AND evidence_hash=? AND provider=?
                """,
                (cluster_id, evidence_hash, provider),
            ).fetchone()
        return row["job_id"]

    def requeue_for_upgrade(self, cluster_id: str, evidence_hash: str, provider: str) -> str:
        """需要用最新提示词/schema 重新远程研判时复用作业行：
        同一 (cluster,evidence_hash,provider) 已存在终态作业（succeeded/invalid_output/
        failed/paused_auth）时把它重置回 queued；已在排队则直接返回；不存在则新建。
        解决 enqueue 的唯一约束导致"旧版本远程研判永远无法被新schema重判"的问题。"""
        if provider not in self.providers:
            raise KeyError(provider)
        now_text = _iso(self.now())
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT job_id,status FROM judgment_jobs
                WHERE cluster_id=? AND evidence_hash=? AND provider=?
                """,
                (cluster_id, evidence_hash, provider),
            ).fetchone()
            if row is None:
                return self.enqueue(cluster_id, evidence_hash, provider)
            if row["status"] in ("queued", "retry", "queued_budget"):
                return row["job_id"]
            connection.execute(
                """
                UPDATE judgment_jobs SET status='queued',attempts=0,request_chars=0,
                    next_attempt_at=?,last_error='',finished_at=NULL WHERE job_id=?
                """,
                (now_text, row["job_id"]),
            )
            return row["job_id"]

    def _bump_remote_usage(self, connection, now, count: int = 1) -> None:
        """把"今日已发起的远程调用次数"加 `count`，与作业状态更新**同一事务**。

        为什么不能只数行：一个作业最多重试 `MAX_REMOTE_RETRIES-1` 次，而重试分支
        **不写 `finished_at`** —— 只数 `finished_at IS NOT NULL` 的行，就会把
        "失败重试"这一次真实付费调用**整个漏掉**，而且失败得越厉害漏得越多
        （日预算因此形同虚设：账上写着 2000，实际可发出约 2000×重试倍数次）。
        按次记账才能对上账单；计数器跨重启不丢，这正是"日预算"需要的性质。
        """
        day = _usage_day(now)
        payload = {}
        row = connection.execute(
            "SELECT value_json FROM runtime_state WHERE state_key=?",
            (REMOTE_USAGE_STATE_KEY,),
        ).fetchone()
        if row:
            try:
                loaded = json.loads(row["value_json"])
            except (ValueError, TypeError):
                loaded = None
            if isinstance(loaded, dict):
                payload = loaded
        if payload.get("day") != day:
            # 换日 ⇒ 从零开始；旧日期的数据自然作废，不需要清理任务。
            payload = {"day": day, "used": 0}
        payload["used"] = int(payload.get("used", 0) or 0) + count
        connection.execute(
            """
            INSERT INTO runtime_state(state_key,value_json,updated_at)
            VALUES (?,?,?)
            ON CONFLICT(state_key) DO UPDATE SET
                value_json=excluded.value_json,updated_at=excluded.updated_at
            """,
            (
                REMOTE_USAGE_STATE_KEY,
                json.dumps(payload, sort_keys=True),
                _iso(now),
            ),
        )

    def _remote_used_today(self, connection, now):
        """今日已实际发起的远程调用**次数**（成功/失败/格式错误/重试都算）。

        首选 `runtime_state.ai_remote_usage` 计数器：按次记账、重试也算、跨重启不丢。
        计数器缺失（老库 / 本轮之前从未调用过）时退回原来的**按行**统计 —— 那条
        路径只数 `finished_at` 非空的行，**会低估**（重试不计），仅作兼容，不是准值。
        """
        row = connection.execute(
            "SELECT value_json FROM runtime_state WHERE state_key=?",
            (REMOTE_USAGE_STATE_KEY,),
        ).fetchone()
        if row:
            try:
                payload = json.loads(row["value_json"])
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                if payload.get("day") == _usage_day(now):
                    return int(payload.get("used", 0) or 0)
                # 计数器还停在昨天 ⇒ 今日一笔都还没发过
                return 0
        start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return connection.execute(
            """
            SELECT COUNT(*) FROM judgment_jobs
            WHERE provider!='local' AND finished_at IS NOT NULL
              AND finished_at>=? AND finished_at<?
            """,
            (_iso(start), _iso(end)),
        ).fetchone()[0]

    def remote_budget_snapshot(self) -> dict:
        """后端诊断用：本轮生效的上限 / 今日已用次数 / 熔断状态。

        `budget` 与 `used_today` 都在这里出口 —— 前端要展示"还剩多少次"就取这两个
        字段，不要自己另算一份（另算必然与真正的闸不一致）。
        """
        now = self.now().astimezone(timezone.utc)
        with self.database.connect() as connection:
            used = int(self._remote_used_today(connection, now))
        return {
            "budget": self._current_daily_budget(),
            "used_today": used,
            "day": _usage_day(now),
            "circuit_open": sorted(self._circuit_open),
            "failure_streak": dict(self._failure_streak),
            "circuit_probe_at": {
                name: _iso(moment)
                for name, moment in sorted(self._circuit_probe_at.items())
            },
        }

    def _open_circuit(self, connection, provider_name, now, *, limit_scope="") -> int:
        """连续失败达阈值：冻结该 provider 的**全部**排队作业并标注原因。

        复用既有 `paused_auth` 状态，**不新增状态** —— 新增一个状态就要同时改
        `shutdown()` 的清理范围、`requeue_for_upgrade` 的终态表与诊断面板，
        那是实打实的第二套机制。区分两种冻结靠 `last_error`：
        `circuit_open`（连续失败）与 `auth_paused`（认证失败）互不相干。

        `limit_scope` 非空时（目前只有 `upstream_free_tier`）把原因写成
        `circuit_open:upstream_free_tier`：**用户什么都不用改**，等对端恢复即可；
        不带后缀的那种更可能是网络侧问题。诊断页靠这个后缀把两种话说清楚
        （见 `diagnostics._read_remote_health`）。匹配一律用前缀。

        打开时一并记下**下次探测时刻**（`REMOTE_CIRCUIT_PROBE_MINUTES`）：不留这条
        缝，"成功一次即解除"就永远等不到那一次成功。
        """
        reason = (
            f"{CIRCUIT_OPEN_REASON}:{limit_scope}" if limit_scope else CIRCUIT_OPEN_REASON
        )
        self._circuit_open.add(provider_name)
        self._circuit_probe_at[provider_name] = now + timedelta(
            minutes=REMOTE_CIRCUIT_PROBE_MINUTES
        )
        paused = connection.execute(
            """
            UPDATE judgment_jobs SET status='paused_auth',last_error=?
            WHERE provider=? AND status IN ('queued','retry','queued_budget')
            """,
            (reason, provider_name),
        ).rowcount
        _logger.warning(
            "远程 provider %s 连续失败 %d 次，触发熔断：冻结 %d 条排队作业"
            "（status=paused_auth，last_error=%s）；成功一次或重新入队即解除",
            provider_name,
            self._failure_streak.get(provider_name, 0),
            paused,
            reason,
        )
        return paused

    def _close_circuit(self, connection, provider_name, now=None) -> int:
        """一次成功即解除该 provider 的熔断：计数清零 + **错峰**解冻这批作业。

        **只**解冻 `last_error` 以 `CIRCUIT_OPEN_REASON` 开头的那批 —— 认证失败
        冻结的（`auth_paused`）绝不放行：那是用户没配好凭据，放行就是白花钱重试。
        用前缀匹配是因为原因可能带 `:upstream_free_tier` 后缀，写等号会让那批
        永远解不了冻（一次限流升级成永久封死）。

        **错峰**（`REMOTE_CIRCUIT_RESUME_SPACING_SECONDS`）：解冻**不是**把几百条
        一起设回"现在到期"。那样下一轮会成批打出去，几乎必然再次被 429 打回、
        再次熔断，形成"偶发成功 → 惊群 → 立刻再熔断"的空转（真库证据见该常量）。
        按序号往后排，一轮只消化得掉 `REMOTE_SLOTS_PER_ROUND` 条。
        """
        was_open = provider_name in self._circuit_open
        self._failure_streak[provider_name] = 0
        self._circuit_open.discard(provider_name)
        self._circuit_probe_at.pop(provider_name, None)
        if not was_open:
            return 0
        frozen = connection.execute(
            """
            SELECT job_id FROM judgment_jobs
            WHERE provider=? AND status='paused_auth' AND last_error LIKE ?
            ORDER BY created_at, job_id
            """,
            (provider_name, f"{CIRCUIT_OPEN_REASON}%"),
        ).fetchall()
        resume_at = now or self.now()
        for index, row in enumerate(frozen):
            due = resume_at + timedelta(
                seconds=index * REMOTE_CIRCUIT_RESUME_SPACING_SECONDS
            )
            connection.execute(
                """
                UPDATE judgment_jobs SET status='queued',last_error='',next_attempt_at=?
                WHERE job_id=?
                """,
                (_iso(due), row["job_id"]),
            )
        resumed = len(frozen)
        _logger.warning(
            "远程 provider %s 熔断解除：本轮有一次调用成功，解冻 %d 条排队作业"
            "（按 %g 秒错峰排回队列，不再一次性全部到期）",
            provider_name,
            resumed,
            REMOTE_CIRCUIT_RESUME_SPACING_SECONDS,
        )
        return resumed

    def remote_used_today(self) -> int:
        """公共接口：今日已实际调用的远程 AI 次数（含失败/格式错误，诊断面板用）。"""
        now = self.now().astimezone(timezone.utc)
        with self.database.connect() as connection:
            return int(self._remote_used_today(connection, now))

    def rehydrate_circuit_state(self, now=None) -> dict:
        """启动时把"熔断曾经打开过"这件事从库里捡回来。

        **不捡的后果是永久冻结。** `_circuit_open` 是**进程内**状态，重启就空；
        而 `_close_circuit` 一进门就是 `if not was_open: return 0` —— 于是上一轮
        熔断冻结的那批 `paused_auth`（`last_error=circuit_open%`）**永远等不到
        解冻**，除非某个簇恰好被 `requeue_for_upgrade` 扫到。真库 2026-09-23
        就是这样一路堆到 256 条的：重启 → 熔断状态归零 → 冻结作业无人认领 →
        下次熔断再冻一批，只增不减。

        捡回来 = 重新打开熔断：作业**继续冻结**（不白花钱），但探测窗口一过就会
        放一条进去试，成功一次即整批错峰解冻（见 `_close_circuit`）。

        **这里只恢复状态，不动作业。** 启动时把一条冻结作业放回 `queued` 看似更
        主动，实则有坑：`shutdown()` 的"退出即取消"会 `DELETE` 所有 `queued` 的
        远程作业（那是**有意设计**，防止退出后仍在计费），于是每重启一次就白丢
        一条积压。放回作业的时机交给 `_promote_circuit_probe`，它只在真正要探测
        的那一刻做，没有这个窗口。

        返回 `{provider: 冻结条数}`，便于启动日志说清楚。
        """
        moment = (now or self.now()).astimezone(timezone.utc)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT provider, COUNT(*) AS n FROM judgment_jobs
                WHERE provider!='local' AND status='paused_auth' AND last_error LIKE ?
                GROUP BY provider
                """,
                (f"{CIRCUIT_OPEN_REASON}%",),
            ).fetchall()
            resumed = {}
            for row in rows:
                name = row["provider"]
                self._circuit_open.add(name)
                self._circuit_probe_at[name] = moment + timedelta(
                    minutes=REMOTE_CIRCUIT_PROBE_MINUTES
                )
                resumed[name] = int(row["n"])
        if resumed:
            _logger.warning(
                "启动时发现熔断遗留的冻结作业 %s，已恢复熔断状态（每 %d 分钟放一条探测；"
                "探测成功即整批错峰解冻）",
                resumed,
                REMOTE_CIRCUIT_PROBE_MINUTES,
            )
        return resumed

    def _promote_circuit_probe(self, connection, now) -> int:
        """给"熔断已打开、探测窗口已过、且一条到期作业都没有"的 provider 放回一条探测作业。

        为什么必须显式放一条：半开探测挂在对 `run_due` **选出来的到期行**上，而冻结
        作业全在 `paused_auth`、不在到期集合里。若某 provider 的到期集合为空
        （整批都被冻住），探测**没有对象**，"冷却期后放一条进去试"这条自愈的缝
        就形同不存在 —— 真库重启后正是这个状态。

        只在该 provider **完全没有** `queued/retry/queued_budget` 作业时才放一条：
        雷达随时会采到新条目、新簇入队，那些新作业本身就是天然探测对象，不需要
        再从积压里搬一条出来（否则每次 `run_due` 都会搬，把积压一条条搬到队首）。
        """
        promoted = 0
        for name in sorted(self._circuit_open):
            due_at = self._circuit_probe_at.get(name)
            if due_at is not None and now < due_at:
                continue
            probe = connection.execute(
                """
                SELECT job_id FROM judgment_jobs
                WHERE provider=? AND status='paused_auth' AND last_error LIKE ?
                  AND NOT EXISTS (
                      SELECT 1 FROM judgment_jobs q
                      WHERE q.provider=?
                        AND q.status IN ('queued','retry','queued_budget')
                  )
                ORDER BY created_at, job_id LIMIT 1
                """,
                (name, f"{CIRCUIT_OPEN_REASON}%", name),
            ).fetchone()
            if probe is None:
                continue
            connection.execute(
                "UPDATE judgment_jobs SET status='queued',next_attempt_at=? WHERE job_id=?",
                (_iso(now), probe["job_id"]),
            )
            promoted += 1
        return promoted

    def _current_daily_budget(self) -> int:
        """本轮生效的每日上限。构造时没给固定值就从设置现读（用户改完即时生效）。"""
        if self.daily_budget is not None:
            return self.daily_budget
        return read_ai_setting(self.database)["daily_budget"]

    def _persist_judgment(self, connection, job, provider, result, now):
        """把一次判读结果落库，**始终返回该簇真实存在的一条研判 id**。

        调用方依赖返回值有效，且 `event_clusters.latest_judgment_id` 必须始终
        指向真实存在的研判——以下所有分支都满足这两点。

        防冗余写入（2026-09-14 新增两道闸门，只跳过「新增」，绝不改动已有数据，
        也绝不抛异常）：

        - **F2 · 内容相同不重复写**：本次 `content_json` 与该簇**最新一条**
          （按 `created_at`）**完全相同**时跳过插入。真正被拦住的不是"同证据重判"
          ——那个早已由 `UNIQUE(cluster_id, provider, evidence_hash)` 加
          `INSERT OR IGNORE` 覆盖（实测三元组重复数为 0，F1 是空操作）——
          而是「簇内新增条目 → `evidence_hash` 变化 → 整条重新研判，模型却给出
          一字不差的相同内容」。不拦的话就是把同一份 JSON 再存一遍。
        - **F3 · 单簇条数上限**：该簇研判数 ≥ `max_judgments_per_cluster`
          （默认 8）时不再新增。

        **F3 的产品代价（必须知情，不要只当成一个磁盘开关）**：一旦某个簇触顶，
        它的研判就**停止进化**——`latest_judgment_id` 不再跟随后续证据更新，
        用户看到的是旧证据下的判读，直到用户调高上限。这是拿「判读新鲜度」
        换磁盘，而实测 N=8 只省约 8.2 MB（1,193 条研判 + 1,731 条连带
        `personal_impacts`，杠杆系数 1.569）。**交换比并不好**，所以上限
        必须让用户可调（设置项 `max_judgments_per_cluster`，1~100）。

        两条跳过路径都会把 `needs_judgment` 置 0。原因：不置 0 的话，认知扫描
        会认为该簇"仍待研判"，每次扫描反复入队，**反而制造 `judgment_jobs` 垃圾**
        ——那正是本层要治理的东西，得不偿失。

        本方法不删除任何历史研判（判读不可变，`judgments` 另有触发器保护），
        也不涉及 VACUUM。
        """
        cluster_id = job["cluster_id"]
        content_json = json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)

        # 该簇最新一条研判。主排序是契约规定的 created_at；
        # 附加 judgment_id 只是为了让同一秒并列时的结果确定，不改变语义。
        latest = connection.execute(
            """
            SELECT judgment_id, content_json FROM judgments
            WHERE cluster_id=?
            ORDER BY created_at DESC, judgment_id DESC LIMIT 1
            """,
            (cluster_id,),
        ).fetchone()

        # F2：与最新一条内容完全相同 -> 不新增，仍返回已存在的最新 id
        if latest is not None and latest["content_json"] == content_json:
            return self._mark_cluster_judged(
                connection, cluster_id, latest["judgment_id"], now
            )

        # F3：已达单簇上限 -> 不新增，仍返回已存在的最新 id
        if latest is not None:
            limit = read_retention_setting(self.database)["max_judgments_per_cluster"]
            total = connection.execute(
                "SELECT COUNT(*) FROM judgments WHERE cluster_id=?", (cluster_id,)
            ).fetchone()[0]
            if total >= limit:
                return self._mark_cluster_judged(
                    connection, cluster_id, latest["judgment_id"], now
                )

        judgment_id = "J-" + uuid.uuid4().hex
        connection.execute(
            """
            INSERT OR IGNORE INTO judgments(
                judgment_id,cluster_id,provider,evidence_hash,content_json,created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                judgment_id,
                cluster_id,
                provider,
                job["evidence_hash"],
                content_json,
                _iso(now),
            ),
        )
        stored = connection.execute(
            """
            SELECT judgment_id FROM judgments
            WHERE cluster_id=? AND provider=? AND evidence_hash=?
            """,
            (cluster_id, provider, job["evidence_hash"]),
        ).fetchone()["judgment_id"]
        return self._mark_cluster_judged(connection, cluster_id, stored, now)

    @staticmethod
    def _mark_cluster_judged(connection, cluster_id, judgment_id, now):
        """把簇标记为「已研判」：指到一条真实研判，并清掉待研判标志。"""
        connection.execute(
            """
            UPDATE event_clusters SET latest_judgment_id=?,needs_judgment=0,updated_at=?
            WHERE cluster_id=?
            """,
            (judgment_id, _iso(now), cluster_id),
        )
        return judgment_id

    def run_due(self, limit: int = 5, *, remote_limit: int = 3) -> dict:
        """处理到期任务。远程任务每次最多处理remote_limit个，严格控制API消耗；
        本地任务不受此限制（不花钱）。"""
        # 关闭状态下不处理任何任务 —— 尤其**一条待处理的远程作业都不许进 `rows`**，
        # 否则"退出后仍在发 API 请求"会直接变成账单。这是进程内的最后一道闸；
        # 跨重启的**持久**兜底见 `shutdown()` 的冻结路径（`PAUSED_SHUTDOWN_STATUS`）。
        if self._shutdown.is_set():
            return {"succeeded": 0, "deferred": 0, "failed": 0, "shutdown": True}
        # 远程任务最多重试3次（普通失败退避15/30/60分钟；429 走 1/2/4 分钟，
        # 见 REMOTE_RATE_LIMIT_BACKOFF_MINUTES），超过降级本地
        MAX_REMOTE_RETRIES = 4
        # 自适应降速后，"本轮能发几条"必须跟着当前间隔一起缩 —— 否则间隔涨到
        # 300 秒时一轮 6 条要跑满 30 分钟，把单线程调度器里的采集/态势一起饿死
        # （理由与预算见 REMOTE_ROUND_TIME_BUDGET_SECONDS）。下限 1：再慢也得放
        # 一条出去，否则熔断的半开探测永远等不到那"一次成功"。
        remote_limit = _affordable_remote_slots(remote_limit)
        now = self.now().astimezone(timezone.utc)
        with self.database.connect() as connection:
            # 半开探测要有对象：熔断打开的 provider 若一条到期作业都没有
            # （整批都被冻在 paused_auth 里），探测就无从发生 —— 详见
            # `_promote_circuit_probe`。必须在取 remote_rows **之前**做。
            self._promote_circuit_probe(connection, now)
            due_sql = (
                "status IN ('queued','retry','queued_budget')"
                " AND (next_attempt_at IS NULL OR next_attempt_at<=?)"
            )
            # 修复：远程任务优先选取，避免被上千条本地任务按 created_at 排序挤出
            # limit 窗口（旧逻辑一次性 LIMIT 30 混合排序，远程任务几乎永远轮不到）。
            # 远程内部再按"当前个人影响等级"优先：L4 > L3 > 其他，让用户看得到的
            # 高影响事件最先获得 AI 结合个人画像的研判。
            remote_rows = connection.execute(
                f"""
                SELECT j.* FROM judgment_jobs j
                WHERE {due_sql} AND j.provider!='local'
                ORDER BY
                  (SELECT MAX(CASE WHEN p.alert_level='L4' THEN 2
                                   WHEN p.alert_level='L3' THEN 1 ELSE 0 END)
                   FROM personal_impacts p WHERE p.cluster_id=j.cluster_id) DESC,
                  j.created_at, j.job_id
                LIMIT ?
                """,
                (_iso(now), max(1, int(remote_limit))),
            ).fetchall()
            local_rows = connection.execute(
                f"""
                SELECT * FROM judgment_jobs
                WHERE {due_sql} AND provider='local'
                ORDER BY created_at,job_id LIMIT ?
                """,
                (_iso(now), max(1, min(int(limit), 100))),
            ).fetchall()
            rows = list(remote_rows) + list(local_rows)
            used = self._remote_used_today(connection, now)
        # 本轮固定用同一个上限：中途用户改设置也不让"半轮用旧值半轮用新值"。
        daily_budget = self._current_daily_budget()
        summary = {"succeeded": 0, "deferred": 0, "failed": 0}
        remote_done = 0
        # 本轮是否已"因限流停发"。429 的语义是"你发太快了"——继续把这一轮剩下的
        # 作业发出去，只会把同一个拒绝重复 N 遍，每一次都真实计入日预算与熔断计数。
        # 真库 2026-09-23 的现场就是这样：一次侥幸成功后连发，35 秒里吃 5 个 429。
        # 所以**第一次 429 就停掉本轮剩下的远程作业**，本地作业照跑（不花钱）。
        remote_halted = False
        for row in rows:
            # 每个任务处理前检查关闭标志
            if self._shutdown.is_set():
                break
            job = dict(row)
            provider = self.providers.get(job["provider"])
            if provider is None:
                continue
            is_remote = job["provider"] != "local"
            # 本轮已被限流 ⇒ 剩下的远程作业一条都不再发（本地作业不受影响）。
            if is_remote and remote_halted:
                continue
            # 熔断已打开 ⇒ 该 provider 本轮**剩下**的作业一律不发。
            # `rows` 是开轮时一次性取出的快照，状态还是旧的 'queued'；不显式跳过的话，
            # 刚被 `_open_circuit` 冻结的作业会在同一轮里被继续调用 —— 熔断就白开了。
            if is_remote and job["provider"] in self._circuit_open:
                # 半开：冷却期过后放行**一条**探测作业，对端恢复了就有机会自愈
                # （详见 REMOTE_CIRCUIT_PROBE_MINUTES）。放行后立刻把下次探测时刻
                # 推后，所以一轮里最多一条、一个冷却窗口里也最多一条。
                if now < self._circuit_probe_at.get(job["provider"], now):
                    continue
                self._circuit_probe_at[job["provider"]] = (
                    now + timedelta(minutes=REMOTE_CIRCUIT_PROBE_MINUTES)
                )
            # 远程任务数量限制
            if is_remote and remote_done >= remote_limit:
                continue
            # 远程任务在发起请求前再次检查关闭标志
            if is_remote and self._shutdown.is_set():
                break
            # 日预算检查（所有远程任务，含失败/格式错误，只要调用了API就计数）
            # 上限 0 = 用户明确关闭远程：`used >= 0` 恒真，全部延到明天，一个请求都不发。
            if is_remote and used >= daily_budget:
                tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE judgment_jobs SET status='queued_budget',next_attempt_at=?,last_error='daily_budget' WHERE job_id=?",
                        (_iso(tomorrow), job["job_id"]),
                    )
                summary["deferred"] += 1
                continue
            bundle = self.bundle_loader(job["cluster_id"])
            # P0-1（2026-09-15）：远程路径**不再注入**个人上下文。
            #
            # `PRIVACY.md` 明文承诺「外部 AI 的唯一输入是 build_public_bundle()
            # 生成的公开证据包」。此前远程分支会在这里把利益地图 + 历史预测 +
            # 用户主动记录的个人近况塞进 bundle 再随请求外发，既违背该承诺，
            # 交换比也不成立：远程 AI 只贡献 1.11% 的研判（803/72,253），却要
            # 为此外发完整个人画像 736 次。
            #
            # 个性化一律留在本机完成：`impacts.map_judgment()` 在本机算分数与
            # 等级，不需要远程参与。`personal_context_loader` 这个注入机制**保留**
            # （将来若走「本机模型做个性化」可复用），但**远程队列不得使用它**。
            # 另见 application.py 的 `_make_personal_context_loader`：那个函数即便
            # 被复用，也已被改成默认按 privacy_level 过滤 P1、且不外发 manual signals。
            #
            # 这里对远程分支做一次**硬剥离**而不是「只是不注入」：bundle_loader 是
            # 外部注入的，将来若被换成会塞个人数据的实现，请求体也不会因此变脏。
            # 本地分支不动——本机个性化正是要走 personal_context 的。
            if is_remote:
                bundle = _strip_personal_context(bundle)
            request_chars = len(json.dumps(bundle.to_public_dict(), ensure_ascii=False))
            try:
                result = provider.analyze(bundle)
                # 远程调用成功（无论后续是否格式错误，API请求已发出，token已消耗）
                if is_remote:
                    used += 1
                    remote_done += 1
                with self.database.connect() as connection:
                    self._persist_judgment(connection, job, job["provider"], result, now)
                    connection.execute(
                        """
                        UPDATE judgment_jobs SET status='succeeded',attempts=attempts+1,
                            request_chars=?,finished_at=?,last_error=''
                        WHERE job_id=?
                        """,
                        (request_chars, _iso(now), job["job_id"]),
                    )
                    if is_remote:
                        # 按次记账：这次调用已经真实发生并计费
                        self._bump_remote_usage(connection, now)
                        self._close_circuit(connection, job["provider"], now)
                        # 对端这次收了 ⇒ 把自适应间隔往回收一点（`relax` 有下限，
                        # 不会因为一次侥幸成功就一路打回基线）。
                        _remote_pacer.relax()
                summary["succeeded"] += 1
            except InvalidJudgmentError as exc:
                # 格式错误不是瞬态错误，重试无意义——立即降级本地处理，不重试
                attempts = int(job["attempts"]) + 1
                if is_remote:
                    used += 1
                    remote_done += 1
                fallback = self.local_provider.analyze(bundle)
                err_detail = str(exc)[:200]
                with self.database.connect() as connection:
                    self._persist_judgment(connection, job, "local", fallback, now)
                    connection.execute(
                        """
                        UPDATE judgment_jobs SET status='invalid_output',attempts=?,
                            request_chars=?,finished_at=?,last_error=?
                        WHERE job_id=?
                        """,
                        (attempts, request_chars, _iso(now), f"invalid_output: {err_detail}", job["job_id"]),
                    )
                    if is_remote:
                        # 格式错误也是"请求已发出、token 已消耗"，必须计入日预算
                        self._bump_remote_usage(connection, now)
                summary["failed"] += 1
            except RemoteProviderError as error:
                # API调用失败（网络/认证等），token可能已消耗也可能没有
                attempts = int(job["attempts"]) + 1
                if is_remote:
                    used += 1
                    remote_done += 1
                if error.kind == "auth":
                    # 认证错误：暂停当前任务+所有其他排队中的远程任务，不再重试
                    with self.database.connect() as connection:
                        connection.execute(
                            """
                            UPDATE judgment_jobs SET status='paused_auth',attempts=?,request_chars=?,
                                finished_at=?,last_error=? WHERE job_id=?
                            """,
                            (attempts, request_chars, _iso(now), error.kind, job["job_id"]),
                        )
                        # 同时暂停同一 provider 下其他排队中的任务，
                        # 避免不同 provider 之间互相影响（如 auth provider 出错不应暂停 rate/invalid provider）
                        connection.execute(
                            """
                            UPDATE judgment_jobs SET status='paused_auth',last_error='auth_paused'
                            WHERE provider=? AND status IN ('queued','retry','queued_budget')
                              AND job_id!=?
                            """,
                            (job["provider"], job["job_id"]),
                        )
                        # 认证失败的这一次调用也已经发出（对端回了 401），照实计费
                        self._bump_remote_usage(connection, now)
                    summary["failed"] += 1
                elif is_remote and attempts < MAX_REMOTE_RETRIES:
                    # ── 连续失败熔断（B3）─────────────────────────────────────
                    # 非鉴权失败原本**只有逐作业退避**（15/30/60/120 分钟），没有总闸：
                    # 对端整体不可用时，预算会被一路烧在"明知会失败"的调用上。
                    # 连续失败达阈值 ⇒ 冻结该 provider 的**全部**排队作业并停工；
                    # 任一作业成功即解除（见 `_close_circuit`）。
                    provider_name = job["provider"]
                    self._failure_streak[provider_name] = (
                        self._failure_streak.get(provider_name, 0) + 1
                    )
                    if error.kind == "rate_limit":
                        # 对端明确说"你发太快了"：立刻把自适应间隔成倍放慢，并**停掉
                        # 本轮剩下的远程作业**。继续发只是把同一个拒绝重复 N 遍，
                        # 每一次都照样计入日预算与熔断计数。
                        _remote_pacer.penalize()
                        remote_halted = True
                        _logger.warning(
                            "远程 provider %s 被限流（HTTP %s%s）：%s；已停发本轮剩余作业，"
                            "相邻请求间隔自适应放慢到 %.0f 秒",
                            provider_name,
                            error.status or 429,
                            f" {error.scope}" if error.scope else "",
                            error.detail or "对端未说明原因",
                            _remote_pacer.interval,
                        )
                    if self._failure_streak[provider_name] >= REMOTE_CIRCUIT_THRESHOLD:
                        with self.database.connect() as connection:
                            self._bump_remote_usage(connection, now)
                            self._open_circuit(
                                connection,
                                provider_name,
                                now,
                                # 限流引起的熔断带后缀：诊断页据此告诉用户
                                # "对端在限流，不用改任何配置"，而不是笼统的"连续失败"。
                                # 用 `scope`（短标识）而不是 `detail`（对端原话）——
                                # 这一列要被前缀匹配，必须是稳定字符串。
                                limit_scope=(
                                    error.scope if error.kind == "rate_limit" else ""
                                ),
                            )
                        summary["failed"] += 1
                        continue
                    # 远程任务还有重试机会：指数退避后重试。
                    # 普通失败（network/timeout/http_error）是在等对端恢复，用
                    # 15/30/60/120 分钟；**429 的语义不同**——它说的是"你发太快了"，
                    # 长退避会把积压作业攒到同一时刻一起到期、下一轮再来一次突发，
                    # 所以走独立且更短的 1/2/4 分钟
                    # （见 REMOTE_RATE_LIMIT_BACKOFF_MINUTES 的完整理由）。
                    if error.kind == "rate_limit":
                        rates = REMOTE_RATE_LIMIT_BACKOFF_MINUTES
                        delay = rates[min(attempts, len(rates)) - 1]
                    else:
                        delay = min(120, 15 * (2 ** (attempts - 1)))
                    next_attempt = _iso(now + timedelta(minutes=delay))
                    with self.database.connect() as connection:
                        connection.execute(
                            """
                            UPDATE judgment_jobs SET status='retry',attempts=?,request_chars=?,
                                next_attempt_at=?,last_error=? WHERE job_id=?
                            """,
                            (
                                attempts,
                                request_chars,
                                next_attempt,
                                error.kind,
                                job["job_id"],
                            ),
                        )
                        # 重试这一次的调用同样已经发出 —— 这正是旧判据漏掉的部分
                        self._bump_remote_usage(connection, now)
                    summary["failed"] += 1
                else:
                    # 超过重试次数 或 本地provider出错：降级本地
                    fallback = self.local_provider.analyze(bundle)
                    with self.database.connect() as connection:
                        self._persist_judgment(connection, job, "local", fallback, now)
                        connection.execute(
                            """
                            UPDATE judgment_jobs SET status='remote_error_fallback_local',attempts=?,
                                request_chars=?,finished_at=?,last_error=?
                            WHERE job_id=?
                            """,
                            (attempts, request_chars, _iso(now), error.kind, job["job_id"]),
                        )
                        if is_remote:
                            self._bump_remote_usage(connection, now)
                    summary["failed"] += 1
        return summary
