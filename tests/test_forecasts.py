import hashlib
import tempfile
import unittest
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.forecasts import ForecastService


def card(probability, title="测试预测"):
    return f"""---
forecast_id: F-1
created_at: 2026-08-06T13:00:00+08:00
status: open
title: {title}
resolution_criteria: 到期可以判断
window_start: 2026-08-06
window_end: 2026-08-16
probability: {probability:.2f}
confidence: medium
alert_level: L3
next_review_at: 2026-08-08
model_version: v0.1
privacy_level: P2
---
## 因果链
事件到结果。
## 支持证据
- 支持。
## 反对证据
- 反对。
## 替代假设
- 替代。
## 反证条件
- 未发生。
## 建议行动
- 观察。
"""


class ForecastServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "yuanjian.db")
        self.database.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_version(self, probability, version):
        content = card(probability)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO forecasts(forecast_id, status, window_end, category) VALUES ('F-1', 'open', '2026-08-16', 'finance')"
            )
            connection.execute(
                "INSERT INTO forecast_versions VALUES (?, ?, ?, ?, ?)",
                (
                    "F-1",
                    version,
                    probability,
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    content,
                ),
            )

    def test_list_forecasts_uses_latest_immutable_version(self):
        self.add_version(0.65, 1)
        self.add_version(0.80, 2)

        items, total = ForecastService(self.database).list_forecasts()

        self.assertEqual(total, 1)
        self.assertEqual(items[0]["probability"], 0.80)
        self.assertEqual(items[0]["version"], 2)
        self.assertEqual(items[0]["title"], "测试预测")
        self.assertEqual(items[0]["category"], "finance")

    def test_resolve_binary_records_brier_and_audit(self):
        self.add_version(0.65, 1)

        result = ForecastService(self.database).resolve(
            "F-1", "occurred", "2026-08-16", "实际发生"
        )

        self.assertEqual(result["brier_score"], 0.1225)
        with self.database.connect() as connection:
            actions = [
                row[0]
                for row in connection.execute(
                    "SELECT action FROM audit_log ORDER BY audit_id"
                ).fetchall()
            ]
        self.assertEqual(actions, ["forecast.resolve"])

    def valid_data(self, **changes):
        data = {
            "forecast_id": "F-NEW-1",
            "title": "月底前收到工资",
            "resolution_criteria": "工资到账记录可核验",
            "window_start": "2026-08-06",
            "window_end": "2026-08-31",
            "probability": 0.80,
            "confidence": "medium",
            "alert_level": "L2",
            "privacy_level": "P2",
        }
        data.update(changes)
        return data

    def test_create_forecast_records_version_one(self):
        result = ForecastService(self.database).create_forecast(self.valid_data())

        self.assertEqual(result["forecast_id"], "F-NEW-1")
        self.assertEqual(result["version"], 1)
        item = ForecastService(self.database).get_forecast("F-NEW-1")
        self.assertEqual(item["title"], "月底前收到工资")
        self.assertEqual(item["probability"], 0.80)

    def test_create_forecast_rejects_invalid_core_fields(self):
        service = ForecastService(self.database)
        invalid_cases = (
            (self.valid_data(probability=0.77), "概率"),
            (self.valid_data(title="  "), "标题"),
            (self.valid_data(window_start="2026-09-01"), "日期"),
            (self.valid_data(resolution_criteria=""), "结算标准"),
        )

        for data, message in invalid_cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    service.create_forecast(data)

    def test_add_version_is_immutable_and_deduplicates_identical_content(self):
        service = ForecastService(self.database)
        service.create_forecast(self.valid_data())

        changed = service.add_version(
            "F-NEW-1", self.valid_data(probability=0.90, title="月底前大概率收到工资")
        )
        duplicate = service.add_version(
            "F-NEW-1", self.valid_data(probability=0.90, title="月底前大概率收到工资")
        )

        self.assertEqual(changed["version"], 2)
        self.assertFalse(changed["duplicate"])
        self.assertEqual(duplicate["version"], 2)
        self.assertTrue(duplicate["duplicate"])
        detail = service.get_forecast("F-NEW-1")
        self.assertEqual([row["version"] for row in detail["versions"]], [1, 2])

    def test_add_version_preserves_unsubmitted_reasoning_sections(self):
        service = ForecastService(self.database)
        service.create_forecast(
            self.valid_data(
                causal_chain="收入确认后改善现金流。",
                opposing_evidence="发薪流程可能延迟。",
            )
        )

        service.add_version("F-NEW-1", self.valid_data(probability=0.90))

        latest = service.get_forecast("F-NEW-1")["versions"][-1]["content"]
        self.assertIn("收入确认后改善现金流。", latest)
        self.assertIn("发薪流程可能延迟。", latest)


if __name__ == "__main__":
    unittest.main()
