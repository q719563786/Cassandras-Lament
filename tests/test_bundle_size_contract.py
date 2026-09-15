"""缺陷 2（P2）· 体积契约必须在**所有** provider 上生效，且越限不许打断整轮。

`judgment_models.py` 定了 `MAX_BUNDLE_CHARACTERS=12000` / `MAX_EVIDENCE_SOURCES=8`，
但只有 `OpenAIResponsesProvider._request_body` 校验；`DeepSeekChatProvider`
**完全没有校验**（实测曾实发 18,985 字符）。

更严重的是**失败形态**：越限时抛的是裸 `ValueError`，它既不是
`InvalidJudgmentError` 也不是 `RemoteProviderError`，所以会穿过
`JudgmentQueue.run_due` 的两个 except 分支直接冲出 —— 该轮认知剩下的
增量映射、通知、首页刷新全部不执行，而那个 job 永远停在 `queued`
（下次 `run_due` 再抛一次，无限循环）。

所以本文件钉两件事：
1. 越限的**异常类型**是 `InvalidJudgmentError`（可被降级逻辑接住）；
2. 越限时**整轮不中断**：该 job 降级走 local 分支，后面的 job 照常处理。
3. 边界双向钉死：恰好 12,000 字符放行、12,001 拒绝。
"""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    JudgmentQueue,
    OpenAIResponsesProvider,
)

STAMP = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc).isoformat().replace(
    "+00:00", "Z"
)


def payload():
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
        "personal_action": "偏向保守：先补充现金缓冲。",
        "gyw": {
            "stakeholders": "推动方：发文机关；阻力方：执行部门",
            "constraints": "资源约束：财政预算、配套立法",
            "least_resistance_path": "最小阻力路径：试点后推广",
            "counter_evidence": "反对证据：执行阻力、政策转向",
            "leading_indicators": "领先指标：试点公告、配套细则",
            "beneficiaries": [
                {"subject": "发文机关", "gain": "政绩落地", "evidence_refs": ["S-1"]}
            ],
            "cost_bearers": [
                {"subject": "[推断]执行部门", "cost": "配套资源压力", "evidence_refs": []}
            ],
            "historical_parallel": None,
            "observable_signals": ["配套细则挂网", "部门预算批复"],
        },
    }


def bundle_with(items_count=2, summary_chars=120):
    items = tuple(
        EvidenceItem(
            source_id="S-%d" % index,
            title="公开来源标题 %d" % index,
            summary="x" * summary_chars,
            domain="news.example",
            url="https://news.example/%d" % index,
            published_at=STAMP,
        )
        for index in range(1, items_count + 1)
    )
    return EvidenceBundle(
        cluster_id="C-1",
        title="公开事件标题",
        summary="公开事件摘要",
        evidence_level="E2",
        categories=("policy",),
        items=items,
    )


def serialized_length(bundle):
    """与被测代码同口径：`to_public_dict()` 的 JSON 字符数。"""
    return len(json.dumps(bundle.to_public_dict(), ensure_ascii=False))


def bundle_of_exact_length(target):
    """构造一个序列化后**恰好** target 字符的 bundle（用 ASCII 填充，1:1）。"""
    base = bundle_with(items_count=1, summary_chars=0)
    delta = target - serialized_length(base)
    if delta < 1:
        raise AssertionError("目标长度 %d 比骨架还短" % target)
    fitted = replace(
        base,
        items=(replace(base.items[0], summary="x" * delta),),
    )
    actual = serialized_length(fitted)
    if actual != target:
        raise AssertionError("长度校准失败：想要 %d，实际 %d" % (target, actual))
    return fitted


PROVIDER_NAMES = ("openai_responses", "deepseek_chat")


def make_provider(name, transport):
    if name == "openai_responses":
        return OpenAIResponsesProvider(
            model="test-model", token_loader=lambda: "unit-test-token", transport=transport
        )
    if name == "deepseek_chat":
        return DeepSeekChatProvider(
            model="test-model", token_loader=lambda: "unit-test-token", transport=transport
        )
    raise AssertionError("未知 provider: %s" % name)


def providers_with(transport):
    return tuple((name, make_provider(name, transport)) for name in PROVIDER_NAMES)


def reply_for(name):
    """按该 provider 的响应格式造一份能通过校验的输出。"""
    encoded = json.dumps(payload(), ensure_ascii=False)
    if name == "openai_responses":
        return {"output_text": encoded}
    return {"choices": [{"message": {"content": encoded}}]}


class SizeContractTests(unittest.TestCase):
    """校验必须发生在构造请求体的那一刻，且以领域异常报错。"""

    def assert_rejected_with_domain_error(self, provider, bundle, reason):
        """断言被拦下、且抛的是 `InvalidJudgmentError`。

        裸 `ValueError` 会穿过 `run_due` 的两个 except 分支冲出整轮，所以
        "抛了什么类型"和"有没有拦住"同等重要。
        """
        try:
            provider._request_body(bundle)
        except InvalidJudgmentError:
            return
        except ValueError as error:
            self.fail(
                "%s：越限时抛的是裸 %s（%s）—— 它会冲出 run_due 打断整轮认知；"
                "必须是 InvalidJudgmentError"
                % (reason, type(error).__name__, error)
            )
        else:
            self.fail("%s：越限的证据包没有被拦下" % reason)

    def oversized(self):
        # 8 条 × 3000 字符摘要，远超 12,000；来源数仍在 8 以内
        return bundle_with(items_count=MAX_EVIDENCE_SOURCES, summary_chars=3000)

    def too_many_sources(self):
        return bundle_with(items_count=MAX_EVIDENCE_SOURCES + 1, summary_chars=10)

    def test_oversized_bundle_is_rejected_by_every_provider(self):
        oversized = self.oversized()
        self.assertGreater(serialized_length(oversized), MAX_BUNDLE_CHARACTERS)

        for name, provider in providers_with(lambda *args: {}):
            with self.subTest(provider=name):
                self.assert_rejected_with_domain_error(
                    provider, oversized, "%s 未校验字符上限" % name
                )

    def test_more_than_eight_sources_is_rejected_by_every_provider(self):
        too_many = self.too_many_sources()
        self.assertEqual(len(too_many.items), MAX_EVIDENCE_SOURCES + 1)
        # 必须把两条校验隔离开：字符数留在上限以内，才能确认拦下它的**是**
        # 来源数校验。否则体积校验也会拦，这条就变成"反正被拦了"的糊涂断言。
        self.assertLess(
            serialized_length(too_many),
            MAX_BUNDLE_CHARACTERS,
            "夹具同时越过了体积上限，就没法确认是来源数校验拦下的",
        )

        for name, provider in providers_with(lambda *args: {}):
            with self.subTest(provider=name):
                self.assert_rejected_with_domain_error(
                    provider, too_many, "%s 未校验来源数上限" % name
                )

    def test_exactly_at_the_limit_is_accepted(self):
        """恰好 12,000 字符必须放行，且这个"恰好"是真的对准了。"""
        boundary = bundle_of_exact_length(MAX_BUNDLE_CHARACTERS)
        self.assertEqual(serialized_length(boundary), MAX_BUNDLE_CHARACTERS)

        for name, provider in providers_with(lambda *args: {}):
            with self.subTest(provider=name):
                provider._request_body(boundary)

    def test_one_character_over_the_limit_is_rejected(self):
        """12,001 必须拒绝 —— 与上一条成对，才钉得住边界不是"差不多"。"""
        over = bundle_of_exact_length(MAX_BUNDLE_CHARACTERS + 1)
        self.assertEqual(serialized_length(over), MAX_BUNDLE_CHARACTERS + 1)

        for name, provider in providers_with(lambda *args: {}):
            with self.subTest(provider=name):
                self.assert_rejected_with_domain_error(
                    provider, over, "%s 放过了超限 1 字符的包" % name
                )

    def test_valid_bundle_actually_reaches_the_transport(self):
        """反向：合法体积的包必须真的发出去 —— 防止"校验"退化成"一律拒绝"。"""
        sent = []

        def transport(url, headers, body, timeout):
            sent.append(body)
            return {"choices": [{"message": {"content": json.dumps(payload(), ensure_ascii=False)}}]}

        provider = DeepSeekChatProvider(
            model="test-model", token_loader=lambda: "unit-test-token", transport=transport
        )

        result = provider.analyze(bundle_with(items_count=2, summary_chars=100))

        self.assertEqual(len(sent), 1)
        self.assertEqual(result.fact_summary, "公开政策调整")


    def test_rejection_happens_before_anything_leaves_the_machine(self):
        """超限包必须在**发送之前**被拦下：transport 调用次数必须是 0。

        这是体积/隐私类契约的硬标准——本组唯一能证明"确实没有东西外发"的断言。

        两个刻意的选择：
        - 走完整的 `analyze()`，不只调私有 `_request_body`。只测私有方法时，若有人把
          校验挪进 `analyze`（完全合法的实现）测试会红；而若有人把校验挪到**发送之后**，
          测试反而绿——两个方向都错。
        - 数的是 transport 的真实调用次数，而不是"抛没抛异常"：先发出去再校验的实现
          照样抛异常，但内容已经出门了。
        """
        for provider_name in PROVIDER_NAMES:
            for label, bundle in (
                ("字符超限", self.oversized()),
                ("来源超限", self.too_many_sources()),
            ):
                with self.subTest(provider=provider_name, case=label):
                    calls = []

                    def transport(url, headers, body, timeout, calls=calls):
                        calls.append(body)
                        return reply_for(provider_name)

                    provider = make_provider(provider_name, transport)
                    with self.assertRaises(InvalidJudgmentError):
                        provider.analyze(bundle)
                    self.assertEqual(
                        calls,
                        [],
                        "%s 处理「%s」时仍把请求发了出去——内容已经离开这台机器了"
                        % (provider_name, label),
                    )


class RunDueResilienceTests(unittest.TestCase):
    """越限的 job 必须**降级**，而不是把整轮认知带走。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.clock = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        # 真实发出的请求体。体积契约的硬标准是"超限内容一次都没出门"，
        # 所以这里必须留下每次调用的痕迹，而不是用永远成功的哑 transport。
        self.sent = []
        with self.database.connect() as connection:
            for cluster_id in ("C-big", "C-small"):
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

    def tearDown(self):
        self.temporary.cleanup()

    def transport(self, url, headers, body, timeout):
        """记录真实外发的请求体；返回合法输出，让合法请求能走完。"""
        self.sent.append(body)
        return reply_for("deepseek_chat")

    def queue(self):
        oversized = bundle_with(items_count=MAX_EVIDENCE_SOURCES, summary_chars=3000)
        normal = bundle_with(items_count=1, summary_chars=40)

        def loader(cluster_id):
            return oversized if cluster_id == "C-big" else normal

        return JudgmentQueue(
            self.database,
            providers={
                "deepseek": DeepSeekChatProvider(
                    model="test-model",
                    token_loader=lambda: "unit-test-token",
                    transport=self.transport,
                )
            },
            bundle_loader=loader,
            local_provider=LocalHeuristicProvider(),
            now=lambda: self.clock,
        )

    def jobs(self):
        with self.database.connect() as connection:
            return {
                row["cluster_id"]: dict(row)
                for row in connection.execute("SELECT * FROM judgment_jobs")
            }

    def test_oversized_job_degrades_and_the_round_keeps_going(self):
        """越限 job 降级 local；排在它后面的 job 必须照样被处理完。

        修复前：`provider.analyze()` 抛裸 ValueError → 冲出 `run_due` →
        C-small 永远停在 queued、C-big 也停在 queued，整轮认知白跑。
        """
        queue = self.queue()
        queue.enqueue("C-big", "hash-big", "deepseek")
        queue.enqueue("C-small", "hash-small", "deepseek")

        summary = queue.run_due(limit=10, remote_limit=10)

        jobs = self.jobs()
        self.assertEqual(
            jobs["C-big"]["status"],
            "invalid_output",
            "越限的 job 没有降级，状态停在 %s" % jobs["C-big"]["status"],
        )
        self.assertEqual(jobs["C-small"]["status"], "succeeded")
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["succeeded"], 1)
        for cluster_id, row in jobs.items():
            with self.subTest(cluster_id=cluster_id):
                self.assertIsNotNone(row["finished_at"], "job 被留在了半空状态")

        # 硬标准：只有合法那一个请求真的出门，超限那个一次都没发出去。
        # 只断 job 状态的话，"先发出超限请求、再降级"的实现照样能过。
        self.assertEqual(
            len(self.sent), 1, "除合法作业外还有请求发了出去（超限内容可能已外发）"
        )
        self.assertLess(
            len(json.dumps(self.sent[0], ensure_ascii=False)),
            MAX_BUNDLE_CHARACTERS,
            "唯一发出去的那个请求体本身就是超限的",
        )

    def test_degraded_job_still_produces_a_local_judgment(self):
        """降级不是"丢掉"：用户仍要拿到一份本地兜底研判。"""
        queue = self.queue()
        queue.enqueue("C-big", "hash-big", "deepseek")

        queue.run_due(limit=10, remote_limit=10)

        with self.database.connect() as connection:
            providers = [
                row["provider"]
                for row in connection.execute(
                    "SELECT provider FROM judgments WHERE cluster_id='C-big'"
                )
            ]
            needs_judgment = connection.execute(
                "SELECT needs_judgment FROM event_clusters WHERE cluster_id='C-big'"
            ).fetchone()[0]
        self.assertEqual(providers, ["local"], "越限后没有落一条本地兜底研判")
        self.assertEqual(needs_judgment, 0, "簇仍被标成待研判，会反复入队")
        self.assertEqual(self.sent, [], "只有超限作业，却仍然有请求发出去了")

    def test_the_oversized_job_does_not_loop_forever(self):
        """降级后不许再排队重试 —— 否则每个周期都会重抛一次。"""
        queue = self.queue()
        job_id = queue.enqueue("C-big", "hash-big", "deepseek")
        queue.run_due(limit=10, remote_limit=10)

        self.clock += timedelta(hours=1)
        second = queue.run_due(limit=10, remote_limit=10)

        self.assertEqual(second["failed"], 0, "越限 job 又被捡起来重跑了一遍")
        self.assertEqual(second["succeeded"], 0)
        with self.database.connect() as connection:
            status = connection.execute(
                "SELECT status FROM judgment_jobs WHERE job_id=?", (job_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "invalid_output")
        self.assertEqual(self.sent, [], "超限作业在重跑时把请求发了出去")


if __name__ == "__main__":
    unittest.main()
