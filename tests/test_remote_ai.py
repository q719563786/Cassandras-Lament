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


class RemotePaidGovernanceTests(unittest.TestCase):
    """第二批 · 远程付费三条（B1 账目 / B2 默认上限 / B3 熔断）。

    三条守的是同一件事：**付费端点的钱要花在明处**。B1 保证账目对得上账单，
    B2 保证默认值按最坏情况（付费）来定，B3 保证对端整体挂掉时不再无谓烧钱。
    """

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

    def _rows(self, sql, args=()):
        with self.database.connect() as connection:
            return connection.execute(sql, args).fetchall()

    def _count(self, sql, args=()):
        return self._rows(sql, args)[0][0]

    # ── B1 账目 ──────────────────────────────────────────────────────────
    def test_failed_retries_are_counted_against_the_daily_budget(self):
        """B1：重试是一次真实付费调用，必须计入当日预算。

        旧判据只数 `finished_at IS NOT NULL` 的行，而**重试分支不写 `finished_at`**
        ⇒ 失败重试整个漏掉，越失败漏得越多（账上写着 2000，实际能发出
        2000×重试倍数次）。验收口径：打桩持续抛错，当天累计调用**不超 budget**。
        """
        provider = FakeProvider(lambda: RemoteProviderError("network"))
        queue = self.queue({"remote": provider}, daily_budget=3)
        for index in range(6):
            queue.enqueue(f"C-{index}", f"h-{index}", "remote")

        # 连跑 6 轮、每轮把时钟推过退避点，给重试充分的发生机会
        for _ in range(6):
            queue.run_due(limit=20, remote_limit=20)
            self.clock.value += timedelta(minutes=20)

        self.assertEqual(provider.calls, 3, "日预算没能拦住持续失败的重试")
        snapshot = queue.remote_budget_snapshot()
        self.assertEqual(
            snapshot["used_today"],
            provider.calls,
            "账目与实际发起的调用次数不一致 —— 重试又被漏记了",
        )

    def test_usage_counter_survives_a_restart_and_a_new_day_resets_it(self):
        """B1 的落点：按**次**记账、跨重启不丢、换日归零。

        不跨重启就不叫"日预算"——重启一次账本清零，等于给了绕过上限的直通车道。
        """
        queue = self.queue({"remote": FakeProvider()})
        with self.database.connect() as connection:
            queue._bump_remote_usage(connection, self.clock.value)
            queue._bump_remote_usage(connection, self.clock.value)

        restarted = self.queue({"remote": FakeProvider()})
        self.assertEqual(restarted.remote_used_today(), 2)

        # 同一个替身时钟（`now=self.clock`）推到第二天
        self.clock.value = datetime(2026, 8, 12, 8, tzinfo=timezone.utc)
        self.assertEqual(restarted.remote_used_today(), 0, "换日后账本没有归零")

    # ── B2 默认上限 ──────────────────────────────────────────────────────
    def test_default_daily_budget_is_two_hundred_not_two_thousand(self):
        """B2：非本地 provider 的默认日上限 = 200。

        2000 是照着**免费** Agnes 定的（那端点限的是 20 RPM、没有日配额）；一旦
        端点换成付费 provider，同一个数字就从"白给"变成"一天几千次真实扣费"。
        默认值必须按最坏情况定，免费额度宽松的用户自己在设置页调高。
        """
        self.assertEqual(remote_ai.DAILY_REMOTE_BUDGET, 200)
        self.assertTrue(
            remote_ai.MIN_DAILY_BUDGET <= remote_ai.DAILY_REMOTE_BUDGET <= remote_ai.MAX_DAILY_BUDGET
        )
        with self.database.connect() as connection:
            connection.execute("DELETE FROM runtime_state WHERE state_key='ai_settings'")
        # 从未存过设置的库 ⇒ 读回来就是新默认
        self.assertEqual(remote_ai.read_ai_setting(self.database)["daily_budget"], 200)

    def test_stored_budget_is_never_overwritten_by_the_new_default(self):
        """B2 的红线：**只改默认，不覆盖用户已显式存过的值**。

        真库 `ai_settings` 里显式存了 2000（见
        `build-artifacts/scratch-yj-20260921/probe_remote_budget.txt`），
        把默认改成 200 之后那台机器读回来**必须还是 2000** —— 收紧"已存过"的
        值需要一次写库迁移，那是产品决策，本轮不做。
        """
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_state(state_key,value_json,updated_at)
                VALUES ('ai_settings',?,'2026-08-11T08:00:00Z')
                """,
                (json.dumps({"enabled": True, "daily_budget": 2000}),),
            )

        self.assertEqual(remote_ai.read_ai_setting(self.database)["daily_budget"], 2000)
        # daily_budget=None ⇒ 现读设置（生产路径）
        live = JudgmentQueue(
            self.database,
            providers={"remote": FakeProvider()},
            bundle_loader=lambda cluster_id: bundle(),
            local_provider=LocalHeuristicProvider(),
            now=self.clock,
        )
        self.assertEqual(live._current_daily_budget(), 2000, "默认值覆盖了用户已存的上限")
        self.assertEqual(live.remote_budget_snapshot()["budget"], 2000)

    def test_budget_and_used_today_are_exposed_for_diagnostics(self):
        """B2 的出口：`budget` / `used_today` 由后端统一给出，前端不要自己另算一份。"""
        queue = self.queue({"remote": FakeProvider()}, daily_budget=7)
        queue.enqueue("C-1", "h-1", "remote")

        queue.run_due(limit=10)

        snapshot = queue.remote_budget_snapshot()
        self.assertEqual(snapshot["budget"], 7)
        self.assertEqual(snapshot["used_today"], 1)
        self.assertEqual(snapshot["day"], "2026-08-11")
        self.assertEqual(queue.remote_used_today(), 1)

    # ── B3 熔断 ──────────────────────────────────────────────────────────
    def test_consecutive_failures_open_the_circuit_and_stop_the_queue(self):
        """B3：同一 provider 连续失败达阈值 ⇒ 排队作业**统一冻结**且**停工**。

        非鉴权失败原本只有逐作业退避（15/30/60/120 分钟）没有总闸；对端整体
        不可用时，预算会一路烧在"明知会失败"的调用上。冻结复用既有
        `paused_auth`（`last_error=circuit_open` 与 `auth_paused` 区分）。
        """
        provider = FakeProvider(lambda: RemoteProviderError("network"))
        queue = self.queue({"remote": provider}, daily_budget=1000)
        for index in range(8):
            queue.enqueue(f"C-{index}", f"h-{index}", "remote")

        queue.run_due(limit=20, remote_limit=20)

        self.assertEqual(
            provider.calls,
            remote_ai.REMOTE_CIRCUIT_THRESHOLD,
            "达阈值后没有停工，剩余作业仍在继续发请求",
        )
        rows = self._rows("SELECT status,last_error FROM judgment_jobs")
        self.assertEqual(len(rows), 8)
        self.assertEqual({row["status"] for row in rows}, {"paused_auth"})
        self.assertEqual(
            {row["last_error"] for row in rows}, {remote_ai.CIRCUIT_OPEN_REASON}
        )
        self.assertEqual(queue.remote_budget_snapshot()["circuit_open"], ["remote"])

        # 停工是**持续**的：后续几轮（paused_auth 不在到期集合里）一条都不发
        for _ in range(3):
            self.clock.value += timedelta(minutes=30)
            queue.run_due(limit=20, remote_limit=20)
        self.assertEqual(
            provider.calls,
            remote_ai.REMOTE_CIRCUIT_THRESHOLD,
            "熔断后又在发请求 —— 熔断没起作用",
        )

    def test_one_success_closes_the_circuit_and_resumes_the_frozen_jobs(self):
        """B3 的解除：**半开** —— 冷却期后放行一条探测作业，成功一次即整批解冻。

        这条同时锁住"熔断不能是永久封死"：打开时排队作业全被冻结成 `paused_auth`
        （不在到期集合里），若不放行探测，"成功一次即解除"就永远等不到那次成功。
        """
        provider = FakeProvider(lambda: RemoteProviderError("network"))
        queue = self.queue({"remote": provider}, daily_budget=1000)
        for index in range(8):
            queue.enqueue(f"C-{index}", f"h-{index}", "remote")
        queue.run_due(limit=20, remote_limit=20)
        self.assertEqual(provider.calls, remote_ai.REMOTE_CIRCUIT_THRESHOLD)

        # 对端已恢复，但**冷却期内**不许放行 —— 否则熔断等于没有停工
        provider.action = None
        queue.enqueue("C-new", "h-new", "remote")
        self.clock.value += timedelta(minutes=remote_ai.REMOTE_CIRCUIT_PROBE_MINUTES - 5)
        queue.run_due(limit=20, remote_limit=20)
        self.assertEqual(
            provider.calls,
            remote_ai.REMOTE_CIRCUIT_THRESHOLD,
            "熔断冷却期内就放行了作业 —— 停工形同虚设",
        )

        # 冷却期过 ⇒ 放行**一条**探测作业，成功 ⇒ 解冻整批
        self.clock.value += timedelta(minutes=10)
        queue.run_due(limit=20, remote_limit=20)
        self.assertEqual(provider.calls, remote_ai.REMOTE_CIRCUIT_THRESHOLD + 1)

        snapshot = queue.remote_budget_snapshot()
        self.assertEqual(snapshot["circuit_open"], [])
        self.assertEqual(snapshot["failure_streak"].get("remote"), 0)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM judgment_jobs WHERE status='queued'"), 8
        )

        self.clock.value += timedelta(minutes=5)
        queue.run_due(limit=20, remote_limit=20)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM judgment_jobs WHERE status='succeeded'"),
            9,
            "解冻后的作业没有跑完（8 条解冻 + 1 条新作业）",
        )

    def test_closing_the_circuit_leaves_auth_paused_jobs_frozen(self):
        """复用 `paused_auth` 的代价：两种冻结必须靠 `last_error` 区分清楚。

        认证失败冻结的那批（`auth_paused`）是用户没配好凭据，一次远程调用成功
        就顺手放行 ⇒ 白花钱重试。
        """
        queue = self.queue({"remote": FakeProvider()})
        with self.database.connect() as connection:
            for index, reason in enumerate(("auth_paused", remote_ai.CIRCUIT_OPEN_REASON)):
                connection.execute(
                    """
                    INSERT INTO judgment_jobs(
                        job_id,cluster_id,evidence_hash,provider,model,status,
                        attempts,request_chars,created_at,next_attempt_at,last_error
                    ) VALUES (?,?,?,?,'m','paused_auth',0,0,?,?,?)
                    """,
                    (
                        f"J-{index}",
                        f"C-{index}",
                        f"h-{index}",
                        "remote",
                        "2026-08-11T08:00:00Z",
                        "2026-08-11T08:00:00Z",
                        reason,
                    ),
                )
        queue._circuit_open.add("remote")

        with self.database.connect() as connection:
            resumed = queue._close_circuit(connection, "remote")

        self.assertEqual(resumed, 1, "解冻范围越界 —— 认证冻结的作业被一起放行了")
        self.assertEqual(
            {
                row["last_error"]
                for row in self._rows(
                    "SELECT last_error FROM judgment_jobs WHERE status='paused_auth'"
                )
            },
            {"auth_paused"},
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
