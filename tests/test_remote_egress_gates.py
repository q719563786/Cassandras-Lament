"""remote_ai 三道远程出口闸的**分支**覆盖（qa · 2026-09-15）。

为什么单独写这一份：`build-artifacts/coverage/yuanjian_app.remote_ai.cover`
显示 P0-1 新增的剥离线**从来没被执行过**——

    remote_ai.py:88   46:  if bundle is None or not getattr(bundle, "personal_context", None):
    remote_ai.py:89   46:      return bundle
    remote_ai.py:90  >>>>>>     return _replace(bundle, personal_context=None)   ← 死的

也就是说：那道"硬剥离"闸门此前只走过「没什么可剥」的短路分支，真正的剥离语句
一次都没跑过。这与 `build-artifacts/qa_g1_leak_experiment.py` 的变异结论一致：
**把两道闸整个删掉、且不做任何重新注入时，零条测试变红** —— 等于把闸删了也没人
看得见，新防线无人看守。

根因是注入点：`run_due` 现在**不再调用** `personal_context_loader`，所以任何只在
`queue.personal_context_loader = ...` 上做手脚的测试都不会真的把个人数据送进
链路，闸门自然无事可做。本文件把注入点放在**真正的注入位置** —— `bundle_loader`
（`remote_ai.py:901`）—— 让 `remote_ai.py:919-920` 与 `:90` 这两条分支真的跑到。

本文件同时钉住三件**不能只看正面**的事：
1. 剥离只对远程生效：本地作业**必须**保住 `personal_context`（本机个性化正是要用它），
   否则就是"用删功能实现隐私"；
2. 体积/来源越限抛的必须是 `InvalidJudgmentError` **本身**，不是它的父类
   `ValueError`（子类关系使 `assertRaises(ValueError)` 分辨不出来）；
3. 两个上限都是**闭区间**：恰好 8 条来源、恰好 12000 字符必须放行。

第二批（同日）：把**第 4 道闸**也并进来 —— `_default_transport` 在
`socket.getaddrinfo` **之后**对解析出的每个地址再复查一次（`remote_ai.py:247-248`）。
它是所有防线里最靠外的一道：`_validate_endpoint` 只看**字面量**主机名，挡不住
「主机名合法、解析结果指向内网」这一整类（DNS 重绑定 / SSRF 拿
`169.254.169.254` 云元数据）。这道闸在 HEAD 里就存在、与本轮 P0-1 无关，
但此前同样**一次都没执行过**（`remote_ai.py:247-248` 全是 `>>>>>>`）。
"""

import json
import socket
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from yuanjian_app import remote_ai
from yuanjian_app.database import Database
from yuanjian_app.judgment_models import (
    MAX_BUNDLE_CHARACTERS,
    MAX_EVIDENCE_SOURCES,
    EvidenceBundle,
    EvidenceItem,
    InvalidJudgmentError,
)
from yuanjian_app.judgments import LocalHeuristicProvider
from yuanjian_app.remote_ai import (
    DeepSeekChatProvider,
    RateLimiter,
    RemoteProviderError,
    JudgmentQueue,
    _default_transport,
    _public_payload_for_request,
    _strip_personal_context,
    _validate_endpoint,
)

STAMP = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc).isoformat().replace(
    "+00:00", "Z"
)

# 个人数据的"指纹"：用中文长串，避免与任何真实英文键名撞车。
MARKER_INTEREST = "闸门指纹甲-现金流水位"
MARKER_SIGNAL = "闸门指纹乙-月初收入下降"
MARKER_FORECAST = "闸门指纹丙-房贷重定价"

# 公开证据包侧（**应该**外发）
PUBLIC_TITLE = "闸门公开标题丁"
PUBLIC_SUMMARY = "闸门公开摘要戊"
EVIDENCE_TITLE = "闸门公开来源己"
EVIDENCE_SUMMARY = "闸门公开来源摘要庚"


def personal_context():
    """照抄缺陷里那份个人画像的形状（利益对象 / 关系 / 近况 / 近期预测）。"""
    return {
        "用户个人近况（用户本人主动记录）": [
            {"recorded_at": "2026-09-01T00:00:00", "situation": MARKER_SIGNAL}
        ],
        "interests": {"objects": [{"name": MARKER_INTEREST, "category": "cashflow"}]},
        "recent_forecasts": [{"title": MARKER_FORECAST, "probability": 0.5}],
    }


def base_bundle(items_count=2, summary_chars=40, context=None):
    items = tuple(
        EvidenceItem(
            source_id="S-%d" % index,
            title="%s%d" % (EVIDENCE_TITLE, index),
            summary=EVIDENCE_SUMMARY + "x" * summary_chars,
            domain="news.example",
            url="https://news.example/%d" % index,
            published_at=STAMP,
        )
        for index in range(1, items_count + 1)
    )
    return EvidenceBundle(
        cluster_id="C-gate",
        title=PUBLIC_TITLE,
        summary=PUBLIC_SUMMARY,
        evidence_level="E2",
        categories=("policy",),
        items=items,
        personal_context=context,
    )


def serialized_length(bundle):
    """与被测代码同口径：`to_public_dict()` 的 JSON 字符数。"""
    return len(json.dumps(bundle.to_public_dict(), ensure_ascii=False))


def bundle_of_exact_length(target, items_count=1):
    """构造一个序列化后**恰好** target 字符的 bundle（ASCII 填充，1:1）。

    注意填充是**追加**到第 0 条的 summary 上，不是替换 —— 替换会把原来那串中文摘要
    （`ensure_ascii=False` 下每个汉字算 1 字符）一起删掉，长度就校准不准了。
    """
    base = base_bundle(items_count=items_count, summary_chars=0)
    delta = target - serialized_length(base)
    if delta < 1:
        raise AssertionError("目标长度 %d 比骨架还短" % target)
    fitted = replace(
        base,
        items=tuple(
            replace(item, summary=item.summary + "x" * delta) if index == 0 else item
            for index, item in enumerate(base.items)
        ),
    )
    actual = serialized_length(fitted)
    if actual != target:
        raise AssertionError("长度校准失败：想要 %d，实际 %d" % (target, actual))
    return fitted


def reply_payload():
    return {
        "fact_summary": "公开政策调整",
        "actors": ["主管部门"],
        "causal_chain": ["政策变化", "执行变化"],
        "uncertainties": ["细则待公布"],
        "horizons": ["未来30天"],
        "probability_low": 0.5,
        "probability_high": 0.7,
        "confidence": 0.65,
        "supporting_source_ids": ["S-1"],
        "counter_source_ids": [],
        "up_triggers": ["正式生效"],
        "down_triggers": ["延期"],
        "impact_categories": ["policy"],
        "personal_action": "偏向保守：先补充现金缓冲，暂缓非必要支出。",
        "gyw": {
            "stakeholders": "推动方：发文机关；阻力方：执行部门",
            "constraints": "资源约束：财政预算、配套立法",
            "least_resistance_path": "最小阻力路径：试点后推广",
            "counter_evidence": "反对证据：执行阻力、政策转向",
            "leading_indicators": "领先指标：试点公告、配套细则",
            "beneficiaries": [
                {"subject": "发文机关", "gain": "政绩落地", "evidence_refs": ["S-1"]}
            ],
            "cost_bearers": [{"subject": "[推断]执行部门", "cost": "配套资源压力"}],
            "historical_parallel": None,
            "observable_signals": ["配套细则挂网", "部门预算批复"],
        },
    }


class CapturingTransport:
    """记录真实发出的请求体，不联网。"""

    def __init__(self, response=None):
        self.response = response or {
            "choices": [{"message": {"content": json.dumps(reply_payload(), ensure_ascii=False)}}]
        }
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": body})
        return self.response

    @property
    def body_text(self):
        return json.dumps(self.calls[-1]["body"], ensure_ascii=False)


class RecordingLocalProvider:
    """把收到的 bundle 记下来，再交给真实本地启发式。"""

    def __init__(self):
        self.inner = LocalHeuristicProvider()
        self.seen = []

    def analyze(self, bundle):
        self.seen.append(bundle)
        return self.inner.analyze(bundle)


# ---------------------------------------------------------------------------
# 第 1 道闸：_strip_personal_context（remote_ai.py:75-90）
# ---------------------------------------------------------------------------


class StripPersonalContextBranchTests(unittest.TestCase):
    """四条分支逐一走到，重点是 `:90` 那条此前从未执行的剥离语句。"""

    def test_none_bundle_passes_through(self):
        self.assertIsNone(_strip_personal_context(None))

    def test_bundle_without_context_is_returned_unchanged(self):
        bundle = base_bundle()
        self.assertIsNone(bundle.personal_context, "夹具本身就不该带个人上下文")
        self.assertIs(_strip_personal_context(bundle), bundle)

    def test_falsy_context_is_not_silently_treated_as_a_leak(self):
        """空 dict 是 falsy，会被短路分支放过；钉住它也确实不外发任何东西。

        短路本身没问题（空画像等于没有画像），但必须证明"放过"的后果依然干净：
        `to_public_dict()` 对 falsy 的 `personal_context` 不输出该键。
        """
        bundle = base_bundle(context={})
        self.assertIs(_strip_personal_context(bundle), bundle)
        self.assertNotIn("personal_context", bundle.to_public_dict())

    def test_truthy_context_is_replaced_with_none_and_the_original_is_untouched(self):
        """`remote_ai.py:90` —— 真正的剥离。

        两侧都要钉：剥离后的新 bundle 上没有上下文；**原 bundle 也不许被就地篡改**
        （`EvidenceBundle` 是 frozen dataclass，剥离必须是"换一个"，不是"改这个"）。
        """
        bundle = base_bundle(context=personal_context())
        self.assertIn("personal_context", bundle.to_public_dict(), "夹具没把上下文带上")

        stripped = _strip_personal_context(bundle)

        self.assertIsNot(stripped, bundle, "剥离必须产生新对象，不能就地改 frozen 的 bundle")
        self.assertIsNone(stripped.personal_context)
        self.assertNotIn("personal_context", stripped.to_public_dict())
        for marker in (MARKER_INTEREST, MARKER_SIGNAL, MARKER_FORECAST):
            self.assertNotIn(marker, json.dumps(stripped.to_public_dict(), ensure_ascii=False))
        # 反向：原对象仍带着上下文（证明我们没做破坏性改写）
        self.assertIsNotNone(bundle.personal_context)

    def test_stripping_does_not_touch_the_public_evidence(self):
        """剥离只许摘掉个人上下文，公开证据必须一个字段不少。"""
        bundle = base_bundle(context=personal_context())
        stripped = _strip_personal_context(bundle)

        self.assertEqual(stripped.items, bundle.items)
        self.assertEqual(stripped.cluster_id, bundle.cluster_id)
        self.assertEqual(stripped.title, bundle.title)
        self.assertEqual(stripped.evidence_level, bundle.evidence_level)
        self.assertEqual(stripped.categories, bundle.categories)
        self.assertEqual(stripped.system_instruction, bundle.system_instruction)


# ---------------------------------------------------------------------------
# 第 2 道闸：_public_payload_for_request（remote_ai.py:93-119）
# ---------------------------------------------------------------------------


class PublicPayloadBranchTests(unittest.TestCase):
    def test_published_context_is_dropped_even_before_the_size_checks(self):
        """`:114` 的 pop 必须真的摘掉 `to_public_dict()` 已经吐出来的那个键。

        这条专门制造"上游没剥"的场景：直接拿一个带上下文的 bundle 调这个助手，
        `to_public_dict()` 会把 `personal_context` 写进公开字典，pop 若不生效就会外发。
        """
        bundle = base_bundle(context=personal_context())
        self.assertIn("personal_context", bundle.to_public_dict(), "夹具没把上下文带上")

        public = _public_payload_for_request(bundle)

        self.assertNotIn("personal_context", public)
        text = json.dumps(public, ensure_ascii=False)
        for marker in (MARKER_INTEREST, MARKER_SIGNAL, MARKER_FORECAST):
            self.assertNotIn(marker, text)
        # 反向：公开字段仍在（不许用删功能实现隐私）
        self.assertEqual(public["cluster"]["title"], PUBLIC_TITLE)
        self.assertEqual(len(public["evidence"]), 2)
        self.assertIn(EVIDENCE_TITLE, text)

    def test_source_count_over_the_limit_raises_the_exact_domain_error(self):
        """来源数越限：抛的必须是 `InvalidJudgmentError` 本身，不是裸 `ValueError`。

        `InvalidJudgmentError` 是 `ValueError` 的子类，所以 `assertRaises(ValueError)`
        分辨不出"被拦下"和"用错类型抛"。裸 `ValueError` 会穿过 `run_due` 的两个
        except 分支冲出整轮认知 —— 类型本身就是契约的一部分。
        """
        too_many = base_bundle(items_count=MAX_EVIDENCE_SOURCES + 1, summary_chars=1)
        self.assertEqual(len(too_many.items), MAX_EVIDENCE_SOURCES + 1)
        # 隔离两条校验：字符数留在上限内，确认拦下它的是来源数（而不是体积）
        self.assertLess(
            serialized_length(too_many),
            MAX_BUNDLE_CHARACTERS,
            "夹具同时越过了体积上限，就没法确认是来源数校验拦下的",
        )

        with self.assertRaises(InvalidJudgmentError) as caught:
            _public_payload_for_request(too_many)

        self.assertIs(
            type(caught.exception),
            InvalidJudgmentError,
            "抛的是 %s —— 裸 ValueError 会冲出 run_due 打断整轮" % type(caught.exception).__name__,
        )
        self.assertNotEqual(type(caught.exception), ValueError)

    def test_character_count_over_the_limit_raises_the_exact_domain_error(self):
        """字符数越限：同样必须是 `InvalidJudgmentError` 本身。"""
        oversized = bundle_of_exact_length(MAX_BUNDLE_CHARACTERS + 1)
        self.assertEqual(len(oversized.items), 1, "隔离：来源数不越限")
        self.assertEqual(serialized_length(oversized), MAX_BUNDLE_CHARACTERS + 1)

        with self.assertRaises(InvalidJudgmentError) as caught:
            _public_payload_for_request(oversized)

        self.assertIs(type(caught.exception), InvalidJudgmentError)
        self.assertNotEqual(type(caught.exception), ValueError)

    def test_exactly_at_both_limits_is_allowed(self):
        """两个上限都是**闭区间**：恰好 8 条来源、恰好 12,000 字符必须放行。

        只钉"越限被拒"会让实现退化成 `>=` 也能全绿，那会误伤正好卡在上限的合法包。
        """
        at_limit = bundle_of_exact_length(MAX_BUNDLE_CHARACTERS, items_count=MAX_EVIDENCE_SOURCES)
        self.assertEqual(len(at_limit.items), MAX_EVIDENCE_SOURCES)
        self.assertEqual(serialized_length(at_limit), MAX_BUNDLE_CHARACTERS)

        public = _public_payload_for_request(at_limit)

        self.assertEqual(len(public["evidence"]), MAX_EVIDENCE_SOURCES)

    def test_missing_evidence_key_does_not_raise(self):
        """`:115` 的 `or ()` 分支：没有 evidence 键时不许因为 None 而炸。"""

        class NoEvidence:
            """最小替身：只提供 `to_public_dict()`，故意不含 evidence。"""

            def to_public_dict(self):
                return {"cluster": {"cluster_id": "C-empty"}}

        result = _public_payload_for_request(NoEvidence())
        self.assertEqual(result, {"cluster": {"cluster_id": "C-empty"}})


# ---------------------------------------------------------------------------
# 第 3 道闸：_validate_endpoint（remote_ai.py:122-135）—— 地址不许指向本机
# ---------------------------------------------------------------------------


class EndpointGateTests(unittest.TestCase):
    """`:128`（localhost）与 `:134`（私网）两条拒绝分支此前都是死的。"""

    def test_localhost_variants_are_rejected(self):
        for endpoint in (
            "https://localhost/v1/chat/completions",
            "https://api.localhost/v1/chat/completions",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    _validate_endpoint(endpoint)

    def test_private_and_loopback_addresses_are_rejected(self):
        for endpoint in (
            "https://127.0.0.1/v1",
            "https://10.1.2.3/v1",
            "https://192.168.1.5/v1",
            "https://172.16.9.9/v1",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    _validate_endpoint(endpoint)

    def test_non_https_and_credentialed_urls_are_rejected(self):
        for endpoint in (
            "http://api.openai.com/v1/responses",
            "https://user:pass@api.openai.com/v1/responses",
            "not-a-url",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    _validate_endpoint(endpoint)

    def test_public_https_endpoint_is_accepted(self):
        """反向：真·公网 HTTPS 不许被误伤。"""
        endpoint = "https://api.openai.com/v1/responses"
        self.assertEqual(_validate_endpoint(endpoint), endpoint)


# ---------------------------------------------------------------------------
# 第 4 道闸：_default_transport 的 **DNS 解析之后**地址复查
#          （remote_ai.py:238-248）—— 防 DNS 重绑定 / SSRF
# ---------------------------------------------------------------------------


class _FakeResponse:
    """`urlopen` 返回值的替身：它要能被 `with` 用，还要有 `read(n)`。"""

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self._payload


class _FakeUrlOpen:
    """`urllib.request.urlopen` 的替身：记下每次调用；要么回一段固定字节，要么抛指定异常。"""

    def __init__(self, payload=b'{"ok": true}', error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "method": request.get_method(),
            }
        )
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.payload)


def getaddrinfo_answer(*addresses):
    """照 `socket.getaddrinfo` 的形状造返回值；被测代码只取 `result[4][0]`。"""
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))
        for address in addresses
    ]


class _TransportHarness(unittest.TestCase):
    """只放替身夹具，**刻意一条 `test_` 方法都没有**。

    原因：`unittest` 会把父类的 `test_*` 一起继承下去。若把夹具放在
    `DefaultTransportAddressGateTests` 这类**有**用例的类里再继承它，7 条用例会被
    每个子类各跑一遍（20 → 47 条假数字），用例数虚高还掩盖真实覆盖。
    """

    URL = "https://rebind.example.com/v1/chat/completions"
    PUBLIC_ADDRESS = "93.184.216.34"

    def gate(
        self,
        addresses=(),
        resolver_error=None,
        payload=b'{"ok": true}',
        send_error=None,
    ):
        """装好替身：DNS 答复可控，`urlopen` 只记录（或按需抛错），绝不真发。"""
        opener = _FakeUrlOpen(payload, send_error)
        opener_patch = mock.patch(
            "yuanjian_app.remote_ai.urllib.request.urlopen", opener
        )
        opener_patch.start()
        self.addCleanup(opener_patch.stop)

        if resolver_error is not None:
            resolver = mock.Mock(side_effect=resolver_error)
        else:
            resolver = mock.Mock(return_value=getaddrinfo_answer(*addresses))
        resolver_patch = mock.patch(
            "yuanjian_app.remote_ai.socket.getaddrinfo", resolver
        )
        resolver_patch.start()
        self.addCleanup(resolver_patch.stop)
        return opener, resolver

    def send(self, url=None):
        return _default_transport(
            url or self.URL, {"Content-Type": "application/json"}, {"hello": "world"}, 5
        )


class DefaultTransportAddressGateTests(_TransportHarness):
    """`:247-248` —— `_validate_endpoint` 只看**字面量**主机名，挡不住
    「主机名本身合法、解析结果指向内网」这一类。

    正常路径上 `OpenAIResponsesProvider.__init__` 会先过 `_validate_endpoint`，
    但那只覆盖字面量（`127.0.0.1`、`localhost`）。攻击者用 `rebind.example.com`
    这种看起来人畜无害的名字，把 A 记录指到 `127.0.0.1` 或云元数据端点
    `169.254.169.254`，`_validate_endpoint` 一路放行。真正兜底的是这里 ——
    **解析之后再复查**。

    本类只调 `_default_transport`，故意绕过 provider 构造，把这道闸单独拎出来验。
    """

    def test_hostname_resolving_to_loopback_is_blocked_and_nothing_is_sent(self):
        """DNS 重绑定的最小复现：名字合法，A 记录是 `127.0.0.1`。

        同时钉死"拒绝之后不许再发"—— 光看抛异常不够，若异常在 `urlopen` 之后才抛，
        请求早就出去了。
        """
        opener, _ = self.gate(addresses=["127.0.0.1"])

        with self.assertRaises(RemoteProviderError) as caught:
            self.send()

        self.assertEqual(caught.exception.kind, "unsafe_endpoint")
        self.assertEqual(opener.calls, [], "闸门已判定端点不安全，请求却还是发出去了")

    def test_private_link_local_and_ipv6_answers_are_all_blocked(self):
        """私网 / 链路本地（云元数据）/ IPv6 环回 / IPv6 ULA 全部要拦。

        `169.254.169.254` 是 AWS/GCP/阿里云都用的实例元数据地址，SSRF 的首选靶子。
        """
        for address in (
            "10.1.2.3",
            "192.168.1.5",
            "172.16.9.9",
            "169.254.169.254",
            "::1",
            "fd00::1",
        ):
            with self.subTest(address=address):
                opener, _ = self.gate(addresses=[address])

                with self.assertRaises(RemoteProviderError) as caught:
                    self.send()

                self.assertEqual(caught.exception.kind, "unsafe_endpoint")
                self.assertEqual(opener.calls, [])

    def test_a_mixed_answer_is_rejected_when_any_address_is_internal(self):
        """量词是 `any(...)` 而不是 `all(...)`：轮询式 DNS 里只要混进一个内网地址，
        整包必须拒绝 —— 否则攻击者用「一半公网一半内网」的答复就能穿透。

        这条专门钉那个量词：把它改成 `all(...)` 时，上面逐条内网的用例仍然全绿，
        只有本用例会红。
        """
        opener, _ = self.gate(addresses=[self.PUBLIC_ADDRESS, "10.0.0.7"])

        with self.assertRaises(RemoteProviderError) as caught:
            self.send()

        self.assertEqual(caught.exception.kind, "unsafe_endpoint")
        self.assertEqual(opener.calls, [])

    def test_an_empty_answer_is_blocked(self):
        """`:247` 里的 `not addresses`：解析成功但一个地址都没给出，等于**没验过**，
        必须按不安全处理，不能因为"没有地址违反规则"而放行空集。
        """
        opener, _ = self.gate(addresses=[])

        with self.assertRaises(RemoteProviderError) as caught:
            self.send()

        self.assertEqual(caught.exception.kind, "unsafe_endpoint")
        self.assertEqual(opener.calls, [])

    def test_resolution_failure_is_network_not_unsafe_endpoint(self):
        """`:245-246`：DNS 查不动是 `network`，跟"解析到内网"不是一回事。

        两者最终都会进 `run_due` 的重试/降级，但 `last_error` 里存的 kind 不同：
        运维要靠它分辨「网络抖动，等它自己好」和「有人在打 SSRF，得去看日志」。
        """
        opener, _ = self.gate(resolver_error=socket.gaierror(-2, "Name or service not known"))

        with self.assertRaises(RemoteProviderError) as caught:
            self.send()

        self.assertEqual(caught.exception.kind, "network")
        self.assertEqual(opener.calls, [])

    def test_a_global_answer_passes_and_the_request_really_goes_out(self):
        """反向：公网地址必须放行，而且真的组装出 POST 请求。

        没有这条，上面几条"全部拒绝"的实现也能全绿 —— 那测的是"闸门关死了"，
        不是"闸门会开门"。
        """
        opener, resolver = self.gate(addresses=[self.PUBLIC_ADDRESS])

        result = self.send()

        self.assertEqual(
            resolver.call_args.args[0], "rebind.example.com", "复查不是按主机名去解析的"
        )
        self.assertEqual(len(opener.calls), 1, "公网地址被误伤，请求没发出去")
        self.assertEqual(opener.calls[0]["method"], "POST")
        self.assertTrue(opener.calls[0]["url"].startswith("https://rebind.example.com/"))
        self.assertEqual(opener.calls[0]["timeout"], 5)
        self.assertEqual(result, {"ok": True}, "响应体没有被解析成 JSON 返回")

    def test_a_literal_internal_address_is_refused_by_this_gate_as_well(self):
        """第二条独立防线：就算有人绕过 `_validate_endpoint`（换 provider、直接调
        传输层），`_default_transport` 自己也会因为 `127.0.0.1` 不是全局地址而拒绝。

        防线不该只有一层，也不该只有一处的调用者会记得去查。
        """
        opener, _ = self.gate(addresses=["127.0.0.1"])

        with self.assertRaises(RemoteProviderError) as caught:
            self.send(url="https://127.0.0.1/v1")

        self.assertEqual(caught.exception.kind, "unsafe_endpoint")
        self.assertEqual(opener.calls, [])


# ---------------------------------------------------------------------------
# 同一条通道的其余部分：`_default_transport` 的**错误分类**
#          （remote_ai.py:258-269 + RemoteProviderError.from_http :145-150）
# ---------------------------------------------------------------------------


class DefaultTransportErrorTaxonomyTests(_TransportHarness):
    """错误分类不是"好看的标签"，它**直接决定 `run_due` 的分支**：

    - `auth`        → `paused_auth`，并把同 provider 的排队任务一起停掉（`:964-983`）
    - `network` / `timeout` / `rate_limit` / `http_error` → 指数退避重试（`:985-1003`）
    - `InvalidJudgmentError` → 立即降级 local、**不重试**（`:939-957`）

    映射错了不会报错，只会让队列做出错误动作：把 429 当成 `auth` 会误停整条 provider；
    把非 JSON 响应当成 `RemoteProviderError` 会拿预算反复重试一段永远解析不出来的文本。
    """

    def test_http_status_codes_map_to_the_kinds_run_due_branches_on(self):
        """401/403 → `auth`；429 → `rate_limit`；其余 → `http_error`。

        `status` 必须一并带出来：`run_due` 把它写进 `last_error`，运维靠它定位。
        """
        for status, expected in (
            (401, "auth"),
            (403, "auth"),
            (429, "rate_limit"),
            (500, "http_error"),
        ):
            with self.subTest(status=status):
                self.gate(
                    addresses=[self.PUBLIC_ADDRESS],
                    send_error=urllib.error.HTTPError(self.URL, status, "boom", {}, None),
                )

                with self.assertRaises(RemoteProviderError) as caught:
                    self.send()

                self.assertEqual(caught.exception.kind, expected)
                self.assertEqual(caught.exception.status, status)

    def test_url_error_maps_to_network(self):
        """连接被重置 / 底层网络失败 → `network`，可重试。"""
        self.gate(
            addresses=[self.PUBLIC_ADDRESS],
            send_error=urllib.error.URLError("connection reset by peer"),
        )

        with self.assertRaises(RemoteProviderError) as caught:
            self.send()

        self.assertEqual(caught.exception.kind, "network")

    def test_timeout_maps_to_timeout(self):
        """超时单独成一类：`network` 与 `timeout` 的重试节奏、告警话术不一样。"""
        self.gate(addresses=[self.PUBLIC_ADDRESS], send_error=TimeoutError("timed out"))

        with self.assertRaises(RemoteProviderError) as caught:
            self.send()

        self.assertEqual(caught.exception.kind, "timeout")

    def test_a_non_json_response_is_a_judgment_error_so_it_is_not_retried(self):
        """`:268-269`：响应不是 JSON（含非法 UTF-8）时抛的必须是 `InvalidJudgmentError`。

        重试一段永远解析不出来的文本只会白烧配额，`run_due` 对它的处理正是
        「立即降级 local」。若这里错抛 `RemoteProviderError`，任何"能跑通"的测试都
        发现不了，只会在真实环境里表现为预算被吃光。
        """
        for payload in (b"not json at all", b"\xff\xfe\x00broken"):
            with self.subTest(payload=payload):
                self.gate(addresses=[self.PUBLIC_ADDRESS], payload=payload)

                with self.assertRaises(InvalidJudgmentError) as caught:
                    self.send()

                self.assertIs(
                    type(caught.exception),
                    InvalidJudgmentError,
                    "抛了 %s —— 那会被 run_due 当成可重试错误"
                    % type(caught.exception).__name__,
                )


class AgnesPermitOrderingTests(_TransportHarness):
    """`：250-251`：限流许可是**消耗品**。

    注释写明「DNS/SSRF校验通过后再获取许可，避免无效端点浪费配额」，所以顺序本身
    就是契约：端点没验过就先去领许可，一次 SSRF 探测就能白吃一个 20 RPM 的名额。
    """

    AGNES_URL = "https://apihub.agnes-ai.com/v1/chat/completions"

    def limiter(self):
        spy = mock.Mock()
        patcher = mock.patch.object(remote_ai, "_agnes_ai_limiter", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return spy

    def test_the_permit_is_taken_for_agnes_hosts_and_only_after_the_address_check(self):
        """正向：agnes 端点要真的去领许可，否则免费额度会被 429 打回来。"""
        spy = self.limiter()
        opener, _ = self.gate(addresses=[self.PUBLIC_ADDRESS])

        self.send(url=self.AGNES_URL)

        self.assertEqual(spy.acquire.call_count, 1, "agnes 端点没有领取限流许可")
        self.assertEqual(len(opener.calls), 1, "领了许可却没把请求发出去")

    def test_no_permit_is_wasted_on_a_host_that_never_got_validated(self):
        """反向：地址闸先拦下时，一个许可都不该被领走。"""
        spy = self.limiter()
        self.gate(addresses=["169.254.169.254"])

        with self.assertRaises(RemoteProviderError) as caught:
            self.send(url=self.AGNES_URL)

        self.assertEqual(caught.exception.kind, "unsafe_endpoint")
        self.assertEqual(
            spy.acquire.call_count,
            0,
            "端点还没验过就把限流许可领走了 —— 一次无效请求白吃一个 RPM 名额",
        )


# ---------------------------------------------------------------------------
# 限流器分支（remote_ai.py:53-64）—— 此前整个函数体是死的
# ---------------------------------------------------------------------------


class FakeTime:
    """替身时钟：sleep 只推进自己的秒表，不真的睡。"""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class RateLimiterBranchTests(unittest.TestCase):
    def patch_time(self):
        fake = FakeTime()
        patcher = mock.patch.object(remote_ai, "time", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_permits_up_to_the_limit_are_granted_without_waiting(self):
        fake = self.patch_time()
        limiter = RateLimiter(3)

        for _ in range(3):
            limiter.acquire()

        self.assertEqual(fake.slept, [], "没满窗口却等过了")
        self.assertEqual(len(limiter._timestamps), 3)

    def test_expired_permits_are_filtered_out_of_the_window(self):
        """`:58` 的过滤：61 秒前的许可必须被清掉，腾出名额且不等待。"""
        fake = self.patch_time()
        limiter = RateLimiter(1)

        limiter.acquire()
        fake.now += 61.0
        limiter.acquire()

        self.assertEqual(fake.slept, [], "许可已过期却还在等")
        self.assertEqual(len(limiter._timestamps), 1, "过期许可没有被过滤掉")

    def test_when_the_window_is_full_it_waits_instead_of_dropping(self):
        """`:62-64`：窗口满了要**阻塞等待**，不能丢请求、也不能超发。"""
        fake = self.patch_time()
        limiter = RateLimiter(1)

        limiter.acquire()
        limiter.acquire()

        self.assertTrue(fake.slept, "窗口已满却没有等待 —— 请求被静默丢弃或超发了")
        self.assertTrue(all(step <= 5.0 for step in fake.slept), "单次等待超过了 5 秒上限")
        self.assertEqual(len(limiter._timestamps), 1, "超发了许可")
        self.assertGreaterEqual(fake.now, 60.0, "等待时长不足以让旧许可过期")

    def test_rpm_is_clamped_so_a_zero_setting_cannot_deadlock(self):
        """`rpm=0` 若不做下限钳制，窗口恒满 → `acquire()` 永远出不来。"""
        self.patch_time()
        limiter = RateLimiter(0)
        self.assertEqual(limiter.rpm, 1)
        limiter.acquire()


# ---------------------------------------------------------------------------
# 端到端：注入点在 bundle_loader（remote_ai.py:901），剥离在 :919-920
# ---------------------------------------------------------------------------


class RunDueStripScopeTests(unittest.TestCase):
    """把个人上下文从**真正的注入点**送进去，看它能不能出门。

    只在 `queue.personal_context_loader` 上做手脚是抓不住的：`run_due` 现在
    根本不调用那个加载器，所以无论它返回什么，链路里都不会出现个人数据 ——
    这类测试会"绿得毫无意义"。真正的注入点是 `bundle_loader`（`remote_ai.py:901`）。
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.clock = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        self.addCleanup(self.temporary.cleanup)
        with self.database.connect() as connection:
            for cluster_id in ("C-remote", "C-local"):
                connection.execute(
                    "INSERT INTO event_clusters(cluster_id,title,summary,first_seen_at,"
                    "last_seen_at,evidence_level,evidence_hash,categories_json,status,"
                    "needs_judgment,independent_domains,primary_source_count,created_at,"
                    "updated_at) VALUES (?,?,?,?,?,'E2','h','[\"policy\"]','active',1,1,1,?,?)",
                    (
                        cluster_id,
                        "标题" + cluster_id,
                        "",
                        STAMP,
                        STAMP,
                        STAMP,
                        STAMP,
                    ),
                )

    def queue(self, transport, local_provider):
        loaded = []

        def loader(cluster_id):
            loaded.append(cluster_id)
            return replace(base_bundle(), personal_context=personal_context())

        queue = JudgmentQueue(
            self.database,
            providers={
                "deepseek": DeepSeekChatProvider(
                    model="test-model",
                    token_loader=lambda: "unit-test-token",
                    transport=transport,
                ),
                "local": local_provider,
            },
            bundle_loader=loader,
            local_provider=LocalHeuristicProvider(),
            now=lambda: self.clock,
        )
        return queue, loaded

    def jobs(self):
        with self.database.connect() as connection:
            return {
                row["cluster_id"]: dict(row)
                for row in connection.execute("SELECT * FROM judgment_jobs")
            }

    def test_remote_job_ships_nothing_personal_even_when_the_loader_injects_it(self):
        """`remote_ai.py:919-920` + `:90` 一起走到：加载器真的注入了，出门的包里没有。"""
        transport = CapturingTransport()
        queue, loaded = self.queue(transport, RecordingLocalProvider())
        queue.enqueue("C-remote", "hash-remote", "deepseek")

        summary = queue.run_due(limit=5, remote_limit=5)

        self.assertEqual(loaded, ["C-remote"], "加载器没被调用 —— 那这条测试什么也没证明")
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(len(transport.calls), 1, "远程请求没有真的发出去")

        text = transport.body_text
        self.assertNotIn("personal_context", text, "个人上下文键漏到了请求体里")
        for marker in (MARKER_INTEREST, MARKER_SIGNAL, MARKER_FORECAST):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, text)
        # 反向：公开证据必须还在（不许用"什么都不发"来糊弄）
        self.assertIn(PUBLIC_TITLE, text)
        self.assertIn(EVIDENCE_TITLE, text)
        self.assertIn("E2", text)

    def test_local_job_keeps_the_personal_context_because_local_is_the_personalized_path(self):
        """反向的边界：剥离**只对远程生效**，不许顺手把本地也剥了。

        本机个性化正是靠 `personal_context`；若有人把 `if is_remote:` 去掉、或把剥离
        挪到 `bundle_loader` 之后无条件执行，本地就再也拿不到画像了 —— 这条会红。
        """
        transport = CapturingTransport()
        local = RecordingLocalProvider()
        queue, loaded = self.queue(transport, local)
        queue.enqueue("C-local", "hash-local", "local")

        summary = queue.run_due(limit=5, remote_limit=5)

        self.assertEqual(loaded, ["C-local"])
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(transport.calls, [], "本地作业不该发出任何网络请求")
        self.assertEqual(len(local.seen), 1, "本地 provider 没被调用")
        seen = local.seen[0]
        self.assertIsNotNone(
            seen.personal_context,
            "本地作业的个人上下文被剥掉了 —— 本机个性化路径断了（剥离只该对远程生效）",
        )
        self.assertIn(MARKER_INTEREST, json.dumps(seen.personal_context, ensure_ascii=False))
        # 顺带确认本地作业确实落了库
        self.assertEqual(self.jobs()["C-local"]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
