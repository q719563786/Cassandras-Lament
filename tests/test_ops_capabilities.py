"""六项新能力的单元测试：备份 / 保留 / 诊断 / 反馈学习 / 设置 / 移动导出 / 校准。"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yuanjian_app.backup import BackupService, read_backup_setting, write_backup_setting
from yuanjian_app.database import Database
from yuanjian_app.diagnostics import DiagnosticsService
from yuanjian_app.forecasts import ForecastService
from yuanjian_app.mobile_export import MobileExportService
from yuanjian_app.retention import (
    RetentionService,
    read_retention_setting,
    write_retention_setting,
)
from yuanjian_app.system_settings import (
    SystemSettingsService,
    read_learning_setting,
    write_learning_setting,
)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "data" / "yuanjian.db")
        self.database.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_produces_integrity_checked_rolling_set(self):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO forecasts(forecast_id, status, window_end, category)"
                " VALUES ('F-1', 'open', '2026-12-31', 'finance')"
            )
        moments = [
            datetime(2026, 8, d, 3, 0, tzinfo=timezone.utc) for d in range(1, 10)
        ]
        service = BackupService(
            self.database, Path(self.temp.name) / "backups", now=lambda: moments[0]
        )
        for moment in moments:
            service.now = lambda moment=moment: moment
            service.run()

        files = sorted(
            (Path(self.temp.name) / "backups").glob("yuanjian-*.db")
        )
        self.assertEqual(len(files), 7)  # 滚动保留 7 份
        latest = service.latest()
        self.assertIsNotNone(latest)
        restored = Database(files[-1])
        with restored.connect() as connection:
            kept = connection.execute(
                "SELECT forecast_id FROM forecasts"
            ).fetchall()
        self.assertEqual([row["forecast_id"] for row in kept], ["F-1"])

    def test_backup_setting_roundtrip_and_clamps_hour(self):
        write_backup_setting(
            self.database, {"enabled": True, "hour": 99}, now=datetime(2026, 8, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(
            read_backup_setting(self.database), {"enabled": True, "hour": 23, "keep": 7}
        )
        with self.assertRaises(ValueError):
            write_backup_setting(self.database, {"hour": "bad"})


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "yuanjian.db")
        self.database.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def seed_item(self, item_id, published_at):
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO external_items(
                    item_id, canonical_url, title, summary, published_at, fetched_at,
                    source_id, source_name, content_hash, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'S-1', '源', ?, ?, ?)
                """,
                (
                    item_id,
                    f"https://example.com/{item_id}",
                    item_id,
                    "摘要",
                    published_at,
                    published_at,
                    f"hash-{item_id}",
                    published_at,
                    published_at,
                ),
            )
            connection.execute(
                "INSERT INTO external_item_sources(item_id, source_id, url, first_seen_at)"
                " VALUES (?, 'S-1', ?, ?)",
                (item_id, f"https://example.com/{item_id}", published_at),
            )

    def test_retention_deletes_only_stale_items_and_audits(self):
        now = datetime(2026, 8, 15, tzinfo=timezone.utc)
        old = (now - timedelta(days=90)).isoformat()
        fresh = (now - timedelta(days=5)).isoformat()
        self.seed_item("old-1", old)
        self.seed_item("fresh-1", fresh)
        service = RetentionService(self.database, now=lambda: now)

        result = service.run()

        self.assertEqual(result["deleted_items"], 1)
        with self.database.connect() as connection:
            remaining = {
                row["item_id"]
                for row in connection.execute("SELECT item_id FROM external_items")
            }
            sources = {
                row["item_id"]
                for row in connection.execute(
                    "SELECT item_id FROM external_item_sources"
                )
            }
            actions = [
                row[0]
                for row in connection.execute("SELECT action FROM audit_log")
            ]
        self.assertEqual(remaining, {"fresh-1"})
        self.assertEqual(sources, {"fresh-1"})
        self.assertIn("retention_cleanup", actions)

    def test_retention_setting_roundtrip_and_range(self):
        write_retention_setting(
            self.database, {"enabled": False, "days": 30},
            now=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            read_retention_setting(self.database), {"enabled": False, "days": 30}
        )
        with self.assertRaises(ValueError):
            write_retention_setting(self.database, {"days": 3})
        with self.assertRaises(ValueError):
            write_retention_setting(self.database, {"days": 9999})


class SettingsAndDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "yuanjian.db")
        self.database.initialize()
        self.backup_dir = Path(self.temp.name) / "backups"

    def tearDown(self):
        self.temp.cleanup()

    def test_learning_setting_defaults_on_and_roundtrips(self):
        self.assertEqual(read_learning_setting(self.database), {"enabled": True})
        service = SystemSettingsService(self.database)
        service.put_learning({"enabled": False})
        self.assertEqual(service.get_learning(), {"enabled": False})
        with self.assertRaises(ValueError):
            service.put_learning({})

    def test_diagnostics_snapshot_matches_flat_frontend_contract(self):
        backup = BackupService(self.database, self.backup_dir)
        backup.run()
        diagnostics = DiagnosticsService(
            self.database, backup_service=backup
        )
        payload = diagnostics.snapshot()
        for key in (
            "sources_enabled",
            "sources_total",
            "ai_enabled",
            "ai_jobs_today",
            "db_bytes",
            "last_backup",
            "backup_enabled",
            "last_run_ms",
            "runtime",
        ):
            self.assertIn(key, payload)
        self.assertGreater(payload["db_bytes"], 0)
        self.assertIsNotNone(payload["last_backup"])


class MobileExportTests(unittest.TestCase):
    def test_export_writes_self_contained_escaped_html(self):
        with tempfile.TemporaryDirectory() as temp:
            service = MobileExportService(
                Path(temp) / "mobile",
                now=lambda: datetime(2026, 8, 15, 9, 30, tzinfo=timezone.utc),
            )
            dashboard = {
                "state": "action",
                "summary": "今天有 1 件事需要处理",
                "counts": {"action": 1, "watch": 2},
                "coverage": {"enabled": 5, "healthy": 4},
                "items": [
                    {
                        "title": "<script>alert(1)</script>",
                        "risk_label": "高风险",
                        "decision_by": "2026-08-16",
                        "advice": "保留现金",
                    }
                ],
            }
            result = service.export(dashboard)

            self.assertIn("path", result)
            page = Path(result["path"]).read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn("https://", page)
            self.assertIn("远见 · 今日摘要", page)


class FeedbackLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "yuanjian.db")
        self.database.initialize()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO interest_objects(object_id, name, category, importance, privacy_level, status)"
                " VALUES ('I-1', '健康', 'health', 5, 'P1', 'active')"
            )
            connection.execute(
                "INSERT INTO event_clusters(cluster_id, title, first_seen_at, last_seen_at, evidence_hash, created_at, updated_at)"
                " VALUES ('C-1', '测试事件', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z', 'h1', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')"
            )
            connection.execute(
                "INSERT INTO judgments(judgment_id, cluster_id, provider, evidence_hash, content_json, created_at)"
                " VALUES ('J-1', 'C-1', 'local', 'h1', '{}', '2026-08-01T00:00:00Z')"
            )
            connection.execute(
                "INSERT INTO personal_impacts(impact_id, cluster_id, judgment_id, interest_id, impact_score, alert_level, components_json, reason, created_at, updated_at)"
                " VALUES ('P-1', 'C-1', 'J-1', 'I-1', 0.8, 'L3', '{}', '测试', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')"
            )
            connection.execute(
                "INSERT INTO event_cluster_items(cluster_id, item_id, similarity, merge_reason, source_domain, added_at)"
                " VALUES ('C-1', 'IT-1', 1.0, 'primary', 'example.com', '2026-08-01T00:00:00Z')"
            )
            connection.execute(
                "INSERT INTO external_sources(source_id, name, kind, endpoint, enabled, refresh_minutes, reliability_weight)"
                " VALUES ('S-EX', '示例源', 'rss', 'https://example.com/feed', 1, 60, 0.9)"
            )

    def tearDown(self):
        self.temp.cleanup()

    def _controller(self):
        from yuanjian_app.cognition import CognitionController

        return CognitionController(
            self.database, None, None, None, None, None, None,
            now=lambda: datetime(2026, 8, 15, tzinfo=timezone.utc),
        )

    def test_feedback_records_event_and_learning_consumes_it(self):
        controller = self._controller()
        controller.feedback("C-1", "false_positive")

        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT interest_category, source_domains_json, applied_json"
                " FROM feedback_events"
            ).fetchall()
        self.assertEqual(rows[0]["interest_category"], "health")
        self.assertEqual(
            json.loads(rows[0]["source_domains_json"]), ["example.com"]
        )
        self.assertEqual(rows[0]["applied_json"], "{}")

        applied = controller.apply_feedback_learning()
        self.assertEqual(applied, {"applied": 1})

        with self.database.connect() as connection:
            after = connection.execute(
                "SELECT applied_json FROM feedback_events"
            ).fetchone()
            penalties = json.loads(
                connection.execute(
                    "SELECT value_json FROM runtime_state"
                    " WHERE state_key='learning.category_penalties'"
                ).fetchone()["value_json"]
            )
            actions = [
                row[0] for row in connection.execute("SELECT action FROM audit_log")
            ]
        self.assertIn("example.com", json.loads(after["applied_json"])["source_weight"])
        self.assertAlmostEqual(penalties["health"], 0.95)
        self.assertIn("feedback_learning", actions)
        with self.database.connect() as connection:
            weight = connection.execute(
                "SELECT reliability_weight FROM external_sources WHERE source_id='S-EX'"
            ).fetchone()["reliability_weight"]
        self.assertAlmostEqual(weight, 0.85)

        # 再跑一遍：流水已消费，不重复降权。
        self.assertEqual(controller.apply_feedback_learning(), {"applied": 0})

    def test_mute_and_lower_importance_also_log_events(self):
        controller = self._controller()
        controller.feedback("C-1", "mute", {"hours": 24})
        controller.feedback("C-1", "lower_importance", {"importance": 2})
        controller.feedback("C-1", "false_positive")
        with self.database.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM feedback_events"
            ).fetchone()[0]
        self.assertEqual(count, 3)
        # 三条流水全部被消费（applied_json 回写），但只有 false_positive
        # 那条会产生源降权——源权重只降一档。
        self.assertEqual(controller.apply_feedback_learning(), {"applied": 3})
        with self.database.connect() as connection:
            weight = connection.execute(
                "SELECT reliability_weight FROM external_sources WHERE source_id='S-EX'"
            ).fetchone()["reliability_weight"]
        self.assertAlmostEqual(weight, 0.85)


class CalibrationSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "yuanjian.db")
        self.database.initialize()
        self.service = ForecastService(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_summary_reports_null_not_zero(self):
        summary = self.service.calibration_summary()
        self.assertEqual(summary["resolved_total"], 0)
        self.assertIsNone(summary["hit_rate"])
        self.assertIsNone(summary["false_positive_rate"])
        self.assertIsNone(summary["brier"])
        self.assertEqual(summary["brier_series"], [])
        self.assertEqual(summary["by_category"], {})

    def test_resolved_forecasts_feed_the_summary(self):
        data = {
            "forecast_id": "F-1",
            "title": "测试预测",
            "resolution_criteria": "可以核验",
            "window_start": "2026-08-01",
            "window_end": "2026-08-10",
            "probability": 0.80,
            "confidence": "medium",
            "alert_level": "L2",
            "privacy_level": "P2",
        }
        self.service.create_forecast({**data, "category": "finance"})
        self.service.resolve("F-1", "occurred", "2026-08-10", "发生")

        summary = self.service.calibration_summary()

        self.assertEqual(summary["resolved_total"], 1)
        self.assertEqual(summary["resolved_binary"], 1)
        self.assertEqual(summary["hit_rate"], 1.0)
        self.assertAlmostEqual(summary["brier"], 0.040000000000000036)
        self.assertEqual(summary["by_category"], {"finance": 1.0})


if __name__ == "__main__":
    unittest.main()
