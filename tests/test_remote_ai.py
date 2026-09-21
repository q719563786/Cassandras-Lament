import contextlib
import json
import socket
import sqlite3
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from yuanjian_app import remote_ai
from yuanjian_app.database import Database
from yuanjian_app.judgments import (
    InvalidJudgmentError,
    LocalHeuristicProvider,
    build_public_bundle,
)
from yuanjian_app.remote_ai import (
    REMOTE_MIN_INTERVAL_SECONDS,
    REMOTE_RATE_LIMIT_BACKOFF_MINUTES,
    JudgmentQueue,
    MinIntervalPacer,
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

    def queue(self, providers, daily_budget=30):
        return JudgmentQueue(
            self.database,
            providers=providers,
            bundle_loader=lambda cluster_id: bundle(),
            local_provider=LocalHeuristicProvider(),
            now=self.clock,
            daily_budget=daily_budget,
        )

    def test_enqueue_deduplicates_same_cluster_evidence_and_provider(self):
        queue = self.queue({"remote": FakeProvider()})

        first = queue.enqueue("C-1", "hash-1", "remote")
        second = queue.enqueue("C-1", "hash-1", "remote")

        self.assertEqual(first, second)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM judgment_jobs").fetchone()[0], 1)

    def test_auth_pauses_rate_limit_backs_off_and_invalid_output_falls_back(self):
        """三条互不相同的失败路径，三种互不相同的处置。

        `rate_limit`（429）走**独立且更短**的退避序列 `REMOTE_RATE_LIMIT_BACKOFF_MINUTES`
        = 1/2/4 分钟，不是普通失败那条 15/30/60/120。依据是 429 的语义是"你发太快
        了"：把它推到 +15 分钟只会和积压的作业一起到期，下一轮重新形成突发（真库
        15 小时里 152 成功 / 23 个 429 正是这么来的）。反向保护见
        `test_plain_failures_keep_the_long_backoff_sequence`。
        """
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
        # 第 1 次 429：clock 08:00 + 1 分钟
        self.assertEqual(first_next, "2026-08-11T08:01:00Z")

        self.clock.value += timedelta(minutes=15)
        queue.run_due(limit=10)
        with self.database.connect() as connection:
            second_next = connection.execute(
                "SELECT next_attempt_at FROM judgment_jobs WHERE job_id=?", (rate_id,)
            ).fetchone()[0]
        # 第 2 次 429：clock 08:15 + 2 分钟
        self.assertEqual(second_next, "2026-08-11T08:17:00Z")

        self.clock.value += timedelta(minutes=30)
        queue.run_due(limit=10)
        with self.database.connect() as connection:
            third_next = connection.execute(
                "SELECT next_attempt_at FROM judgment_jobs WHERE job_id=?", (rate_id,)
            ).fetchone()[0]
        # 第 3 次 429：clock 08:45 + 4 分钟
        self.assertEqual(third_next, "2026-08-11T08:49:00Z")

    def test_plain_failures_keep_the_long_backoff_sequence(self):
        """反向对照：`network` 类失败**仍然**走 15/30/60/120。

        没有这条，"429 与普通失败分开"就只剩一半保护 —— 将来有人图省事把两条分支
        合并回一条指数退避，上面那条只会跟着一起变，不会被发现。普通失败是在等
        对端恢复，长退避是对的，不该被 429 的短序列顺手带走。
        """
        queue = self.queue({"remote": FakeProvider(lambda: RemoteProviderError("network"))})
        job_id = queue.enqueue("C-net", "h-net", "remote")

        queue.run_due(limit=10)

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status,attempts,next_attempt_at FROM judgment_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        self.assertEqual(row["status"], "retry")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["next_attempt_at"], "2026-08-11T08:15:00Z")

        self.clock.value += timedelta(minutes=15)
        queue.run_due(limit=10)
        with self.database.connect() as connection:
            second = connection.execute(
                "SELECT next_attempt_at FROM judgment_jobs WHERE job_id=?", (job_id,)
            ).fetchone()[0]
        self.assertEqual(second, "2026-08-11T08:45:00Z")

    def test_rate_limit_gives_up_after_the_short_sequence_and_degrades_local(self):
        """1+2+4 走完就是第 4 次失败 —— 必须降级本地，不能变成忙等。

        这条是短退避序列**长度**的保护：序列只有 3 个值，配 `MAX_REMOTE_RETRIES=4`，
        整条链路约 7 分钟收敛（旧序列要走满 105 分钟）。如果谁把序列拉长或把上限
        抬高，整批作业会长期卡在 retry 里而不是给用户一份本地研判。
        """
        queue = self.queue({"remote": FakeProvider(lambda: RemoteProviderError("rate_limit"))})
        job_id = queue.enqueue("C-rate", "h-rate", "remote")

        offsets = []
        for _ in range(len(REMOTE_RATE_LIMIT_BACKOFF_MINUTES)):
            queue.run_due(limit=10)
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT status,next_attempt_at FROM judgment_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
            self.assertEqual(row["status"], "retry")
            offsets.append(row["next_attempt_at"])
            self.clock.value = datetime.fromisoformat(
                row["next_attempt_at"].replace("Z", "+00:00")
            )

        self.assertEqual(
            offsets,
            ["2026-08-11T08:01:00Z", "2026-08-11T08:03:00Z", "2026-08-11T08:07:00Z"],
        )

        queue.run_due(limit=10)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status,attempts,last_error FROM judgment_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            providers = [
                item["provider"]
                for item in connection.execute(
                    "SELECT provider FROM judgments WHERE cluster_id='C-rate'"
                )
            ]
        self.assertEqual(row["status"], "remote_error_fallback_local")
        self.assertEqual(row["attempts"], 4)
        self.assertEqual(row["last_error"], "rate_limit")
        self.assertEqual(providers, ["local"], "退避走完没有落到本地兜底")

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


class FakeTime:
    """替身时钟。`MinIntervalPacer` 取的是模块级 `time`，所以整体替换它即可不真睡。"""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ShutdownQueueCleanupTests(JudgmentQueueTests):
    """#24：shutdown 清队列失败不许静默，且必须防住"重启后误发请求"。

    缺陷现场：`except: pass` 把 DELETE 失败吃掉，排队中的远程作业原样留在库里。
    用户以为"退出即取消"，下次启动 `run_due` 照常把它们发出去 —— 意外花钱。
    """

    def _job_statuses(self):
        with self.database.connect() as connection:
            return {
                row["job_id"]: row["status"]
                for row in connection.execute("SELECT job_id,status FROM judgment_jobs")
            }

    def test_delete_failure_is_logged_with_a_stack_and_freezes_the_queue(self):
        provider = FakeProvider()
        queue = self.queue({"remote": provider})
        queue.enqueue("C-1", "h-1", "remote")

        real_connect = self.database.connect
        state = {"first": True}

        @contextlib.contextmanager
        def flaky_connect():
            if state["first"]:
                state["first"] = False
                raise sqlite3.OperationalError("database is locked")
            with real_connect() as connection:
                yield connection

        self.database.connect = flaky_connect
        try:
            with self.assertLogs("yuanjian_app.remote_ai", level="WARNING") as captured:
                queue.shutdown()
        finally:
            self.database.connect = real_connect

        # ① 失败留痕：有日志、带堆栈（原先是一条 `pass`，什么都没留下）
        joined = "\n".join(captured.output)
        self.assertIn("清空待处理的远程研判作业失败", joined)
        self.assertIn("Traceback (most recent call last)", joined)

        # ② 作业没丢，但被冻结成非到期态（不在 run_due 的选取集合里）
        statuses = self._job_statuses()
        self.assertEqual(len(statuses), 1)
        self.assertEqual(
            list(statuses.values()),
            [JudgmentQueue.PAUSED_SHUTDOWN_STATUS],
            "清队列失败后作业仍是 queued —— 下次启动会被自动执行",
        )

        # ③ 模拟"重启"：新队列（`_shutdown` 未置位）跑一轮，也**不得**真发请求
        restarted = self.queue({"remote": provider})
        summary = restarted.run_due(limit=10)

        self.assertEqual(provider.calls, 0, "重启后被冻结的远程作业仍然被自动执行了")
        self.assertEqual(summary["succeeded"], 0)
        self.assertEqual(
            self._job_statuses(),
            {job_id: JudgmentQueue.PAUSED_SHUTDOWN_STATUS for job_id in statuses},
        )

    def test_happy_path_deletes_the_queue_and_does_not_freeze_anything(self):
        """反面对照：DELETE 成功时走原路径 —— 行被删掉，不留下冻结态。"""
        queue = self.queue({"remote": FakeProvider()})
        queue.enqueue("C-1", "h-1", "remote")

        queue.shutdown()

        self.assertEqual(self._job_statuses(), {})

    def test_run_due_refuses_to_send_while_the_shutdown_flag_is_set(self):
        """进程内最后一道闸：`_shutdown` 已置位时 `run_due` 必须一条都不发。

        与冻结兜底**互为独立**：冻结管"跨重启"（状态层面，见上一条），这个标志管
        "本进程退出时"——哪怕队列里还有 `queued` 行（比如退出途中又被塞进来一条），
        也不许真的发出去，否则就是意外账单。
        """
        provider = FakeProvider()
        queue = self.queue({"remote": provider})
        queue.shutdown()  # 置位 _shutdown（此刻库里还没有作业）
        queue.enqueue("C-1", "h-1", "remote")  # 退出途中又被塞进来一条

        summary = queue.run_due(limit=10)

        self.assertTrue(summary.get("shutdown"), "置位后 run_due 仍照常处理")
        self.assertEqual(provider.calls, 0, "_shutdown 已置位，run_due 仍然发了远程请求")
        self.assertEqual(
            list(self._job_statuses().values()),
            ["queued"],
            "作业不该被处理、也不该被删掉",
        )


class RemotePacerTests(unittest.TestCase):
    """瞬时速率闸（`REMOTE_MIN_INTERVAL_SECONDS` / `MinIntervalPacer`）。

    它与 `RateLimiter` 管的不是同一件事：`RateLimiter` 允许 60 秒窗口里前 20 次
    一拥而上，`MinIntervalPacer` 从第一次起就把相邻两次摊开。真库里的 429 不是
    "一小时发多了"，而是"几秒内连着发"。
    """

    def test_pacer_spaces_consecutive_calls_by_the_interval(self):
        fake = FakeTime()
        with mock.patch.object(remote_ai, "time", fake):
            pacer = MinIntervalPacer(REMOTE_MIN_INTERVAL_SECONDS)

            pacer.wait()
            self.assertEqual(fake.sleeps, [], "首次调用不该白等")

            pacer.wait()
            self.assertEqual(
                fake.sleeps, [REMOTE_MIN_INTERVAL_SECONDS], "紧接着的第二次调用没有补满间隔"
            )

            fake.now += REMOTE_MIN_INTERVAL_SECONDS * 3
            pacer.wait()
            self.assertEqual(
                fake.sleeps,
                [REMOTE_MIN_INTERVAL_SECONDS],
                "单次调用本来就慢、间隔已自然满足时又白等了一次",
            )

    def test_disabled_interval_is_a_no_op(self):
        """`interval<=0` 必须彻底关掉 —— 否则将来调成 0 会变成忙等。"""
        fake = FakeTime()
        with mock.patch.object(remote_ai, "time", fake):
            pacer = MinIntervalPacer(0)
            pacer.wait()
            pacer.wait()
        self.assertEqual(fake.sleeps, [])

    def test_the_interval_implies_a_rate_below_the_agnes_rpm_limit(self):
        """闸门必须留余量：贴着 20 RPM 走，一有抖动就又是 429，代价远高于多等几秒。"""
        self.assertGreater(REMOTE_MIN_INTERVAL_SECONDS, 0)
        self.assertLessEqual(
            60.0 / REMOTE_MIN_INTERVAL_SECONDS,
            remote_ai.AGNES_AI_RPM_LIMIT,
            "瞬时速率闸放行的每分钟请求数超过了 Agnes 的 20 RPM",
        )

    def test_the_real_http_exit_goes_through_the_pacer(self):
        """闸门挂在**传输层**，所以任何真的 HTTP 请求都绕不过去。

        这是"闸门位置"的保护：如果谁把它挪回 `run_due` 的循环里，这条就会红 ——
        那样一来测试为了跑得快会把节流关掉，等于把这道闸的验证一起关掉。
        """
        waits = []
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        with mock.patch.object(
            remote_ai._remote_pacer, "wait", lambda: waits.append(True)
        ), mock.patch.object(
            remote_ai.socket, "getaddrinfo", lambda *args, **kwargs: public
        ), mock.patch.object(
            remote_ai.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("boom"),
        ):
            with self.assertRaises(RemoteProviderError) as caught:
                remote_ai._default_transport("https://news.example/v1", {}, {}, 5)

        self.assertEqual(caught.exception.kind, "network")
        self.assertEqual(len(waits), 1, "真实 HTTP 出口没有经过瞬时速率闸")

    def test_a_request_that_fails_the_address_gate_does_not_wait_first(self):
        """地址闸在速率闸**之前**：端点是内网就先拒，不要先白等满一个间隔。"""
        waits = []
        loopback = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        with mock.patch.object(
            remote_ai._remote_pacer, "wait", lambda: waits.append(True)
        ), mock.patch.object(
            remote_ai.socket, "getaddrinfo", lambda *args, **kwargs: loopback
        ):
            with self.assertRaises(RemoteProviderError) as caught:
                remote_ai._default_transport("https://news.example/v1", {}, {}, 5)

        self.assertEqual(caught.exception.kind, "unsafe_endpoint")
        self.assertEqual(waits, [], "端点还没校验就先等满间隔，时间被白等掉了")


if __name__ == "__main__":
    unittest.main()
