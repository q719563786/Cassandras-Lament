"""设计审计第一波断言：证伪性闸 / 旧条目标记与 Brier 排除 / L4 改人工 / 趋势护栏。

只动 tests/**。所有断言针对「用户决策后的目标行为」，而非当前 src 的临时状态。
- 已落地的行为写普通断言（应绿）；
- 架构师尚未落地的目标行为用 @unittest.expectedFailure / @unittest.skip 占位，
  并附精确证据，便于落地后转真实断言。

变异对照见 build-artifacts/qa_calibration_mutations.py（拷贝 src 到临时目录，
绝不改真实 src）。
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.forecasts import ForecastService
from yuanjian_app.impacts import ImpactService
from yuanjian_app.interests import InterestService


TS = "2026-08-11T08:00:00Z"


def _valid_candidate(*, vague=False):
    """构造一条 confirm_candidate 可消费的候选预测 JSON。

    vague=True：缺 observable_signals 且含禁用虚词「产生实际影响」，
    应被证伪性闸（_settlement_ready 四要素）拦截，不进不可变账本。
    非 vague：四要素齐备（可核验日期 / 可观测条件 / 判定依据 / 无虚词），应通过闸。
    """
    if vague:
        return {
            "title": "医保政策调整将影响本地自付成本",
            "resolution_criteria": "判断该事件是否对本地利益产生实际影响",
            "window_start": "2026-08-11",
            "window_end": "2026-09-15",
            "probability_low": 0.55,
            "probability_high": 0.78,
            "causal_chain": "政策变化 -> 自付成本变化",
            "supporting_evidence": "S-1",
            "opposing_evidence": "执行细则待公布",
            "falsification": "",
            "recommended_action": "请在校准面板确认概率后记录为正式预测",
        }
    return {
        "title": "截至2026-09-15：本地自付成本是否因医保政策调整受到可观测影响",
        "resolution_criteria": (
            "判定依据：截至 2026-09-15，以官方公布的报销比例下调文件为准，"
            "对照可观测事实核验；任一事实在该日期前发生即记为发生。"
            "核验以官方公告、主管部门文件及本地可核验记录为准。"
        ),
        "window_start": "2026-08-11",
        "window_end": "2026-09-15",
        "probability_low": 0.55,
        "probability_high": 0.78,
        "observable_signals": "医保局官网公布报销比例下调文件；定点医院结算系统更新",
        "causal_chain": "政策变化 -> 自付成本变化",
        "supporting_evidence": "S-1",
        "opposing_evidence": "执行细则待公布",
        "falsification": "官方公布报销比例上调或不变",
        "recommended_action": "请在校准面板确认概率后记录为正式预测",
    }


class CalibrationAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.interests = InterestService(self.database)
        self.forecasts = ForecastService(self.database)
        self.health = self.interests.create_object(
            {
                "name": "本地私人健康利益",
                "category": "health",
                "importance": 5,
                "privacy_level": "P3",
            }
        )
        self.service = ImpactService(
            self.database,
            self.interests,
            self.forecasts,
            now=lambda: datetime(2026, 8, 11, 8, tzinfo=timezone.utc),
        )

    def tearDown(self):
        self.temporary.cleanup()

    # ------------------------------------------------------------------ #
    # 复用 helpers：直接插入 event_clusters + personal_impacts，绕过
    # map_judgment 的内联自动确认，从而把 auto_confirm_all 的行为单独隔离出来测。
    # ------------------------------------------------------------------ #
    def _insert_cluster(self, cluster_id, evidence_level):
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO event_clusters(
                    cluster_id,title,summary,first_seen_at,last_seen_at,
                    evidence_level,evidence_hash,categories_json,
                    latest_judgment_id,created_at,updated_at
                ) VALUES (?,?, '',?,?,?,?,?,?,?,?)
                """,
                (
                    cluster_id,
                    "医保政策调整",
                    TS,
                    TS,
                    evidence_level,
                    f"hash-{cluster_id}",
                    '["health"]',
                    f"J-{cluster_id}",
                    TS,
                    TS,
                ),
            )

    def _insert_impact(
        self, impact_id, cluster_id, alert_level, candidate, user_label=""
    ):
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO personal_impacts(
                    impact_id,cluster_id,judgment_id,interest_id,impact_score,
                    alert_level,components_json,reason,candidate_json,
                    user_label,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    impact_id,
                    cluster_id,
                    f"J-{cluster_id}",
                    self.health["object_id"],
                    0.5,
                    alert_level,
                    '{"confidence":0.5}',
                    "审计构造",
                    json.dumps(candidate, ensure_ascii=False, sort_keys=True),
                    user_label,
                    TS,
                    TS,
                ),
            )

    def _forecast_count(self):
        _, total = self.forecasts.list_forecasts()
        return total

    def add_judgment(self, suffix, evidence_level, confidence=0.9, urgent=True):
        """复用 test_impacts 的真实构造逻辑，生成 event_clusters + judgments。

        直接内联，避免跨模块导入（tests/ 不是包，无 __init__.py）。
        """
        cluster_id = f"C-{suffix}"
        judgment_id = f"J-{suffix}"
        timestamp = "2026-08-11T08:00:00Z"
        result = {
            "fact_summary": "医保政策可能改变自付成本",
            "actors": ["主管部门"],
            "causal_chain": ["政策变化", "自付成本变化"],
            "uncertainties": ["执行细则待公布"],
            "horizons": ["未来7天" if urgent else "未来90天"],
            "probability_low": 0.55,
            "probability_high": 0.78,
            "confidence": confidence,
            "supporting_source_ids": ["S-1"],
            "counter_source_ids": [],
            "up_triggers": ["正式生效"],
            "down_triggers": ["延期"],
            "impact_categories": ["health"],
            "gyw": {
                "stakeholders": "推动方：医保局；阻力方：财政、地方执行",
                "constraints": "资源约束：医保基金、财政补贴",
                "least_resistance_path": "最小阻力路径：试点城市先行",
                "counter_evidence": "反对证据：基金穿底风险",
                "leading_indicators": "领先指标：试点城市名单",
            },
        }
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO event_clusters(
                    cluster_id,title,summary,first_seen_at,last_seen_at,
                    evidence_level,evidence_hash,categories_json,
                    latest_judgment_id,created_at,updated_at
                ) VALUES (?,?, '',?,?,?,?,?,?,?,?)
                """,
                (
                    cluster_id,
                    "医保政策调整",
                    timestamp,
                    timestamp,
                    evidence_level,
                    f"hash-{suffix}",
                    '["health","policy"]',
                    judgment_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO judgments VALUES (?,?,?,?,?,?)",
                (
                    judgment_id,
                    cluster_id,
                    "local",
                    f"hash-{suffix}",
                    json.dumps(result, ensure_ascii=False),
                    timestamp,
                ),
            )
        return cluster_id, judgment_id

    def _confirmed_forecast_id_of(self, impact_id):
        """confirmed_forecast_id 存在 personal_impacts.candidate_json 内部（JSON），
        不是表列；从 JSON 解析。"""
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT candidate_json FROM personal_impacts WHERE impact_id=?",
                (impact_id,),
            ).fetchone()
        if not row:
            return None
        cand = json.loads(row["candidate_json"] or "{}")
        return cand.get("confirmed_forecast_id")

    def _is_confirmed(self, impact_id):
        return self._confirmed_forecast_id_of(impact_id) is not None

    def _confirmed_by_of(self, impact_id):
        fid = self._confirmed_forecast_id_of(impact_id)
        if not fid:
            return None
        forecasts, _ = self.forecasts.list_forecasts()
        for f in forecasts:
            if f["forecast_id"] == fid:
                return f["confirmed_by"]
        return None

    # ================================================================== #
    # C · L4 改人工（部分落地）
    # ================================================================== #
    def test_c_e1_single_source_never_auto_confirmed(self):
        """C 已落地：证据闸 —— 单来源(E1)无论多重要都不得自动进账本。

        即使 alert_level 是 L3（E1 在 map_judgment 里已被压到 L3 封顶），
        auto_confirm_all 仍因 evidence_level 不在 (E2,E3,E4) 而跳过它。
        """
        self._insert_cluster("C-E1", "E1")
        self._insert_impact(
            "P-E1", "C-E1", "L3", _valid_candidate()
        )
        self.service.auto_confirm_all()
        self.assertEqual(self._forecast_count(), 0)
        self.assertFalse(self._is_confirmed("P-E1"))

    def test_c_l3_multisource_auto_confirmed_with_confirmed_by_auto(self):
        """C 已落地：L3 + 多源互证(E2+) 仍走自动确认，且落库 confirmed_by='auto'。

        这是「L3 及以下保持自动」的逆向护栏：不能因为加了 E1 闸就把 L3 自动确认也关掉。
        """
        self._insert_cluster("C-L3", "E3")
        self._insert_impact(
            "P-L3", "C-L3", "L3", _valid_candidate()
        )
        result = self.service.auto_confirm_all()
        self.assertEqual(result["confirmed"], 1)
        self.assertEqual(self._forecast_count(), 1)
        self.assertEqual(self._confirmed_by_of("P-L3"), "auto")

    def test_c_manual_confirm_records_confirmed_by_user(self):
        """C 已落地：本人在界面选概率手动确认，落库 confirmed_by='user'。

        账本不可变，必须能一眼区分「人填的」还是「机器填的」，否则 Brier 校准
        里混着人类从未做过的预测，分数无意义。
        """
        self._insert_cluster("C-USER", "E3")
        self._insert_impact(
            "P-USER", "C-USER", "L3", _valid_candidate()
        )
        confirmed = self.service.confirm_candidate("P-USER", 0.65, by="user")
        self.assertIn("forecast_id", confirmed)
        self.assertEqual(self._forecast_count(), 1)
        self.assertEqual(self._confirmed_by_of("P-USER"), "user")

    def test_c_confirmed_by_exposed_in_list(self):
        """C 已落地：list_forecasts 透出 confirmed_by，界面才能把人机来源分开显示。"""
        self._insert_cluster("C-EXP", "E3")
        self._insert_impact("P-EXP", "C-EXP", "L3", _valid_candidate())
        self.service.confirm_candidate("P-EXP", 0.65, by="user")
        forecasts, _ = self.forecasts.list_forecasts()
        self.assertTrue(forecasts)
        self.assertEqual(forecasts[0]["confirmed_by"], "user")

    def test_c_l4_stays_pending_after_auto_confirm_all(self):
        """C 已落地：L4 改人工确认 —— auto_confirm_all 只自动确认 L3，不得代确认 L4。

        当前 src：auto_confirm_all 筛选为 `alert_level IN ('L3')`（impacts.py:682），
        L4 候选保持待人工确认，不产生不可变账本记录。
        """
        self._insert_cluster("C-L4A", "E3")
        self._insert_impact(
            "P-L4A", "C-L4A", "L4", _valid_candidate()
        )
        self.service.auto_confirm_all()
        # L4 应保持待人工确认：不产生不可变账本记录。
        self.assertEqual(self._forecast_count(), 0)
        self.assertFalse(self._is_confirmed("P-L4A"))

    def test_c_l4_not_auto_confirmed_in_map_judgment(self):
        """C 已落地：map_judgment 内联的 L4 自动确认已移除（impacts.py map_judgment 末尾）。

        L4 候选经 map_judgment 映射后不得被自动下账，保持待人工确认。
        """
        cluster_id, judgment_id = self.add_judgment("calib_l4", "E3")
        self.service.map_judgment(cluster_id, judgment_id)
        # L4 不应被自动确认进账本。
        _, total = self.forecasts.list_forecasts()
        self.assertEqual(total, 0)

    # ================================================================== #
    # A · 证伪性闸（架构师未落地）
    # ================================================================== #
    def test_a_falsification_gate_rejects_vague_proposition(self):
        """A 已落地：证伪性闸（_settlement_ready 四要素）拦截不可结算命题。

        端到端断言（不依赖闸的具体实现位置）：auto_confirm_all 后——
          - 含糊命题（缺 observable_signals 且含虚词「产生实际影响」）被证伪性闸拦截，
            不得进入不可变账本；
          - 可结算命题（日期/可观测条件/判定依据/无虚词 四要素齐备）照常下账。
        """
        # 可结算命题（应被确认）
        self._insert_cluster("C-SETTLE", "E3")
        self._insert_impact("P-SETTLE", "C-SETTLE", "L3", _valid_candidate())
        # 含糊命题（应被拦截，不得下账）
        self._insert_cluster("C-VAGUE", "E3")
        self._insert_impact(
            "P-VAGUE", "C-VAGUE", "L3", _valid_candidate(vague=True)
        )

        self.service.auto_confirm_all()

        # 可结算命题照常进入账本。
        self.assertEqual(self._confirmed_by_of("P-SETTLE"), "auto")
        # 含糊命题被证伪性闸拦截，不下账。
        self.assertFalse(self._is_confirmed("P-VAGUE"))

    # ================================================================== #
    # B · 旧条目标记与 Brier 排除（架构师未落地）
    # ================================================================== #
    @unittest.skip(
        "BLOCKED · 架构师未落地「旧条目标记 + Brier 排除」：\n"
        "  - src 中无任何旧条目标记字段（grep sampling_shift/old_entry/unusable 全 0 命中）；\n"
        "  - forecasts.py:score_summary / calibration_summary 的 Brier 聚合查询\n"
        "    (SELECT ... FROM resolutions WHERE brier_score IS NOT NULL) 未排除旧条目。\n"
        "  落地后补断言：混账本→Brier 只用新条目、旧条目仍在且可查询。当前无法构造带标记的输入，先占位。"
    )
    def test_b_old_entries_excluded_from_brier(self):
        raise AssertionError("占位：架构师落地后实现")

    # ================================================================== #
    # D · 趋势护栏（架构师未落地）
    # ================================================================== #
    @unittest.skip(
        "BLOCKED · 架构师未落地「趋势护栏 sampling_shift」：\n"
        "  - src 中 sampling_shift 字段 0 命中（grep 全仓无此字段）；\n"
        "  - 无「信源集合变化时标记采样偏移」的接口/返回值。\n"
        "  落地后补断言：信源集合变化 → sampling_shift=True+原因；不变 → False。先占位。"
    )
    def test_d_trend_sampling_shift_guard(self):
        raise AssertionError("占位：架构师落地后实现")


if __name__ == "__main__":
    unittest.main(verbosity=2)
