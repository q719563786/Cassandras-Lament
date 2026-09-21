import tempfile
import unittest
from datetime import datetime
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

    def test_blank_occurred_at_falls_back_to_a_real_timestamp(self):
        """`occurred_at` 是时间语义列：界面不带该字段时必须落一个真时间，不能落空串。

        缺陷现场：`POST /api/events` 不带 `occurred_at` 时，
        `payload.get("occurred_at", "")` 给出空串，写入端原样落库 ——
        真库里已经有 1 行 `occurred_at=''`。空串没有时间形状，任何字符串比较/
        排序/`MAX()` 都会把它排在一切真实时间之前，形状护栏也识别不出它。
        """
        signal = self.service.ingest("今天散步半小时", "")

        self.assertTrue(signal["occurred_at"], "空串又落库了")
        moment = datetime.fromisoformat(signal["occurred_at"])
        self.assertIsNotNone(moment.tzinfo, "落的时间必须带时区，否则跨时区不可比")
        # 与 received_at 同源（"最迟此刻已发生"），形状也该一致
        self.assertEqual(signal["occurred_at"][:10], signal["received_at"][:10])

    def test_whitespace_only_occurred_at_is_also_replaced(self):
        signal = self.service.ingest("今天散步半小时", "   ")

        self.assertTrue(signal["occurred_at"].strip())


if __name__ == "__main__":
    unittest.main()
