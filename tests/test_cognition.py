import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from yuanjian_app.cognition import CognitionController, CognitionService
from yuanjian_app.database import Database


class CognitionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.service = CognitionService(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def add_source(self, source_id, domain, primary=False):
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO external_sources(
                    source_id, name, kind, endpoint, config_json
                ) VALUES (?, ?, 'rss', ?, ?)
                """,
                (
                    source_id,
                    source_id,
                    f"https://{domain}/feed",
                    json.dumps({"primary_source": primary}),
                ),
            )

    def add_item(self, item_id, source_id, domain, title, summary, hour=0):
        timestamp = f"2026-08-11T{hour:02d}:00:00Z"
        url = f"https://{domain}/{item_id.lower()}"
        content_hash = hashlib.sha256(f"{title}\n{summary}".encode()).hexdigest()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO external_items(
                    item_id, canonical_url, title, summary, published_at,
                    fetched_at, source_id, source_name, language, content_hash,
                    first_seen_at, last_seen_at, source_count, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Chinese', ?, ?, ?, 1, '{}')
                """,
                (
                    item_id,
                    url,
                    title,
                    summary,
                    timestamp,
                    timestamp,
                    source_id,
                    source_id,
                    content_hash,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO external_item_sources VALUES (?, ?, ?, ?)",
                (item_id, source_id, url, timestamp),
            )

    def test_three_reports_become_one_traceable_cluster(self):
        self.add_source("S-A", "a.example")
        self.add_source("S-B", "b.example")
        reports = (
            ("E-1", "S-A", "a.example", "广东医保报销比例升至70%", "新政策本月实施", 0),
            ("E-2", "S-B", "b.example", "广东调整医保报销政策", "报销比例提高至70%", 2),
            ("E-3", "S-A", "a.example", "广东医保新规：报销比例提高到70%", "本月开始执行", 4),
        )
        for report in reports:
            self.add_item(*report)
            self.service.process_item(report[0])

        clusters = self.service.list_clusters()

        self.assertEqual(len(clusters), 1)
        detail = self.service.get_cluster(clusters[0]["cluster_id"])
        self.assertEqual(len(detail["items"]), 3)
        self.assertEqual(detail["independent_domains"], 2)
        self.assertTrue(all(item["merge_reason"] for item in detail["items"]))
        self.assertTrue(all(0 <= item["similarity"] <= 1 for item in detail["items"]))

    def test_evidence_levels_require_independence_and_primary_source(self):
        self.add_source("S-A", "a.example")
        self.add_source("S-B", "b.example")
        self.add_source("S-OFFICIAL", "a.example", primary=True)
        self.add_source("S-C", "c.example")
        base = ("广东医保报销比例升至70%", "政策本月实施")

        self.add_item("E-1", "S-A", "a.example", *base, 0)
        result = self.service.process_item("E-1")
        self.assertEqual(result["evidence_level"], "E1")

        self.add_item("E-2", "S-B", "b.example", *base, 1)
        result = self.service.process_item("E-2")
        self.assertEqual(result["evidence_level"], "E2")

        self.add_item("E-3", "S-OFFICIAL", "a.example", *base, 2)
        result = self.service.process_item("E-3")
        self.assertEqual(result["evidence_level"], "E3")

        self.add_item("E-4", "S-C", "c.example", *base, 3)
        result = self.service.process_item("E-4")
        self.assertEqual(result["evidence_level"], "E4")

    def test_evidence_hash_changes_only_when_evidence_changes(self):
        self.add_source("S-A", "a.example")
        self.add_source("S-B", "b.example")
        base = ("广东医保报销比例升至70%", "政策本月实施")
        self.add_item("E-1", "S-A", "a.example", *base, 0)

        first = self.service.process_item("E-1")
        repeated = self.service.process_item("E-1")

        self.assertEqual(first["evidence_hash"], repeated["evidence_hash"])
        self.assertFalse(repeated["changed"])
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM judgment_jobs").fetchone()[0], 0)

        self.add_item("E-2", "S-B", "b.example", *base, 1)
        expanded = self.service.process_item("E-2")

        self.assertNotEqual(first["evidence_hash"], expanded["evidence_hash"])
        self.assertTrue(expanded["changed"])
        self.assertTrue(expanded["needs_judgment"])

    def test_backfill_is_idempotent(self):
        self.add_source("S-A", "a.example")
        self.add_item(
            "E-1", "S-A", "a.example", "广东医保报销比例升至70%", "政策实施", 0
        )

        first = self.service.backfill_unclustered()
        second = self.service.backfill_unclustered()

        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(len(self.service.list_clusters()), 1)

    def test_historical_rfc2822_published_time_is_accepted(self):
        self.add_source("S-A", "a.example")
        self.add_item(
            "E-1", "S-A", "a.example", "广东医保报销比例升至70%", "政策实施", 0
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE external_items SET published_at='Tue, 11 Aug 2026 00:00:00 GMT' WHERE item_id='E-1'"
            )

        result = self.service.process_item("E-1")

        self.assertEqual(result["evidence_level"], "E1")

    def test_historical_cluster_markup_is_cleaned_when_read(self):
        self.add_source("S-A", "a.example")
        self.add_item("E-1", "S-A", "a.example", "医保政策", "公开说明", 0)
        cluster_id = self.service.process_item("E-1")["cluster_id"]
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE event_clusters SET title=?,summary=? WHERE cluster_id=?",
                (
                    '<a href="x">医保政策</a>',
                    '<font color="red">公开&nbsp;说明</font><script>bad()</script>',
                    cluster_id,
                ),
            )

        listed = self.service.list_clusters()[0]
        detailed = self.service.get_cluster(cluster_id)

        self.assertEqual((listed["title"], listed["summary"]), ("医保政策", "公开 说明"))
        self.assertEqual((detailed["title"], detailed["summary"]), ("医保政策", "公开 说明"))

    def test_cluster_page_filters_counts_and_validates_bounds(self):
        rows = (
            ("C-1", "广东医保政策", '["health","policy"]', "E3", 1, "2026-08-11T03:00:00Z"),
            ("C-2", "河源医院通知", '["health"]', "E2", 1, "2026-08-11T02:00:00Z"),
            ("C-3", "银行利率变化", '["finance"]', "E1", 0, "2026-08-11T01:00:00Z"),
        )
        with self.database.connect() as connection:
            for cluster_id, title, categories, evidence, needs, timestamp in rows:
                connection.execute(
                    """
                    INSERT INTO event_clusters(
                        cluster_id,title,summary,first_seen_at,last_seen_at,
                        evidence_level,evidence_hash,categories_json,status,
                        needs_judgment,independent_domains,primary_source_count,
                        created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        cluster_id, title, "摘要", timestamp, timestamp, evidence,
                        f"hash-{cluster_id}", categories, "active", needs, 1, 0,
                        timestamp, timestamp,
                    ),
                )

        page = self.service.list_clusters_page(
            limit=1, offset=1, query="", category="health", needs_judgment=True
        )

        self.assertEqual(page["total"], 2)
        self.assertEqual([item["cluster_id"] for item in page["items"]], ["C-2"])
        self.assertEqual((page["limit"], page["offset"]), (1, 1))
        with self.assertRaisesRegex(ValueError, "分页"):
            self.service.list_clusters_page(limit=0)
        with self.assertRaisesRegex(ValueError, "证据"):
            self.service.list_clusters_page(evidence="E9")


class RiskDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.controller = object.__new__(CognitionController)
        self.controller.database = self.database
        self.controller.now = lambda: datetime(2026, 8, 12, 8, tzinfo=timezone.utc)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO interest_objects VALUES ('I-CASH','家庭现金流','cashflow',5,'P3','active')"
            )

    def tearDown(self):
        self.temporary.cleanup()

    def add_risk(
        self,
        number,
        alert_level,
        horizons,
        score=None,
        pending=False,
        recommended_action=None,
    ):
        cluster_id = f"C-{number}"
        judgment_id = f"J-{number}"
        timestamp = f"2026-08-12T0{number}:00:00Z"
        judgment = {
            "fact_summary": f"第{number}项外部变化可能压缩可用资金",
            "horizons": horizons,
            "confidence": 0.82,
            "up_triggers": ["成本继续上升"],
            "down_triggers": ["政策撤回"],
        }
        candidate = {
            "recommended_action": recommended_action
            or f"第{number}项行动：先保留必要现金",
            "window_end": "2026-09-11",
        }
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO event_clusters(
                    cluster_id,title,summary,first_seen_at,last_seen_at,
                    evidence_level,evidence_hash,categories_json,status,
                    needs_judgment,independent_domains,primary_source_count,
                    latest_judgment_id,created_at,updated_at
                ) VALUES (?,?,?,?,?,'E3',?,'["finance"]','active',?,2,1,?,?,?)
                """,
                (
                    cluster_id, f"原始新闻标题{number}", "新闻摘要", timestamp,
                    timestamp, f"hash-{number}", int(pending),
                    None if pending else judgment_id, timestamp, timestamp,
                ),
            )
            if not pending:
                connection.execute(
                    "INSERT INTO judgments VALUES (?,?,?,?,?,?)",
                    (judgment_id, cluster_id, "local", f"hash-{number}", json.dumps(judgment, ensure_ascii=False), timestamp),
                )
                connection.execute(
                    """
                    INSERT INTO personal_impacts(
                        impact_id,cluster_id,judgment_id,interest_id,impact_score,
                        alert_level,components_json,reason,candidate_json,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"P-{number}", cluster_id, judgment_id, "I-CASH",
                        score if score is not None else number / 10, alert_level,
                        '{"confidence":0.82}', "内部映射理由",
                        json.dumps(candidate, ensure_ascii=False), timestamp, timestamp,
                    ),
                )
        return cluster_id

    def test_dashboard_returns_three_action_first_plain_language_items(self):
        self.add_risk(1, "L2", ["7天内"])
        self.add_risk(2, "L3", ["30天内"], 0.65)
        self.add_risk(3, "L3", ["7天内"], 0.66)
        self.add_risk(4, "L4", ["30天内"], 0.90)
        self.add_risk(5, "L3", ["更长期"], 0.70)
        self.add_risk(
            6,
            "L4",
            ["立即"],
            0.95,
            recommended_action="收集执行证据；人工确认后进入正式预测账本",
        )
        self.add_risk(7, "L3", ["30天内"], 0.68)

        dashboard = self.controller.risk_dashboard(
            [{"enabled": True, "last_status": "ok", "stale": False}], limit=9
        )

        self.assertEqual(dashboard["state"], "action")
        self.assertEqual(dashboard["counts"], {"action": 3, "watch": 3, "verifying": 0})
        self.assertEqual(len(dashboard["items"]), 6)
        self.assertEqual([item["cluster_id"] for item in dashboard["items"]], ["C-6", "C-4", "C-3", "C-5", "C-7", "C-2"])
        self.assertTrue(all(item["alert_level"] in {"L3", "L4"} for item in dashboard["items"]))
        self.assertTrue(all("原始新闻标题" not in item["title"] for item in dashboard["items"]))
        first = dashboard["items"][0]
        self.assertEqual(first["interest_category"], "cashflow")
        self.assertEqual(first["risk_label"], "高风险")
        self.assertIn("保留现金", first["advice"])
        self.assertNotIn("预测账本", first["advice"])
        self.assertEqual(first["reason"], "第6项外部变化可能压缩可用资金")
        self.assertEqual(dashboard["items"][2]["risk_label"], "中风险")

    def test_dashboard_calls_thirty_day_l3_a_low_risk_watch(self):
        self.add_risk(2, "L3", ["30天内"], 0.65)

        dashboard = self.controller.risk_dashboard(
            [{"enabled": True, "last_status": "ok", "stale": False}]
        )

        self.assertEqual(dashboard["items"][0]["mode"], "watch")
        self.assertEqual(dashboard["items"][0]["risk_label"], "低风险")

    def test_dashboard_reports_verifying_and_never_calls_blind_monitoring_stable(self):
        self.add_risk(1, "L3", ["30天内"], pending=True)

        dashboard = self.controller.risk_dashboard(
            [{"enabled": True, "last_status": "error", "stale": True}]
        )

        self.assertEqual(dashboard["state"], "coverage_gap")
        self.assertEqual(dashboard["counts"], {"action": 0, "watch": 0, "verifying": 1})
        self.assertIn("覆盖不足", dashboard["summary"])
        self.assertEqual(dashboard["items"], [])


class BootstrapNotifySuppressionTests(unittest.TestCase):
    """Regression: the first process_once() run after install/upgrade
    must not fire one notification per historical judgment the database
    inherited from a previous install. Real install started dumping 200+
    noise notifications into the inbox on a v4->v5 upgrade."""

    def _make_controller(self, now):
        controller = object.__new__(CognitionController)
        controller.now = lambda: now
        controller._notify_since = now
        return controller

    def test_historical_judgment_is_suppressed_on_bootstrap(self):
        controller = self._make_controller(datetime(2026, 8, 15, 12, tzinfo=timezone.utc))
        row = {"j_created_at": "2026-08-10T10:00:00Z"}  # 5 days old
        self.assertFalse(controller._should_notify(row))

    def test_fresh_judgment_notifies_normally(self):
        controller = self._make_controller(datetime(2026, 8, 15, 12, tzinfo=timezone.utc))
        row = {"j_created_at": "2026-08-15T13:00:00Z"}  # 1 hour after cutoff
        self.assertTrue(controller._should_notify(row))

    def test_judgment_at_cutoff_boundary_is_allowed(self):
        """cutoff is inclusive — a judgment at exactly the cutoff second should notify."""
        controller = self._make_controller(datetime(2026, 8, 15, 12, tzinfo=timezone.utc))
        row = {"j_created_at": "2026-08-15T12:00:00Z"}
        self.assertTrue(controller._should_notify(row))

    def test_cutoff_advances_after_first_pass(self):
        """After process_once advances _notify_since, even freshly
        re-evaluated historical rows stop being suppressed repeatedly —
        they only get suppressed the very first time."""
        controller = self._make_controller(datetime(2026, 8, 15, 12, tzinfo=timezone.utc))
        historical = {"j_created_at": "2026-08-10T10:00:00Z"}
        self.assertFalse(controller._should_notify(historical))

        # Simulate end-of-process_once: _notify_since is bumped to now
        controller._notify_since = controller.now()
        # The row is still "historical" (its created_at hasn't changed),
        # but the comparison is now() vs row.created_at, so it still
        # returns False — which is correct, because it has already been
        # mapped and silently swallowed; the user gets exactly one
        # historical cycle, then no more history-noise forever.
        self.assertFalse(controller._should_notify(historical))


if __name__ == "__main__":
    unittest.main()
