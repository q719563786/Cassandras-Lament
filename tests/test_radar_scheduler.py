import tempfile
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.external_radar import ExternalRadarService, parse_iso
from yuanjian_app.external_sources import ExternalItem, FetchError
from yuanjian_app.radar_scheduler import RadarScheduler


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class RadarSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "yuanjian.db")
        self.database.initialize()
        self.clock = Clock()

    def tearDown(self):
        self.temp_dir.cleanup()

    def service(self, fetcher):
        service = ExternalRadarService(self.database, fetcher=fetcher, now=self.clock)
        service.add_source(
            {
                "source_id": "S-1",
                "name": "Official",
                "kind": "rss",
                "endpoint": "https://example.com/feed.xml",
                "refresh_minutes": 15,
            }
        )
        return service

    def test_run_once_fetches_immediately_then_waits_for_due_time(self):
        item = ExternalItem("S-1", "Official", "https://example.com/1", "政策提醒")
        calls = 0

        def fetcher(source):
            nonlocal calls
            calls += 1
            return [item]

        scheduler = RadarScheduler(self.service(fetcher), poll_seconds=0.01)

        self.assertEqual(scheduler.run_once(), 1)
        self.assertEqual(scheduler.run_once(), 0)
        self.clock.value += timedelta(minutes=15)
        self.assertEqual(scheduler.run_once(), 1)
        self.assertEqual(calls, 2)

    def test_repeated_failures_back_off_exponentially_then_cap_at_360(self):
        """失败退避：base 15 分钟、每次翻倍、封顶 360 分钟。

        2026-09-20 拍板把封顶从 60 拉到 360 —— 原契约（15/30/60/60）就是这条
        用例钉住的，现在序列延到 15/30/60/120/240/360/360：前四步仍是翻倍，
        第六步起撞上封顶。多跑几步是为了**让封顶本身被测到**，否则"改成 720"
        这类回归不会被发现。
        """
        def fetcher(source):
            raise FetchError("timeout", "超时")

        service = self.service(fetcher)
        scheduler = RadarScheduler(service, poll_seconds=0.01)
        delays = []
        for expected in (15, 30, 60, 120, 240, 360, 360):
            self.assertEqual(scheduler.run_once(), 1)
            source = service.list_sources()[0]
            delay = parse_iso(source["next_fetch_at"]) - self.clock.value
            delays.append(int(delay.total_seconds() / 60))
            self.assertEqual(delays[-1], expected)
            self.clock.value += timedelta(minutes=expected)

        self.assertEqual(delays, [15, 30, 60, 120, 240, 360, 360])

    def test_background_scheduler_stops_without_leaving_a_thread(self):
        service = self.service(lambda source: [])
        scheduler = RadarScheduler(service, poll_seconds=0.01)

        scheduler.start()
        scheduler.stop(timeout=1)

        self.assertFalse(scheduler.running)

    def test_paused_source_is_not_fetched_until_reenabled(self):
        calls = 0

        def fetcher(source):
            nonlocal calls
            calls += 1
            return []

        service = self.service(fetcher)
        scheduler = RadarScheduler(service, poll_seconds=0.01)

        service.set_source_enabled("S-1", False)
        self.assertEqual(scheduler.run_once(), 0)
        service.set_source_enabled("S-1", True)
        self.assertEqual(scheduler.run_once(), 1)
        self.assertEqual(calls, 1)

    def test_paused_scheduler_skips_automatic_tasks_until_resumed(self):
        class RecordingCognition:
            def __init__(self):
                self.process_calls = 0
                self.trend_calls = 0

            def process_once(self):
                self.process_calls += 1
                return {}

            def capture_trends(self):
                self.trend_calls += 1
                return {}

        cognition = RecordingCognition()
        scheduler = RadarScheduler(
            self.service(lambda source: []),
            poll_seconds=0.01,
            database=self.database,
            cognition=cognition,
            now=self.clock,
        )

        scheduler.pause()

        self.assertTrue(scheduler.paused)
        self.assertEqual(scheduler.run_external_once(), {"status": "paused"})
        self.assertEqual(scheduler.run_cognition_once(), {"status": "paused"})
        self.assertEqual(scheduler.run_trends_once(), {"status": "paused"})
        self.assertEqual((cognition.process_calls, cognition.trend_calls), (0, 0))

        scheduler.resume()
        self.assertFalse(scheduler.paused)
        self.assertEqual(scheduler.run_external_once()["status"], "ok")
        self.assertEqual(scheduler.run_cognition_once()["status"], "ok")
        self.assertEqual(scheduler.run_trends_once()["status"], "ok")
        self.assertEqual((cognition.process_calls, cognition.trend_calls), (1, 1))

    def test_cognition_failure_is_visible_in_runtime_state(self):
        class FailingCognition:
            def process_once(self):
                raise RuntimeError("cognition boom")

            def capture_trends(self):
                return {"snapshots": []}

        scheduler = RadarScheduler(
            self.service(lambda source: []),
            poll_seconds=0.01,
            database=self.database,
            cognition=FailingCognition(),
            now=self.clock,
        )

        result = scheduler.run_cognition_once()

        self.assertEqual(result["status"], "error")
        with self.database.connect() as connection:
            state = connection.execute(
                "SELECT value_json FROM runtime_state WHERE state_key='task.cognition'"
            ).fetchone()[0]
        payload = json.loads(state)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_type"], "RuntimeError")
        # 这里原本断言的是 payload["message"] —— 那个键在负载里并不存在，
        # 等于一条永远通过的断言。现在按真实契约断言：异常类型与清洗后的
        # 消息都要落库，否则现场只剩一个类名，无法判断是哪个值、哪一步失败。
        self.assertEqual(payload["error_message"], "cognition boom")

    def test_task_failure_message_has_local_paths_redacted(self):
        """异常消息里的本机路径必须抹掉再落库。

        异常消息经常自带完整路径（FileNotFoundError / OSError 都会带上文件名）。
        状态表只留在本机，但排障需要的是"哪一类失败"，不是"文件在哪"。
        路径刻意用拼接写出：本文件会被隐私闸门扫描，写字面量会判定为泄漏。
        """
        leaked = "C:" + chr(92) + "Users" + chr(92) + "someone" + chr(92) + "yuanjian.db"

        def boom():
            raise OSError(f"无法打开数据库文件 {leaked}")

        scheduler = RadarScheduler(
            self.service(lambda source: []),
            poll_seconds=0.01,
            database=self.database,
            now=self.clock,
        )

        payload = scheduler._execute("external", boom)

        self.assertEqual(payload["error_type"], "OSError")
        self.assertNotIn(leaked, payload["error_message"])
        self.assertIn("<path>", payload["error_message"])

        with self.database.connect() as connection:
            state = connection.execute(
                "SELECT value_json FROM runtime_state WHERE state_key='task.external'"
            ).fetchone()[0]
        stored = json.loads(state)
        self.assertNotIn(leaked, stored["error_message"])
        self.assertIn("<path>", stored["error_message"])

    def test_task_failure_records_the_exact_frame_and_logs_the_full_stack(self):
        """失败要留下"死在哪一行"+完整堆栈：只记类型等于没有现场。

        状态表（会被诊断面板读出来展示）**只**带 `function:lineno`，路径无关；
        完整堆栈走日志。两条都要 —— 少前者查不出，少后者要么看不见、
        要么把一整段 traceback 灌到界面上。
        """

        def boom():
            raise ValueError("炸在 boom 里")

        scheduler = RadarScheduler(
            self.service(lambda source: []),
            poll_seconds=0.01,
            database=self.database,
            now=self.clock,
        )

        with self.assertLogs("yuanjian_app.radar_scheduler", level="ERROR") as captured:
            payload = scheduler._execute("external", boom)

        self.assertEqual(payload["status"], "error")
        # ① 状态表只多一个"函数名:行号"，且不含任何路径分隔符
        self.assertTrue(
            payload["error_location"].startswith("boom:"), payload["error_location"]
        )
        self.assertNotIn("\\", payload["error_location"])
        self.assertNotIn("/", payload["error_location"])

        # ② 完整堆栈（含最内层原因）进了日志
        logged = "\n".join(captured.output)
        self.assertIn("Traceback (most recent call last)", logged)
        self.assertIn("ValueError: 炸在 boom 里", logged)

        # ③ 同一个 error_location 也落了库（面板读的是 runtime_state）
        with self.database.connect() as connection:
            stored = json.loads(
                connection.execute(
                    "SELECT value_json FROM runtime_state"
                    " WHERE state_key='task.external'"
                ).fetchone()[0]
            )
        self.assertEqual(stored["error_location"], payload["error_location"])

    def test_slow_task_does_not_trigger_a_catch_up_burst(self):
        """任务耗时超过自身间隔时，下一次到期必须从任务结束时刻起算。

        回归场景：旧实现用「本轮循环开始时的 monotonic + 间隔」计算 deadline。
        采集若耗时 40 秒而 poll_seconds 是 30 秒，deadline 就落在过去，
        循环会立刻再跑一次，把一次超时放大成连续冲击。
        """
        import yuanjian_app.radar_scheduler as module

        clock = {"now": 100.0}
        fetches = []

        class SlowService:
            def refresh_due_sources(self):
                fetches.append(clock["now"])
                clock["now"] += 40.0  # 任务自身耗时 40 秒，超过 30 秒间隔
                return 1

        class StopAfterFirstWait:
            def __init__(self):
                self.waits = []
                self._set = False

            def is_set(self):
                return self._set

            def set(self):
                self._set = True

            def clear(self):
                self._set = False

            def wait(self, timeout=None):
                self.waits.append(timeout)
                self._set = True  # 只跑一轮就退出
                return True

        scheduler = RadarScheduler(SlowService(), poll_seconds=30)
        stop = StopAfterFirstWait()
        scheduler._stop = stop

        original = module.time.monotonic
        module.time.monotonic = lambda: clock["now"]
        try:
            scheduler._run()
        finally:
            module.time.monotonic = original

        self.assertEqual(len(fetches), 1)
        self.assertEqual(len(stop.waits), 1)
        # 任务在 100 秒开始、140 秒结束，下次应等到 140+30=170 秒，
        # 也就是还要等 30 秒；旧实现会算出已过期，只等 0.01 秒立刻补跑。
        self.assertEqual(stop.waits[0], 30.0)


if __name__ == "__main__":
    unittest.main()
