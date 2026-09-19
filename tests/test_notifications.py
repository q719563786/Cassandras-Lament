import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.notifications import NotificationService, _toast_invocation


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 11, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.clock = MutableClock()
        self.delivered = []
        self.service = NotificationService(
            self.database,
            notifier=lambda title, body: self.delivered.append((title, body)),
            now=self.clock,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def impact(
        self,
        level="L3",
        evidence_hash="hash-1",
        window=24,
        evidence_level="E1",
        independent_domains=1,
    ):
        return {
            "impact_id": "P-1",
            "cluster_id": "C-1",
            "alert_level": level,
            "evidence_hash": evidence_hash,
            "action_window_hours": window,
            "evidence_level": evidence_level,
            "independent_domains": independent_domains,
            "interest_name": "不应出现在系统通知的私人利益",
        }

    def test_same_cluster_only_material_upgrades_break_through(self):
        # 首次：无历史 → 建立。
        self.assertEqual(
            self.service.consider(self.impact(), "首次")["status"], "created"
        )
        # 6 小时内、同等级、只改 evidence_hash → 必须抑制（本次修的回归点）。
        self.assertEqual(
            self.service.consider(
                self.impact(evidence_hash="hash-2"), "只有哈希变了"
            )["status"],
            "suppressed",
        )
        # 6 小时内升到 L4 → 等级升高可以突破硬下限。
        self.assertEqual(
            self.service.consider(
                self.impact(level="L4", evidence_hash="hash-3"), "等级上升"
            )["status"],
            "created",
        )
        # 越过 6 小时，证据等级由 E1 升到 E3 → 实质提升，放行。
        self.clock.value += timedelta(hours=6, seconds=1)
        self.assertEqual(
            self.service.consider(
                self.impact(level="L4", evidence_hash="hash-4", evidence_level="E3"),
                "证据提升",
            )["status"],
            "created",
        )
        # 再过 6 小时，无实质变化（只改哈希）→ 抑制。
        self.clock.value += timedelta(hours=6, seconds=1)
        self.assertEqual(
            self.service.consider(
                self.impact(level="L4", evidence_hash="hash-5", evidence_level="E3"),
                "又只有哈希变了",
            )["status"],
            "suppressed",
        )
        # 超过 24 小时的实质比较窗口 → 允许重新提醒一次。
        self.clock.value += timedelta(hours=24, seconds=1)
        self.assertEqual(
            self.service.consider(
                self.impact(level="L4", evidence_hash="hash-6", evidence_level="E3"),
                "窗口外重提",
            )["status"],
            "created",
        )

    def test_new_independent_sources_break_through_after_hard_floor(self):
        self.assertEqual(
            self.service.consider(
                self.impact(independent_domains=1), "首次"
            )["status"],
            "created",
        )
        # 6 小时内独立域名 1→3：实质提升也被硬下限挡住。
        self.assertEqual(
            self.service.consider(
                self.impact(independent_domains=3), "六小时内来源变多"
            )["status"],
            "suppressed",
        )
        # 越过 6 小时再评估：来源增长成为放行理由。
        self.clock.value += timedelta(hours=6, seconds=1)
        self.assertEqual(
            self.service.consider(
                self.impact(independent_domains=3), "六小时后来源变多"
            )["status"],
            "created",
        )

    def test_window_tightening_only_fires_on_bucket_change(self):
        self.assertEqual(
            self.service.consider(self.impact(window=720), "窗口 30 天")["status"],
            "created",
        )
        self.clock.value += timedelta(hours=6, seconds=1)
        # 720 → 696 同档（都 >168），不触发。
        self.assertEqual(
            self.service.consider(self.impact(window=696), "窗口 29 天")["status"],
            "suppressed",
        )
        # 720 → 168 跨到更紧的档位，触发。
        self.assertEqual(
            self.service.consider(self.impact(window=168), "窗口 7 天")["status"],
            "created",
        )

    def test_daily_toast_budget_caps_windows_popups(self):
        results = [
            self.service.consider(
                {
                    **self.impact(level="L4"),
                    "cluster_id": f"C-{i}",
                    "impact_id": f"P-{i}",
                },
                f"事件 {i}",
            )
            for i in range(1, 8)
        ]

        self.assertEqual([item["delivery"] for item in results[:6]], ["windows"] * 6)
        self.assertEqual(len(self.delivered), 6)
        self.assertEqual(results[6]["delivery"], "local_only")
        self.assertEqual(results[6]["status"], "created")
        # "不丢信息"：7 条全部进了本地通知中心，且配额用尽那条也仍是 unread。
        unread = self.service.list_page(status="unread")["items"]
        self.assertEqual(len(unread), 7)
        self.assertIn("C-7", {item["cluster_id"] for item in unread})

        # 预算按滚动 24 小时窗口恢复。
        self.clock.value += timedelta(hours=24, seconds=1)
        recovered = self.service.consider(
            {
                **self.impact(level="L4"),
                "cluster_id": "C-8",
                "impact_id": "P-8",
            },
            "新的一天",
        )
        self.assertEqual(recovered["delivery"], "windows")

    def test_daily_toast_budget_does_not_affect_digest_or_local_levels(self):
        for i in range(1, 8):
            self.service.consider(
                {
                    **self.impact(level="L4"),
                    "cluster_id": f"C-{i}",
                    "impact_id": f"P-{i}",
                },
                f"事件 {i}",
            )
        # 预算已耗尽：只有 6 条真的弹了窗。
        self.assertEqual(len(self.delivered), 6)

        local = self.service.consider(
            {**self.impact(level="L3"), "cluster_id": "C-L3", "impact_id": "P-L3"},
            "本地事件",
        )
        digest = self.service.consider(
            {**self.impact(level="L2"), "cluster_id": "C-L2", "impact_id": "P-L2"},
            "汇总",
        )

        self.assertEqual(local["delivery"], "local_only")
        self.assertEqual(local["status"], "created")
        self.assertEqual(digest["delivery"], "daily_digest")
        self.assertEqual(digest["status"], "created")
        # 两级本来就不走弹窗：落库状态分别为 unread / digest，未被预算误抑制。
        self.assertEqual(self.service.list_page(status="unread")["total"], 8)
        self.assertEqual(self.service.list_page(status="digest")["total"], 1)

    def test_toast_invocation_passes_text_by_environment_not_argv(self):
        argv, env = _toast_invocation("标题甲", "正文乙")

        self.assertEqual(len(argv), 7)
        self.assertEqual(
            argv[:5],
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
            ],
        )
        self.assertEqual(argv[5], "-Command")
        script = argv[-1]
        self.assertIn("CreateToastNotifier('远见')", script)
        self.assertIn("$env:YUANJIAN_TOAST_TITLE", script)
        self.assertIn("$env:YUANJIAN_TOAST_BODY", script)
        self.assertNotIn("$args[", script)
        self.assertFalse(any("标题甲" in part for part in argv))
        self.assertFalse(any("正文乙" in part for part in argv))
        self.assertEqual(env["YUANJIAN_TOAST_TITLE"], "标题甲")
        self.assertEqual(env["YUANJIAN_TOAST_BODY"], "正文乙")

    def test_l1_is_ignored_l2_is_digest_and_l4_system_text_is_generic(self):
        self.assertEqual(self.service.consider(self.impact("L1"), "低等级")["status"], "ignored")
        digest = self.service.consider(self.impact("L2"), "每日汇总")
        urgent = self.service.consider(self.impact("L4", "hash-2"), "私人医疗债务细节")

        self.assertEqual(digest["delivery"], "daily_digest")
        self.assertEqual(urgent["delivery"], "windows")
        system_text = " ".join(self.delivered[0])
        self.assertNotIn("私人", system_text)
        self.assertNotIn("医疗债务", system_text)

    def test_windows_failure_falls_back_to_readable_local_center(self):
        service = NotificationService(
            self.database,
            notifier=lambda title, body: (_ for _ in ()).throw(RuntimeError("toast failed")),
            now=self.clock,
        )

        result = service.consider(self.impact("L4"), "仍需保留")
        notifications = service.list_notifications()

        self.assertEqual(result["delivery"], "local_only")
        self.assertEqual(notifications[0]["reason"], "仍需保留")
        self.assertEqual(notifications[0]["status"], "unread")
        marked = service.mark_read(notifications[0]["notification_id"])
        self.assertEqual(marked["status"], "read")

    def test_notification_page_filters_and_mark_all_read_is_idempotent(self):
        first = self.service.consider(self.impact(), "第一条")
        second_impact = {**self.impact(evidence_hash="hash-2"), "cluster_id": "C-2", "impact_id": "P-2"}
        self.service.consider(second_impact, "第二条")
        self.service.mark_read(first["notification_id"])

        unread = self.service.list_page(limit=1, offset=0, status="unread")

        self.assertEqual(unread["total"], 1)
        self.assertEqual(unread["items"][0]["reason"], "第二条")
        self.assertEqual(self.service.mark_all_read()["updated"], 1)
        self.assertEqual(self.service.mark_all_read()["updated"], 0)
        self.assertEqual(self.service.list_page(status="unread")["total"], 0)
        with self.assertRaisesRegex(ValueError, "通知状态"):
            self.service.list_page(status="unknown")


if __name__ == "__main__":
    unittest.main()
