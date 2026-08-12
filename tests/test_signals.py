import tempfile
import unittest
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.interests import InterestService
from yuanjian_app.signals import SignalService


class SignalServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "yuanjian.db")
        self.database.initialize()
        self.interests = InterestService(self.database)
        self.interests.ensure_defaults()
        self.service = SignalService(self.database, self.interests)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_urgent_high_cost_health_signal_becomes_l4_and_is_persisted(self):
        signal = self.service.ingest(
            "明天手术，预计自付12000元",
            "2026-08-06",
            source_type="manual",
            source_ref="user",
        )

        self.assertEqual(signal["alert_level"], "L4")
        self.assertEqual(signal["domains"], ["health", "cashflow"])
        self.assertEqual(len(signal["interest_ids"]), 2)
        self.assertIn("立即", signal["recommended_action"])
        self.assertEqual(self.service.list_signals()[0]["signal_id"], signal["signal_id"])

    def test_general_observation_stays_l1(self):
        signal = self.service.ingest("今天散步半小时", "2026-08-06")

        self.assertEqual(signal["alert_level"], "L1")
        self.assertEqual(signal["domains"], ["general"])
        self.assertEqual(signal["interest_ids"], [])

    def test_salary_arrival_is_a_low_risk_benefit_not_an_urgent_cash_warning(self):
        signal = self.service.ingest("今天工资到账 5000 元", "2026-08-12")

        self.assertEqual(signal["candidate"]["direction"], "benefit")
        self.assertEqual(signal["alert_level"], "L1")
        self.assertIn("现金缓冲", signal["recommended_action"])

    def test_missing_salary_is_not_mistaken_for_a_benefit(self):
        signal = self.service.ingest("今天工资仍未到账 5000 元", "2026-08-12")

        self.assertEqual(signal["candidate"]["direction"], "risk")
        self.assertEqual(signal["alert_level"], "L4")

    def test_empty_signal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不能为空"):
            self.service.ingest("  ", "2026-08-06")


if __name__ == "__main__":
    unittest.main()
