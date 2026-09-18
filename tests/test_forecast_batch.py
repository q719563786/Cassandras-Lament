"""v1.4 · S0（R-01 / R-02）：让「预测 → 结算 → 校准 → 修正」这条闭环第一次真正跑起来。

真库实测（2026-09-18，只读核查）：账本 **8,607 条预测、0 条结算**；逾期未结算
1,691 条；生成速度约 **391 条/天**。而结算 UI 是逐条的（每行一个下拉 + 一个日期 +
一个按钮），折算要结算全部需 25,821 次交互，跟上生成速度需 **1,174 次/天**。
**开环不是使用者的疏忽，是设计的必然结果 —— 这是吞吐量问题，不是纪律问题。**

本文件钉住三条不可妥协：

  1. **批量结算的默认结果必须是 `indeterminate`，绝不能用 `not_occurred`。**
     后者等于凭空给成百上千条命题盖上"没发生"的断言，而这些断言不是观察、是默认值，
     会直接进入 Brier 与命中率/误报率的分子分母，把校准彻底污染 ——
     与"机器填的概率混进人类命中率"是同一个病，只是更严重。
  2. **一次事务，任一条失败整批回滚**，不允许"结了 300 条、剩 700 条"。
  3. **到期自动归档默认关闭** —— 关闭时不存在任何自动结算路径（把时钟推后 400 天，
     `resolutions` 一行都不多）。
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.forecasts import ForecastService
from yuanjian_app.system_settings import (
    FORECAST_ARCHIVE_DEFAULT_DAYS,
    read_forecast_archive_setting,
    write_forecast_archive_setting,
)


NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def _card(forecast_id, window_end, category="finance", probability=0.65):
    start = (
        datetime.strptime(window_end, "%Y-%m-%d") - timedelta(days=30)
    ).date().isoformat()
    return {
        "forecast_id": forecast_id,
        "title": f"截至{window_end}：某泵类采购公告是否发布",
        "resolution_criteria": (
            f"截至 {window_end}，以主管部门公告为判定依据；"
            "公告中出现泵类采购条目即记为发生，否则记为未发生。"
        ),
        "observable_signals": "主管部门网站出现泵类采购公告",
        "window_start": start,
        "window_end": window_end,
        "probability": probability,
        "category": category,
        "confirmed_by": "user",
    }


class BatchResolveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.service = ForecastService(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def seed(self, count, *, window_end="2026-09-01", category="finance", prefix="F-B"):
        for index in range(count):
            self.service.create_forecast(
                _card(f"{prefix}-{index:02d}", window_end, category=category)
            )

    def count(self, table, where=""):
        with self.database.connect() as connection:
            return connection.execute(
                f"SELECT COUNT(*) FROM {table} {where}"
            ).fetchone()[0]

    # ------------------------------------------------------------------ #
    # R-01 主断言：默认 indeterminate，且不污染校准
    # ------------------------------------------------------------------ #
    def test_module_default_outcome_is_indeterminate(self):
        """默认结果只能是无害的 `indeterminate` —— 这条是常量级的护栏。"""
        self.assertEqual(ForecastService.BATCH_DEFAULT_OUTCOME, "indeterminate")
        self.assertNotEqual(ForecastService.BATCH_DEFAULT_OUTCOME, "not_occurred")

    def test_batch_resolve_writes_indeterminate_and_does_not_pollute_calibration(self):
        """造 20 条已到期预测，一次批量结算：
        - `resolutions` 新增 20 行且 outcome='indeterminate'、brier_score IS NULL；
        - `forecasts.status='resolved'`；
        - `calibration_summary()` 的 brier / hit_rate / false_positive_rate
          **保持 None**（证明没有把凭空断言灌进打分）。
        """
        self.seed(20)
        before = self.service.calibration_summary()

        result = self.service.batch_resolve()  # 不传 outcome → 用默认值

        self.assertEqual(result["outcome"], "indeterminate")
        self.assertEqual(result["target_count"], 20)
        self.assertEqual(result["resolved_count"], 20)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT outcome, brier_score FROM resolutions"
            ).fetchall()
            statuses = [
                row[0]
                for row in connection.execute("SELECT DISTINCT status FROM forecasts")
            ]
        self.assertEqual(len(rows), 20)
        self.assertEqual({row["outcome"] for row in rows}, {"indeterminate"})
        self.assertEqual({row["brier_score"] for row in rows}, {None})
        self.assertEqual(statuses, ["resolved"])

        after = self.service.calibration_summary()
        self.assertEqual(after["resolved_total"], 20)
        # 关键：一条都没进 Brier，也一条都没进命中率/误报率。
        self.assertIsNone(after["brier"])
        self.assertIsNone(after["hit_rate"])
        self.assertIsNone(after["false_positive_rate"])
        self.assertIsNone(before["brier"])
        self.assertEqual(after["by_source"]["user"]["brier"], None)
        self.assertEqual(after["by_source"]["user"]["hit_total"], 0)
        self.assertEqual(after["by_source"]["user"]["miss_total"], 0)

    def test_batch_records_resolved_by_batch_and_writes_one_audit_row(self):
        """溯源列 + 一次留痕：批量写出来的 indeterminate 必须与逐条判定的分得开。"""
        self.seed(3)

        self.service.batch_resolve(categories=["finance"], due_before="2026-09-10")

        with self.database.connect() as connection:
            resolved_by = {
                row[0]
                for row in connection.execute("SELECT DISTINCT resolved_by FROM forecasts")
            }
            audits = connection.execute(
                "SELECT details_json FROM audit_log WHERE action='forecast.batch_resolve'"
            ).fetchall()
        self.assertEqual(resolved_by, {"batch"})
        self.assertEqual(len(audits), 1, "整批只该留一条审计，不是每条一条")
        details = audits[0][0]
        for key in (
            "filters", "target_count", "resolved_count", "outcome", "started_at",
        ):
            self.assertIn(f'"{key}"', details)
        self.assertIn('"due_before": "2026-09-10"', details)
        self.assertIn('"trigger": "manual"', details)

    def test_single_resolve_records_resolved_by_user(self):
        """逐条判定走 `resolve()`，落 `resolved_by='user'` —— 与 batch 分得开。"""
        self.seed(1, prefix="F-ONE")
        self.service.resolve("F-ONE-00", "occurred", "2026-09-17", "人工判定")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT resolved_by, status FROM forecasts WHERE forecast_id='F-ONE-00'"
            ).fetchone()
        self.assertEqual(row["resolved_by"], "user")
        self.assertEqual(row["status"], "resolved")

    # ------------------------------------------------------------------ #
    # R-01 原子性
    # ------------------------------------------------------------------ #
    def test_batch_is_atomic_and_rolls_back_the_whole_batch(self):
        """人为让第 10 条失败（预先插一条 `resolutions` 制造主键冲突），
        断言**整批回滚**：`resolutions` 计数不变、没有任何 forecasts 被置为 resolved。
        """
        self.seed(20)
        victim = "F-B-10"
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO resolutions(forecast_id, outcome, resolved_at, probability,"
                " brier_score, category) VALUES (?,?,?,?,?,?)",
                (victim, "indeterminate", "2026-09-01", 0.65, None, "finance"),
            )
        before_rows = self.count("resolutions")
        self.assertEqual(before_rows, 1)

        with self.assertRaises(sqlite3.IntegrityError):
            self.service.batch_resolve()

        self.assertEqual(self.count("resolutions"), before_rows, "整批必须回滚")
        self.assertEqual(
            self.count("forecasts", "WHERE status='resolved'"), 0,
            "回滚之后不该有任何一条被标成已结算",
        )
        self.assertEqual(
            self.count("forecasts", "WHERE resolved_by!='unknown'"), 0,
            "回滚之后不该有任何一条留下 resolved_by 痕迹",
        )

    # ------------------------------------------------------------------ #
    # R-01 预览（只读）+ 筛选
    # ------------------------------------------------------------------ #
    def test_batch_targets_preview_is_read_only_and_reports_span(self):
        self.seed(2, window_end="2026-08-15", prefix="F-A")
        self.seed(3, window_end="2026-09-01", prefix="F-C")

        preview = self.service.batch_targets()

        self.assertEqual(preview["count"], 5)
        self.assertEqual(preview["window_end_min"], "2026-08-15")
        self.assertEqual(preview["window_end_max"], "2026-09-01")
        self.assertEqual(preview["default_outcome"], "indeterminate")
        # 只读：预览不改任何状态。
        self.assertEqual(self.count("resolutions"), 0)
        self.assertEqual(self.count("forecasts", "WHERE status='resolved'"), 0)

    def test_batch_filters_by_category_and_due_before(self):
        self.seed(2, window_end="2026-08-15", category="finance", prefix="F-F")
        self.seed(2, window_end="2026-08-15", category="policy", prefix="F-P")
        self.seed(1, window_end="2026-09-10", category="finance", prefix="F-L")

        preview = self.service.batch_targets(categories=["finance"], due_before="2026-09-01")

        self.assertEqual(preview["count"], 2, "类别与到期区间必须同时生效")
        result = self.service.batch_resolve(
            categories=["finance"], due_before="2026-09-01"
        )
        self.assertEqual(result["resolved_count"], 2)
        self.assertEqual(self.count("forecasts", "WHERE status='open'"), 3)

    def test_batch_skips_already_resolved_rows(self):
        """`status` 默认只看 'open'：已结算的不会被二次写入（resolutions 是主键）。"""
        self.seed(2)
        self.service.resolve("F-B-00", "occurred", "2026-09-17", "先人工结一条")
        result = self.service.batch_resolve()
        self.assertEqual(result["resolved_count"], 1)
        self.assertEqual(self.count("resolutions"), 2)


class AutoArchiveRuleTests(unittest.TestCase):
    """R-02：到期不结算的最终去向 —— **默认关闭**的自动归档规则。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.service = ForecastService(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def count(self, table, where=""):
        with self.database.connect() as connection:
            return connection.execute(
                f"SELECT COUNT(*) FROM {table} {where}"
            ).fetchone()[0]

    def seed(self, count, window_end):
        for index in range(count):
            self.service.create_forecast(_card(f"F-AR-{index:02d}", window_end))

    def test_setting_defaults_to_disabled_with_thirty_days(self):
        setting = read_forecast_archive_setting(self.database)
        self.assertFalse(setting["enabled"], "自动写不可变账本必须是用户明确选择的行为")
        self.assertEqual(setting["days"], FORECAST_ARCHIVE_DEFAULT_DAYS)

    def test_setting_rejects_out_of_range_days(self):
        for days in (6, 181, "x"):
            with self.subTest(days=days):
                with self.assertRaises(ValueError):
                    write_forecast_archive_setting(
                        self.database, {"enabled": True, "days": days}
                    )

    def test_disabled_rule_has_no_auto_settlement_path_at_all(self):
        """开关关闭时把时钟推后 400 天：`resolutions` 计数必须不变。

        这是"默认关闭"的**机制证明**，不是文案证明 —— 只要存在任何一条自动结算
        路径，这个断言就会红。
        """
        self.seed(5, "2026-01-01")
        self.assertEqual(self.count("resolutions"), 0)

        outcome = self.service.auto_archive_overdue(
            now=NOW + timedelta(days=400)
        )

        self.assertFalse(outcome["enabled"])
        self.assertEqual(outcome["archived"], 0)
        self.assertEqual(self.count("resolutions"), 0)
        self.assertEqual(self.count("forecasts", "WHERE status='resolved'"), 0)

    def test_enabled_rule_archives_only_rows_past_the_window(self):
        write_forecast_archive_setting(
            self.database, {"enabled": True, "days": 30}
        )
        # 相对 NOW=2026-09-18：到期日 2026-08-01 已过 48 天（>=30）→ 归档；
        # 2026-09-10 只过 8 天（<30）→ 不动。
        self.seed(3, "2026-08-01")
        for index in range(2):
            self.service.create_forecast(
                _card(f"F-RECENT-{index:02d}", "2026-09-10")
            )

        outcome = self.service.auto_archive_overdue(now=NOW)

        self.assertTrue(outcome["enabled"])
        self.assertEqual(outcome["days"], 30)
        self.assertEqual(outcome["archived"], 3)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT outcome, brier_score FROM resolutions"
            ).fetchall()
            audits = connection.execute(
                "SELECT details_json FROM audit_log WHERE action='forecast.batch_resolve'"
            ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["outcome"] for row in rows}, {"indeterminate"})
        self.assertEqual({row["brier_score"] for row in rows}, {None})
        self.assertEqual(len(audits), 1)
        self.assertIn('"trigger": "auto_archive"', audits[0][0])
        self.assertIn('"resolved_by": "batch"', audits[0][0])
        self.assertEqual(self.count("forecasts", "WHERE status='open'"), 2)
        # 自动归档同样不进校准打分。
        summary = self.service.calibration_summary()
        self.assertIsNone(summary["brier"])
        self.assertEqual(summary["resolved_by_counts"]["batch"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
