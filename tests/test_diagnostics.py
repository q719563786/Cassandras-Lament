"""诊断面板「远程研判还活着吗」四字段（v1.5.2）。

缺陷现场：`diag.js` 的四态状态条早就写好并在防御式读取 `ai_paused` /
`ai_pause_reason` / `ai_fallback_local` / `ai_fallback_reason`，但后端一个都没暴露
⇒ 「已暂停」「已回退本机」两态**永远不会显示**。而远程失效时应用会静默降级到
本机研判、界面看起来一切正常 —— 这正是"安静地坏掉"。

这里按四种情形分别断言取值：正常 / 鉴权暂停 / 熔断 / 本轮有回退。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.diagnostics import DiagnosticsService


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class RemoteStateFieldsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.service = DiagnosticsService(self.database)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    # ---- 造数据 ----------------------------------------------------------
    def _job(self, job_id, status, *, last_error="", finished_at=None):
        stamp = _iso(finished_at or self.now)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO judgment_jobs(
                    job_id,cluster_id,evidence_hash,provider,model,status,
                    attempts,request_chars,created_at,next_attempt_at,finished_at,last_error
                ) VALUES (?,?,?,'remote','m',?,1,0,?,?,?,?)
                """,
                (job_id, f"C-{job_id}", f"H-{job_id}", status, stamp, stamp, stamp, last_error),
            )

    def _cognition_round(self, *, age_minutes=1):
        """写一条 `task.cognition` 轮记录 —— 回退判定就绑在这个窗口上。"""
        started = self.now - timedelta(minutes=age_minutes)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_state(state_key,value_json,updated_at)
                VALUES ('task.cognition',?,?)
                """,
                (
                    json.dumps(
                        {
                            "status": "ok",
                            "started_at": _iso(started),
                            "finished_at": _iso(self.now),
                        },
                        sort_keys=True,
                    ),
                    _iso(self.now),
                ),
            )

    def _assert_reason_present(self, payload, flag, reason):
        """语义不变量：标志为真 ⇒ 原因非空；为假 ⇒ 原因留空。"""
        self.assertIsInstance(payload[flag], bool)
        self.assertIsInstance(payload[reason], str)
        if payload[flag]:
            self.assertTrue(payload[reason].strip(), f"{flag} 为真但 {reason} 是空的")
        else:
            self.assertEqual(payload[reason], "", f"{flag} 为假但 {reason} 非空")

    # ---- 情形 1：正常 ----------------------------------------------------
    def test_healthy_queue_reports_neither_paused_nor_fallback(self):
        payload = self.service.snapshot()

        self.assertFalse(payload["ai_paused"])
        self.assertFalse(payload["ai_fallback_local"])
        self._assert_reason_present(payload, "ai_paused", "ai_pause_reason")
        self._assert_reason_present(payload, "ai_fallback_local", "ai_fallback_reason")

    # ---- 情形 2：鉴权暂停 ------------------------------------------------
    def test_auth_paused_queue_reports_paused_with_a_human_reason(self):
        self._job("J-auth", "paused_auth", last_error="auth")
        self._job("J-other", "paused_auth", last_error="auth_paused")

        payload = self.service.snapshot()

        self.assertTrue(payload["ai_paused"])
        self.assertTrue(payload["ai_pause_reason"].strip())
        self.assertFalse(payload["ai_fallback_local"])
        # 人话，不是内部状态名
        for leaked in ("paused_auth", "auth_paused", "circuit_open", "status"):
            self.assertNotIn(leaked, payload["ai_pause_reason"])
        # 必须点出用户下一步要做什么（重填密钥）——只写"已暂停"等于让用户干瞪眼
        self.assertIn("密钥", payload["ai_pause_reason"])
        self._assert_reason_present(payload, "ai_paused", "ai_pause_reason")

    # ---- 情形 3：熔断 ----------------------------------------------------
    def test_open_circuit_reports_paused_with_a_circuit_specific_reason(self):
        self._job("J-circuit", "paused_auth", last_error="circuit_open")

        payload = self.service.snapshot()

        self.assertTrue(payload["ai_paused"])
        self.assertTrue(payload["ai_pause_reason"].strip())
        self.assertNotIn("circuit_open", payload["ai_pause_reason"])
        # 熔断的成因是"连续失败、在烧钱"，不是"密钥错了"：文案必须说清这一条，
        # 否则用户会去瞎改密钥（而熔断压根不需要他动手）。
        self.assertIn("费用", payload["ai_pause_reason"])
        self.assertNotIn("密钥", payload["ai_pause_reason"])
        # 熔断与鉴权是两种成因，文案必须区分开（用户该做什么不一样）
        self._job("J-auth2", "paused_auth", last_error="auth")
        self.assertNotEqual(
            self.service.snapshot()["ai_pause_reason"], payload["ai_pause_reason"]
        )
        self._assert_reason_present(payload, "ai_paused", "ai_pause_reason")

    # ---- 情形 4：本轮有回退 ----------------------------------------------
    def test_fallback_within_the_current_round_is_reported_with_a_human_reason(self):
        self._cognition_round(age_minutes=1)
        self._job(
            "J-fallback",
            "remote_error_fallback_local",
            last_error="timeout",
            finished_at=self.now - timedelta(seconds=10),
        )

        payload = self.service.snapshot()

        self.assertTrue(payload["ai_fallback_local"])
        self.assertTrue(payload["ai_fallback_reason"].strip())
        # 人话：`timeout` 要说成"连接超时"，而不是把 kind 原样丢给用户
        self.assertIn("超时", payload["ai_fallback_reason"])
        self.assertFalse(payload["ai_paused"])
        for leaked in ("remote_error_fallback_local", "timeout", "last_error"):
            self.assertNotIn(leaked, payload["ai_fallback_reason"])
        self._assert_reason_present(payload, "ai_fallback_local", "ai_fallback_reason")

    def test_fallback_from_a_previous_round_is_not_sticky(self):
        """反向对照：上一轮（窗口之前）的回退必须自己消失。

        不自愈的告警等于噪声：几天前一次连接超时会永远挂在界面上，
        用户以为现在还是坏的。
        """
        self._cognition_round(age_minutes=1)
        self._job(
            "J-old",
            "remote_error_fallback_local",
            last_error="network",
            finished_at=self.now - timedelta(hours=3),
        )

        payload = self.service.snapshot()

        self.assertFalse(payload["ai_fallback_local"])
        self.assertEqual(payload["ai_fallback_reason"], "")

    def test_unknown_fallback_kind_still_yields_a_non_empty_reason(self):
        """kind 是开放集合：将来新增一类失败，也不能回退成空字符串。"""
        self._cognition_round(age_minutes=1)
        self._job(
            "J-weird",
            "remote_error_fallback_local",
            last_error="something_new",
            finished_at=self.now,
        )

        payload = self.service.snapshot()

        self.assertTrue(payload["ai_fallback_local"])
        self.assertTrue(payload["ai_fallback_reason"].strip())

    # ---- 契约：四个键必须始终在场 ----------------------------------------
    def test_all_four_keys_are_always_present(self):
        payload = self.service.snapshot()

        for key in (
            "ai_paused",
            "ai_pause_reason",
            "ai_fallback_local",
            "ai_fallback_reason",
        ):
            self.assertIn(key, payload, f"诊断数据缺少 {key}：前端那一态永远不会显示")


if __name__ == "__main__":
    unittest.main()
