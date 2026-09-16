"""Optional OpenAI Responses adapter and a privacy-safe judgment job queue."""

from __future__ import annotations

import ipaddress
import json
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


DEFAULT_ENDPOINT = "https://api.openai.com/v1/responses"

DAILY_REMOTE_BUDGET = 2000
"""每日远程研判上限的**默认值**（2026-09-15 由 100 提到 2000）。

定 2000 的依据：Agnes AI 免费版对文本模型限的是 **20 RPM**（每分钟请求数），
**没有每日配额**；所以真正的稀缺资源是"每天总共做多少件"，不是"峰值多快"——
旧值 100 天/天把用户卡在了每天精确 100 条（真库 judgment_jobs 9/5–9/8 每天停在
100），而配额其实是白给的。用户可在设置页覆盖（0~100000，0 = 关闭远程）。

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
    """

    def __init__(self, interval: float = 0.0):
        self.interval = max(0.0, float(interval))
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
_remote_pacer = MinIntervalPacer(REMOTE_MIN_INTERVAL_SECONDS)


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


class RemoteProviderError(RuntimeError):
    def __init__(self, kind, status=None):
        self.kind = str(kind)
        self.status = status
        super().__init__(self.kind)

    @classmethod
    def from_http(cls, status):
        if status in {401, 403}:
            return cls("auth", status)
        if status == 429:
            return cls("rate_limit", status)
        return cls("http_error", status)


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
        raise RemoteProviderError.from_http(error.code) from error
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
    """

    name = "deepseek_chat"

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
            "max_tokens": 4000,
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
        # 关闭标志：退出时设置，立即中断任务处理，防止继续调用API
        self._shutdown = threading.Event()

    def shutdown(self):
        """安全关闭：设置关闭标志，清空所有待处理的远程任务。
        退出时必须先调用此方法，确保不会有新的API请求发出。"""
        self._shutdown.set()
        try:
            with self.database.connect() as connection:
                # 清空所有排队中的远程任务（包括重试、预算等待中的）
                connection.execute(
                    """
                    DELETE FROM judgment_jobs
                    WHERE provider!='local' AND status IN ('queued','retry','queued_budget')
                    """
                )
        except Exception:
            pass

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

    def _remote_used_today(self, connection, now):
        """统计今日所有实际发起过API请求的远程任务（无论成功/失败/格式错误），
        因为只要请求发出就消耗了token。"""
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

    def remote_used_today(self) -> int:
        """公共接口：今日已实际调用的远程 AI 次数（含失败/格式错误，诊断面板用）。"""
        now = self.now().astimezone(timezone.utc)
        with self.database.connect() as connection:
            return int(self._remote_used_today(connection, now))

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
        # 关闭状态下不处理任何任务
        if self._shutdown.is_set():
            return {"succeeded": 0, "deferred": 0, "failed": 0, "shutdown": True}
        # 远程任务最多重试3次（普通失败退避15/30/60分钟；429 走 1/2/4 分钟，
        # 见 REMOTE_RATE_LIMIT_BACKOFF_MINUTES），超过降级本地
        MAX_REMOTE_RETRIES = 4
        now = self.now().astimezone(timezone.utc)
        with self.database.connect() as connection:
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
        for row in rows:
            # 每个任务处理前检查关闭标志
            if self._shutdown.is_set():
                break
            job = dict(row)
            provider = self.providers.get(job["provider"])
            if provider is None:
                continue
            is_remote = job["provider"] != "local"
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
                    summary["failed"] += 1
                elif is_remote and attempts < MAX_REMOTE_RETRIES:
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
                    summary["failed"] += 1
        return summary
