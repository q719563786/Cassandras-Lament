import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.judgments import (
    InvalidJudgmentError,
    LocalHeuristicProvider,
    build_public_bundle,
)
from yuanjian_app.remote_ai import (
    JudgmentQueue,
    OpenAIResponsesProvider,
    RemoteProviderError,
)


def valid_output():
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
        "gyw": {
            "stakeholders": "推动方：发文机关、上级政府；阻力方：执行部门、利益集团",
            "constraints": "资源约束：财政预算、编制、配套立法",
            "least_resistance_path": "最小阻力路径：试点 → 推广 → 全面执行",
            "counter_evidence": "反对证据：执行阻力、利益集团游说、政策转向",
            "leading_indicators": "领先指标：试点公告、配套细则、部门预算",
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


def bundle():
    return build_public_bundle(
        {
            "cluster_id": "C-1",
            "title": "医保政策调整",
            "summary": "公开事件",
            "evidence_level": "E2",
            "categories": ["policy"],
        },
        [
            {
                "source_id": "S-1",
                "title": "政策通知",
                "summary": "公开内容",
                "canonical_url": "https://news.example/policy",
                "published_at": "2026-08-11T00:00:00Z",
            }
        ],
    )


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 11, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class RemoteProviderTests(unittest.TestCase):
    def test_request_uses_responses_structured_output_contract(self):
        captured = {}
        token = "super-secret-token"

        def transport(url, headers, body, timeout):
            captured.update(url=url, headers=headers, body=body, timeout=timeout)
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(valid_output())}
                        ],
                    }
                ]
            }

        provider = OpenAIResponsesProvider(
            model="explicit-model-id", token_loader=lambda: token, transport=transport
        )

        result = provider.analyze(bundle())

        request = captured["body"]
        self.assertEqual(captured["url"], "https://api.openai.com/v1/responses")
        self.assertEqual(captured["headers"]["Authorization"], f"Bearer {token}")
        self.assertNotIn(token, json.dumps(request, ensure_ascii=False))
        self.assertEqual(request["model"], "explicit-model-id")
        self.assertTrue(request["input"])
        output_format = request["text"]["format"]
        self.assertEqual(output_format["type"], "json_schema")
        self.assertTrue(output_format["strict"])
        self.assertFalse(output_format["schema"]["additionalProperties"])
        self.assertEqual(set(output_format["schema"]["required"]), set(valid_output()))
        self.assertEqual(result.fact_summary, "公开政策调整")

    def test_model_token_endpoint_and_output_are_validated(self):
        with self.assertRaises(ValueError):
            OpenAIResponsesProvider(model="", token_loader=lambda: "token")
        with self.assertRaises(ValueError):
            OpenAIResponsesProvider(
                endpoint="http://127.0.0.1:9999/v1/responses",
                model="model",
                token_loader=lambda: "token",
            )
        provider = OpenAIResponsesProvider(
            model="model", token_loader=lambda: "", transport=lambda *args: {}
        )
        with self.assertRaisesRegex(RemoteProviderError, "auth"):
            provider.analyze(bundle())
        invalid = OpenAIResponsesProvider(
            model="model",
            token_loader=lambda: "token",
            transport=lambda *args: {"output_text": "not-json"},
        )
        with self.assertRaises(InvalidJudgmentError):
            invalid.analyze(bundle())

    def test_http_statuses_are_classified_without_leaking_token(self):
        for status, kind in ((401, "auth"), (403, "auth"), (429, "rate_limit")):
            with self.subTest(status=status):
                provider = OpenAIResponsesProvider(
                    model="model",
                    token_loader=lambda: "secret",
                    transport=lambda *args, code=status: (_ for _ in ()).throw(
                        RemoteProviderError.from_http(code)
                    ),
                )
                with self.assertRaisesRegex(RemoteProviderError, kind) as context:
                    provider.analyze(bundle())
                self.assertNotIn("secret", str(context.exception))


class FakeProvider:
    def __init__(self, action=None, model="fake-model"):
        self.action = action
        self.model = model
        self.calls = 0

    def analyze(self, evidence):
        self.calls += 1
        if self.action:
            raise self.action()
        return LocalHeuristicProvider().analyze(evidence)


class JudgmentQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.clock = MutableClock()

    def tearDown(self):
        self.temporary.cleanup()

    def queue(self, providers):
        return JudgmentQueue(
            self.database,
            providers=providers,
            bundle_loader=lambda cluster_id: bundle(),
            local_provider=LocalHeuristicProvider(),
            now=self.clock,
        )

    def test_enqueue_deduplicates_same_cluster_evidence_and_provider(self):
        queue = self.queue({"remote": FakeProvider()})

        first = queue.enqueue("C-1", "hash-1", "remote")
        second = queue.enqueue("C-1", "hash-1", "remote")

        self.assertEqual(first, second)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM judgment_jobs").fetchone()[0], 1)

    def test_auth_pauses_rate_limit_backs_off_and_invalid_output_falls_back(self):
        providers = {
            "auth": FakeProvider(lambda: RemoteProviderError("auth")),
            "rate": FakeProvider(lambda: RemoteProviderError("rate_limit")),
            "invalid": FakeProvider(lambda: InvalidJudgmentError("bad output")),
        }
        queue = self.queue(providers)
        queue.enqueue("C-auth", "h-auth", "auth")
        rate_id = queue.enqueue("C-rate", "h-rate", "rate")
        queue.enqueue("C-invalid", "h-invalid", "invalid")

        queue.run_due(limit=10)

        with self.database.connect() as connection:
            states = {
                row["provider"]: row["status"]
                for row in connection.execute("SELECT provider,status FROM judgment_jobs")
            }
            local_count = connection.execute(
                "SELECT COUNT(*) FROM judgments WHERE provider='local'"
            ).fetchone()[0]
            first_next = connection.execute(
                "SELECT next_attempt_at FROM judgment_jobs WHERE job_id=?", (rate_id,)
            ).fetchone()[0]
        self.assertEqual(states, {"auth": "paused_auth", "rate": "retry", "invalid": "invalid_output"})
        self.assertEqual(local_count, 1)
        self.assertEqual(first_next, "2026-08-11T08:15:00Z")

        self.clock.value += timedelta(minutes=15)
        queue.run_due(limit=10)
        with self.database.connect() as connection:
            second_next = connection.execute(
                "SELECT next_attempt_at FROM judgment_jobs WHERE job_id=?", (rate_id,)
            ).fetchone()[0]
        self.assertEqual(second_next, "2026-08-11T08:45:00Z")

        self.clock.value += timedelta(minutes=30)
        queue.run_due(limit=10)
        with self.database.connect() as connection:
            third_next = connection.execute(
                "SELECT next_attempt_at FROM judgment_jobs WHERE job_id=?", (rate_id,)
            ).fetchone()[0]
        self.assertEqual(third_next, "2026-08-11T09:45:00Z")

    def test_daily_budget_defers_thirty_first_hash_until_next_utc_day(self):
        provider = FakeProvider()
        queue = self.queue({"remote": provider})
        for index in range(31):
            queue.enqueue(f"C-{index}", f"hash-{index}", "remote")

        queue.run_due(limit=40, remote_limit=40)

        self.assertEqual(provider.calls, 30)
        with self.database.connect() as connection:
            deferred = connection.execute(
                "SELECT COUNT(*) FROM judgment_jobs WHERE status='queued_budget'"
            ).fetchone()[0]
        self.assertEqual(deferred, 1)

        self.clock.value = datetime(2026, 8, 12, 0, 1, tzinfo=timezone.utc)
        queue.run_due(limit=40, remote_limit=40)
        self.assertEqual(provider.calls, 31)


if __name__ == "__main__":
    unittest.main()
