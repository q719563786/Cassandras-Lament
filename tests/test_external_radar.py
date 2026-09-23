import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from yuanjian_app import external_radar
from yuanjian_app.database import Database
from yuanjian_app.external_radar import ExternalRadarService
from yuanjian_app.external_sources import ExternalItem, FetchError


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 7, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class ExternalRadarTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "yuanjian.db")
        self.database.initialize()
        self.clock = MutableClock()

    def tearDown(self):
        self.temp_dir.cleanup()

    def item(self, source_id="S-1", source_name="Official feed"):
        return ExternalItem(
            source_id=source_id,
            source_name=source_name,
            url="https://example.com/notices/88?utm_source=rss",
            title="河源水泵项目公开招标",
            summary="消防工程设备采购",
            published_at="2026-08-07T08:30:00Z",
        )

    def service(self, fetcher):
        return ExternalRadarService(self.database, fetcher=fetcher, now=self.clock)

    def add_source_and_rule(self, service, source_id="S-1", name="Official feed"):
        service.add_source(
            {
                "source_id": source_id,
                "name": name,
                "kind": "rss",
                "endpoint": f"https://example.com/{source_id}.xml",
                "refresh_minutes": 15,
                "reliability_weight": 0.9,
            }
        )
        service.add_watch_rule(
            {"rule_id": "W-1", "query": "水泵", "importance": 5}
        )

    def test_repeated_refresh_is_idempotent_and_only_matches_relevant_items(self):
        service = self.service(lambda source: [self.item()])
        self.add_source_and_rule(service)

        first = service.refresh_source("S-1")
        second = service.refresh_source("S-1")
        radar = service.radar_items()

        self.assertEqual(first["new_count"], 1)
        self.assertEqual(second["new_count"], 0)
        self.assertEqual(len(radar), 1)
        self.assertEqual(radar[0]["matched_rules"][0]["query"], "水泵")
        self.assertIn(radar[0]["alert_level"], {"L3", "L4"})

    def test_unmatched_external_item_stays_out_of_main_radar(self):
        irrelevant = ExternalItem(
            source_id="S-1",
            source_name="Official feed",
            url="https://example.com/sports/1",
            title="足球比赛结果",
            summary="本轮联赛比分",
        )
        service = self.service(lambda source: [irrelevant])
        self.add_source_and_rule(service)

        service.refresh_source("S-1")

        self.assertEqual(service.radar_items(), [])
        with self.database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM external_items").fetchone()[0]
        self.assertEqual(count, 1)

    def test_same_url_from_two_sources_increases_independent_source_count(self):
        def fetcher(source):
            return [self.item(source["source_id"], source["name"])]

        service = self.service(fetcher)
        self.add_source_and_rule(service)
        service.add_source(
            {
                "source_id": "S-2",
                "name": "Second source",
                "kind": "rss",
                "endpoint": "https://example.org/feed.xml",
            }
        )

        service.refresh_source("S-1")
        service.refresh_source("S-2")

        self.assertEqual(service.radar_items()[0]["source_count"], 2)

    def test_source_failure_keeps_cached_items_and_records_failure_type(self):
        calls = 0

        def fetcher(source):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [self.item()]
            raise FetchError("timeout", "外部源请求超时")

        service = self.service(fetcher)
        self.add_source_and_rule(service)
        service.refresh_source("S-1")

        failed = service.refresh_source("S-1")

        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["error_type"], "timeout")
        self.assertEqual(len(service.radar_items()), 1)
        source = service.list_sources()[0]
        self.assertEqual(source["consecutive_failures"], 1)
        self.assertIn("超时", source["last_error"])

    def test_source_becomes_stale_after_twice_its_refresh_interval(self):
        service = self.service(lambda source: [self.item()])
        self.add_source_and_rule(service)
        service.refresh_source("S-1")

        self.clock.value += timedelta(minutes=31)

        self.assertTrue(service.list_sources()[0]["stale"])

    def test_identical_watch_rule_is_reused_instead_of_duplicated(self):
        service = self.service(lambda source: [])

        first = service.add_watch_rule({"query": "水泵", "importance": 5})
        second = service.add_watch_rule({"query": "水泵", "importance": 5})

        self.assertEqual(second, first)
        self.assertEqual(len(service.list_rules()), 1)

    def test_item_callback_runs_after_commit_and_failure_is_audited(self):
        observed = []

        def callback(item_id):
            with self.database.connect() as connection:
                observed.append(
                    connection.execute(
                        "SELECT title FROM external_items WHERE item_id=?", (item_id,)
                    ).fetchone()[0]
                )
            raise RuntimeError("cluster test failure")

        service = ExternalRadarService(
            self.database,
            fetcher=lambda source: [self.item()],
            now=self.clock,
            on_item_stored=callback,
        )
        self.add_source_and_rule(service)

        result = service.refresh_source("S-1")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(observed, ["河源水泵项目公开招标"])
        with self.database.connect() as connection:
            audit = connection.execute(
                "SELECT action, details_json FROM audit_log ORDER BY audit_id DESC"
            ).fetchone()
        self.assertEqual(audit["action"], "cognition.process_failed")
        self.assertIn("RuntimeError", audit["details_json"])

    def test_new_feed_items_are_stored_and_returned_as_visible_text(self):
        marked_up = ExternalItem(
            source_id="S-1",
            source_name="Official feed",
            url="https://example.com/notices/markup",
            title='<a href="https://example.com">河源水泵招标</a>',
            summary='<font color="red">消防&nbsp;设备</font><script>steal()</script>',
        )
        service = self.service(lambda source: [marked_up])
        self.add_source_and_rule(service)

        service.refresh_source("S-1")
        item = service.radar_items()[0]

        self.assertEqual(item["title"], "河源水泵招标")
        self.assertEqual(item["summary"], "消防 设备")
        with self.database.connect() as connection:
            stored = connection.execute(
                "SELECT title,summary FROM external_items WHERE item_id=?",
                (item["item_id"],),
            ).fetchone()
        self.assertEqual((stored["title"], stored["summary"]), ("河源水泵招标", "消防 设备"))

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE external_items SET title=?,summary=? WHERE item_id=?",
                ("<b>河源水泵招标</b>", "<i>消防&nbsp;设备</i>", item["item_id"]),
            )

        historical = service.radar_items()[0]
        self.assertEqual((historical["title"], historical["summary"]), ("河源水泵招标", "消防 设备"))

    def test_radar_page_filters_and_returns_total_before_slicing(self):
        items = [
            ExternalItem("S-1", "Official feed", "https://example.com/a", "河源水泵项目甲", "公开招标"),
            ExternalItem("S-1", "Official feed", "https://example.com/b", "河源水泵项目乙", "采购公告"),
            ExternalItem("S-1", "Official feed", "https://example.com/c", "深圳水泵项目", "采购公告"),
        ]
        service = self.service(lambda source: items)
        self.add_source_and_rule(service)
        service.refresh_source("S-1")

        page = service.radar_page(limit=1, offset=1, query="河源")

        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["items"]), 1)

    def test_bulk_set_enabled_with_no_filter_flips_every_source(self):
        """The Action Home '批量启用/批量停用' buttons rely on this: when
        the user has the '全部区域' filter active, one POST must flip
        every source at once, not just one."""
        service = self.service(lambda source: [])
        # Seed three sources via the public add path so they go through the
        # same validation the HTTP endpoint uses.
        for sid in ("S-H-A", "S-G-A", "S-N-A"):
            service.add_source({
                "source_id": sid, "name": sid, "kind": "rss",
                "endpoint": f"https://example.com/{sid}.xml",
                "refresh_minutes": 15, "reliability_weight": 0.7,
            })
        # Bulk disable everything.
        result = service.bulk_set_enabled(False)
        self.assertEqual(result["updated"], 3)
        self.assertFalse(result["enabled"])
        with self.database.connect() as conn:
            rows = conn.execute("SELECT source_id, enabled FROM external_sources").fetchall()
        self.assertEqual({r[0]: bool(r[1]) for r in rows}, {"S-H-A": False, "S-G-A": False, "S-N-A": False})
        # Bulk re-enable everything.
        result = service.bulk_set_enabled(True)
        self.assertEqual(result["updated"], 3)
        with self.database.connect() as conn:
            rows = conn.execute("SELECT source_id, enabled FROM external_sources").fetchall()
        self.assertEqual({r[0]: bool(r[1]) for r in rows}, {"S-H-A": True, "S-G-A": True, "S-N-A": True})

    def test_bulk_set_enabled_with_region_filter_only_touches_matching_sources(self):
        service = self.service(lambda source: [])
        # Seed sources across two regions; the Heyuan one must stay
        # untouched when the bulk operation targets Guangdong.
        for sid, region in (
            ("S-HY-1", "heyuan"), ("S-HY-2", "heyuan"),
            ("S-GD-1", "guangdong"), ("S-GD-2", "guangdong"),
        ):
            service.add_source({
                "source_id": sid, "name": sid, "kind": "rss",
                "endpoint": f"https://example.com/{sid}.xml",
                "refresh_minutes": 15, "reliability_weight": 0.7,
                "region": region,
            })
        result = service.bulk_set_enabled(False, region="guangdong")
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["region"], "guangdong")
        with self.database.connect() as conn:
            rows = {
                r[0]: bool(r[1])
                for r in conn.execute("SELECT source_id, enabled FROM external_sources").fetchall()
            }
        # Guangdong flipped, Heyuan untouched.
        self.assertEqual(
            rows, {"S-HY-1": True, "S-HY-2": True, "S-GD-1": False, "S-GD-2": False}
        )


class VirtualMonotonic:
    """替身单调时钟：**不真睡**，每个源"开工"时把时钟推 `cost` 秒。

    用虚拟耗时而不是 `time.sleep`：40 源 × 2 秒的验收用例真睡要 80 秒以上，
    测试为了跑得快只能把预算调大 —— 那等于把要验证的东西一起关掉
    （同一个教训见 `remote_ai.MinIntervalPacer` 那里）。
    """

    def __init__(self, cost):
        self.now = 1000.0
        self.cost = float(cost)

    def monotonic(self):
        return self.now

    def charge(self):
        self.now += self.cost


class CollectPassBudgetTests(unittest.TestCase):
    """采集**每轮时间预算**：任何一次调度迭代都不许长时间独占单线程循环。

    故障现场：冷启动时几乎所有常规源都已到期，`refresh_due_sources` 在一次调用里
    把到期源全部抓完。真库实测（`build-artifacts/v152/c-baseline-real.txt`）：
    35 个到期源、单源中位数 2.4 s（≈88 s），但对端大面积超时时单源可达 60~69 s
    ⇒ 最坏一次 pass ≈ 35 分钟。那段时间里循环轮不到认知/备份，首页只读接口也拿不到
    响应 —— 用户可感的说法就是"刚开机那段时间首页不响应"。
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "yuanjian.db")
        self.database.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _add_due_sources(self, count, service):
        for index in range(count):
            service.add_source(
                {
                    "source_id": "S-%03d" % index,
                    "name": "源 %03d" % index,
                    "kind": "rss",
                    "endpoint": "https://example.com/%03d.xml" % index,
                    "refresh_minutes": 15,
                    "reliability_weight": 0.7,
                }
            )

    def _service(self, virtual, *, seen=None, budget=None, clock=None):
        def fetcher(source):
            if seen is not None:
                seen.append(source["source_id"])
            virtual.charge()  # 每个源消耗虚拟时间，替代真睡
            return []

        return ExternalRadarService(
            self.database,
            fetcher=fetcher,
            now=clock or MutableClock(),
            pass_budget_seconds=budget,
        )

    def _due_count(self):
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM external_sources"
                " WHERE enabled=1 AND (next_fetch_at IS NULL OR next_fetch_at<=?)",
                (external_radar.iso(MutableClock()()),),
            ).fetchone()[0]

    def test_one_pass_never_exceeds_the_budget_and_defers_the_rest(self):
        virtual = VirtualMonotonic(cost=5.0)
        service = self._service(virtual)
        self._add_due_sources(40, service)

        with mock.patch.object(external_radar, "time", virtual):
            began = virtual.monotonic()
            attempted = service.refresh_due_sources()
            elapsed = virtual.monotonic() - began

        budget = external_radar.COLLECT_PASS_BUDGET_SECONDS
        self.assertLessEqual(elapsed, budget, "单轮 pass 超过了时间预算")
        self.assertEqual(attempted, int(budget // 5.0))
        self.assertEqual(attempted, 24, "预算 120s / 单源 5s 应为 24 个")
        # 剩下的**仍然到期**（原样不动）——留到下一轮，不是被丢掉
        self.assertEqual(self._due_count(), 40 - attempted)

    def test_repeated_passes_fetch_every_source_exactly_once(self):
        """不许饿死任何一个源：多轮之后必须**全部**抓到，且不重复抓。"""
        virtual = VirtualMonotonic(cost=5.0)
        seen = []
        service = self._service(virtual, seen=seen)
        self._add_due_sources(40, service)
        budget = external_radar.COLLECT_PASS_BUDGET_SECONDS

        rounds = 0
        with mock.patch.object(external_radar, "time", virtual):
            while self._due_count():
                rounds += 1
                self.assertLess(rounds, 10, "预算把队列推不动了（源被饿死）")
                began = virtual.monotonic()
                attempted = service.refresh_due_sources()
                self.assertLessEqual(virtual.monotonic() - began, budget)
                self.assertGreaterEqual(attempted, 1, "空转一轮 —— 有到期源却一个都没抓")

        self.assertEqual(len(seen), 40, "有源在多轮之间丢了")
        self.assertEqual(len(set(seen)), 40, "同一个源被重复抓了")
        self.assertEqual(rounds, 2, "40 源 / 每轮 24 个应为 2 轮")

    def test_a_single_source_slower_than_the_budget_still_makes_progress(self):
        """一个源就吃掉整个预算时，也必须**动一下**，不能空转、不能死循环。

        这是"至少开一次"那条保证：预算再小，一轮也必然推进一个源。
        """
        virtual = VirtualMonotonic(cost=500.0)  # 单源 500s ≫ 预算 120s
        seen = []
        service = self._service(virtual, seen=seen)
        self._add_due_sources(3, service)

        with mock.patch.object(external_radar, "time", virtual):
            attempted = service.refresh_due_sources()

        self.assertEqual(attempted, 1)
        self.assertEqual(seen, ["S-000"])

    def test_honours_an_explicitly_injected_budget(self):
        virtual = VirtualMonotonic(cost=2.0)
        service = self._service(virtual, budget=10.0)
        self._add_due_sources(40, service)

        with mock.patch.object(external_radar, "time", virtual):
            attempted = service.refresh_due_sources()

        self.assertEqual(attempted, 5, "注入的预算没生效")

    def test_the_shipped_budget_sits_between_the_healthy_and_the_pathological_pass(self):
        """预算取值的依据必须写死在测试里（否则将来谁改都能自称"有依据"）。

        真库实测（近 7 天 external_runs，见 c-baseline-real.txt）：
        35 个到期源 × 单源中位数 2.5 s ≈ 88 s（常态）；单源 max 68.9 s ⇒ 病态可达
        35 分钟。预算取 120 s：常态仍一次抓完（不增加额外延迟），病态被压到 2 分钟。
        """
        budget = external_radar.COLLECT_PASS_BUDGET_SECONDS
        healthy_pass = 35 * 2.5
        pathological_single_source = 60.0
        pathological_pass = 35 * pathological_single_source

        self.assertGreater(
            budget, healthy_pass, "预算小于常态一次 pass：健康网络也被切碎成多轮"
        )
        self.assertLess(
            budget,
            pathological_pass / 10,
            "预算相对病态一次 pass 太大：仍然可以独占调度循环十几分钟",
        )


if __name__ == "__main__":
    unittest.main()
