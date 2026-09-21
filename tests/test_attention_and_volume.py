"""v1.4 · S1/S3（R-05 / R-16）：让关注度分数真的在量"关注度"，并把生成量收回来。

  R-05 · `base_score = evidence×0.25 + confidence×0.20 + importance×0.25
        + exposure×0.20 + urgency×0.10`。其中 `evidence = EVIDENCE_WEIGHTS[E级]`，
        而本地路径下 `confidence = {"E1":0.30,"E2":0.50,"E3":0.70,"E4":0.82}[E级]`
        —— **两者都是"独立域名数"的单调函数，合计权重 0.45**。
        同一个变量计两次，等于把"来源多"这件事放大近一倍来驱动首页排序、通知、
        以及"能否被自动写进账本"。v1.2 已把**概率**与来源数解耦，但驱动关注度的
        分数没动，而这恰好是用户每天看到的东西。

  R-16 · 真库实测个人影响 118,379 条，其中 L2 占 108,535（92%），而首页最多显示
        3 条 —— 信噪比约 39,460 : 1。L1/L2 的候选**本来就不显示**，却仍然生成、
        仍然占库、仍然参与通知节流。
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.forecasts import ForecastService
from yuanjian_app.impacts import ImpactService, _alert_level
from yuanjian_app.interests import InterestService


NOW = datetime(2026, 9, 18, 8, tzinfo=timezone.utc)
TS = "2026-09-18T08:00:00Z"

# 新公式的权重（与 impacts.map_judgment 中的一致）。测试自己算一遍，
# 是为了让"哪个变量参与了运算"变成可核对的算术，而不是读一眼源码。
NEW_WEIGHTS = {"evidence": 0.3125, "importance": 0.3125, "exposure": 0.25, "urgency": 0.125}
# 旧公式（v1.3）：注意 confidence 与 evidence 同源 —— 这就是"同一变量计两次"。
OLD_WEIGHTS = {
    "evidence": 0.25, "confidence": 0.20, "importance": 0.25,
    "exposure": 0.20, "urgency": 0.10,
}
RANK = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}


def _score(components, weights):
    return round(
        sum(components[key] * weight for key, weight in weights.items())
        * components.get("personal_relevance", 1.0),
        6,
    )


def _old_alert_level(score):
    """v1.3 的分档阈值。变异对照必须**连阈值一起**还原 —— 只换公式不换阈值，
    比的就不是"旧行为"，而是"新阈值下的旧分数"。"""
    if score < 0.35:
        return "L1"
    if score < 0.55:
        return "L2"
    if score < 0.67:
        return "L3"
    return "L4"


class AttentionScoreTests(unittest.TestCase):
    """夹具：利益= cashflow × 影响= finance → 暴露度 1.0；horizons 90 天 → 紧迫度 0.4。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.interests = InterestService(self.database)
        self.forecasts = ForecastService(self.database)
        # importance=2：基础分落在 L2，给"命中风险信号上调一档"留出观察空间。
        self.interest = self.interests.create_object(
            {
                "name": "水泵业务回款",
                "category": "cashflow",
                "importance": 2,
                "privacy_level": "P3",
            }
        )
        self.service = ImpactService(
            self.database, self.interests, self.forecasts, now=lambda: NOW
        )
        self.counter = 0

    def tearDown(self):
        self.temporary.cleanup()

    def add_judgment(self, evidence_level="E2", risk_hit=("零容忍",)):
        self.counter += 1
        suffix = str(self.counter)
        cluster_id = f"C-{suffix}"
        judgment_id = f"J-{suffix}"
        # confidence 必须**随 E 级变**：真实数据里它是
        # `{"E1":0.30,"E2":0.50,"E3":0.70,"E4":0.82}[E级]`（judgment_local），
        # 与 evidence 同源 —— 这正是 R-05 要拆掉的共线。夹具给成同一个值的话，
        # "把 confidence 加回去"的变异对照就复现不出来。
        confidence = {"E1": 0.30, "E2": 0.50, "E3": 0.70, "E4": 0.82}.get(
            evidence_level, 0.30
        )
        result = {
            "fact_summary": "河源市水务局发布泵类采购公告",
            "actors": ["河源市水务局"],
            "causal_chain": ["公告发布", "投标", "定标"],
            "uncertainties": ["细则未定"],
            "horizons": ["未来90天"],
            "probability_low": 0.5,
            "probability_high": 0.7,
            "confidence": confidence,
            "supporting_source_ids": ["S-1"],
            "counter_source_ids": [],
            "up_triggers": ["挂出正式招标公告"],
            "down_triggers": ["公告撤销"],
            "impact_categories": ["finance"],
            "analysis_status": "real",
            "gyw": {
                "stakeholders": "【推动方】市水务局；【阻力方】竞争者",
                "constraints": "预算与工期约束",
                "least_resistance_path": "先易后难",
                "counter_evidence": "可能流标",
                "leading_indicators": "河源市水务局主页挂出泵类采购公告",
                "observable_signals": ["河源市水务局官网挂出泵类采购公告"],
                "risk_signal_hit": list(risk_hit),
                # v1.5（改动 A）：L4 现在**必须有结构性抓手**（执行摩擦或风险信号）。
                # `judgment_local` 每条研判都会落 gyw.power_structure；夹具此前缺这一项，
                # 在 risk_hit=() 的用例里会被正确地降为 L3。补上真实产物形状：
                # 河源市水务局=地方执行层 → delay_risk=高（结构强度 s=1.0，不改动既有分数）。
                "power_structure": {
                    "rule": "local_lead",
                    "execution_layer": "基层政府和具体执行机构",
                    "veto_analysis": "基层执行层有较大裁量权，可能变通或拖延",
                    "delay_risk": "高",
                    "matched_orgs": ["河源市水务局"],
                    "basis": "夹具：地方执行机构发文并落地；delay 取最低执行层。",
                },
            },
        }
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO event_clusters(
                    cluster_id,title,summary,first_seen_at,last_seen_at,
                    evidence_level,evidence_hash,categories_json,
                    latest_judgment_id,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    cluster_id, "河源市水务局发布泵类采购公告", "预算 1200 万元", TS, TS,
                    evidence_level, f"hash-{suffix}", '["finance","policy"]',
                    judgment_id, TS, TS,
                ),
            )
            connection.execute(
                "INSERT INTO judgments VALUES (?,?,?,?,?,?)",
                (
                    judgment_id, cluster_id, "local", f"hash-{suffix}",
                    __import__("json").dumps(result, ensure_ascii=False), TS,
                ),
            )
        return cluster_id, judgment_id

    def impacts_of(self, pair):
        rows = self.service.map_judgment(*pair)
        self.assertTrue(rows, "夹具必须产出候选，否则观察不到关注度等级")
        return rows

    # ------------------------------------------------------------------ #
    # R-05
    # ------------------------------------------------------------------ #
    def test_confidence_is_recorded_but_no_longer_counted(self):
        """把"哪个变量参与了运算"做成可核对的算术：
        `impact_score` 必须等于**新公式**的值，且**不等于**把 confidence 加回去的值。
        """
        impact = self.impacts_of(self.add_judgment(evidence_level="E2"))[0]
        components = impact["components"]

        self.assertIn("confidence", components, "confidence 仍要留档，便于回溯旧分数")
        new_score = _score(components, NEW_WEIGHTS)
        old_score = _score(components, OLD_WEIGHTS)

        self.assertAlmostEqual(impact["impact_score"], new_score, places=5)
        self.assertNotAlmostEqual(new_score, old_score, places=5)
        # 差距就是被去掉的那一项：confidence 与 evidence 同源，等于同一变量计两次。
        self.assertAlmostEqual(
            old_score - new_score,
            components["confidence"] * 0.20
            - components["evidence"] * 0.0625
            - components["importance"] * 0.0625
            - components["exposure"] * 0.05
            - components["urgency"] * 0.025,
            places=5,
        )

    def test_attention_level_does_not_rise_with_source_count(self):
        """两个**只有独立域名数不同**的簇（其余输入相同），关注度等级不得再单调上升。

        夹具用"命中风险信号"把两条都顶到 L3：基础分落在 L2 时 R-16 会把候选挡在
        生成之前（那本身是另一条断言），而"上调一档"恰好让它们重新落到 L3/L4，
        于是这个对比是**端到端可观测**的。

        新公式：E1 → 0.503125（L2）→上调 L3；E2 → 0.58125（L2）→上调 L3。**同级。**
        旧公式：E1 → 0.4625（L2）→上调 L3；E2 → 0.565（L3）→上调 L4。**单调上升。**
        """
        single = self.impacts_of(self.add_judgment(evidence_level="E1"))[0]
        multi = self.impacts_of(self.add_judgment(evidence_level="E2"))[0]

        self.assertEqual(
            single["alert_level"], multi["alert_level"],
            f"关注度等级仍在随来源数上升：{single['alert_level']} -> {multi['alert_level']}",
        )
        # 变异对照：把 confidence 加回去 + 还原旧阈值，等级立刻分叉
        # （旧 E1 0.4625 → L2；旧 E2 0.565 → L3，一档之差）。
        self.assertLess(
            RANK[_old_alert_level(_score(single["components"], OLD_WEIGHTS))],
            RANK[_old_alert_level(_score(multi["components"], OLD_WEIGHTS))],
            "旧公式必须表现为单调上升，否则这个对照证明不了新公式改了什么",
        )

    def test_alert_levels_are_spread_across_at_least_three_buckets(self):
        """新阈值不得把分布退化成单一档位（旧口径下 L2 占 91.7%）。

        这里用构造输入覆盖四档：分数由（证据、重要度、暴露度、紧迫度）四个离散量
        组合而成，逐个喂进去看分档。
        """
        cases = (
            ({"evidence": 0.25, "importance": 0.2, "exposure": 0.35, "urgency": 0.4}, "L1"),
            ({"evidence": 0.25, "importance": 0.6, "exposure": 0.7, "urgency": 0.4}, "L2"),
            ({"evidence": 0.25, "importance": 0.8, "exposure": 0.9, "urgency": 0.7}, "L3"),
            ({"evidence": 0.50, "importance": 1.0, "exposure": 1.0, "urgency": 1.0}, "L4"),
        )
        levels = []
        for index, (components, expected) in enumerate(cases):
            level = _alert_level(_score({**components, "confidence": 0.5}, NEW_WEIGHTS))
            levels.append(level)
            with self.subTest(case=index, components=components):
                self.assertEqual(level, expected)

        self.assertEqual(len(set(levels)), 4, f"四档必须都站得住：{levels}")

    # ------------------------------------------------------------------ #
    # R-16
    # ------------------------------------------------------------------ #
    def test_l1_and_l2_clusters_produce_no_candidates_at_all(self):
        """L1/L2 的候选本来就不显示，就**不该生成**：不写 personal_impacts、
        不产生 candidate_json。"""
        count_before = self._impact_count()

        result = self.service.map_judgment(*self.add_judgment(evidence_level="E1", risk_hit=()))

        self.assertEqual(result, [], "L1/L2 不该产出候选")
        self.assertEqual(self._impact_count(), count_before)
        with self.database.connect() as connection:
            with_candidates = connection.execute(
                "SELECT COUNT(*) FROM personal_impacts"
                " WHERE candidate_json IS NOT NULL AND candidate_json != ''"
            ).fetchone()[0]
        self.assertEqual(with_candidates, 0)

    def test_l3_and_l4_clusters_still_generate(self):
        """收敛不能收敛过头：L3/L4 照常生成（否则功能被打死）。

        两个利益（重要度 2 / 5）对同一簇各自出候选：低权重的落到 L3，高权重的落到
        L4 —— 两档都必须照常生成，且等级都要如实带上。
        """
        high = self.interests.create_object(
            {
                "name": "水泵业务主线",
                "category": "cashflow",
                "importance": 5,
                "privacy_level": "P3",
            }
        )
        rows = self.service.map_judgment(
            *self.add_judgment(evidence_level="E3", risk_hit=())
        )
        levels = {row["interest_id"]: row["alert_level"] for row in rows}

        self.assertTrue(rows, "L3/L4 必须照常生成")
        self.assertEqual(levels[self.interest["object_id"]], "L3")
        self.assertEqual(levels[high["object_id"]], "L4")

    def test_elevated_l2_still_generates(self):
        """被"慷慨激昂"上调到 L3 的候选照常生成 —— 上调本来就是"值得看"的信号，
        在这里截断会让上调规则再次变成装饰品。"""
        rows = self.impacts_of(self.add_judgment(evidence_level="E1", risk_hit=("零容忍",)))
        self.assertEqual(rows[0]["alert_level"], "L3")
        self.assertIn(
            "risk_signal_keywords", rows[0]["components"],
            "这条候选是靠风险信号上调的，components 里必须留下依据",
        )

    def _impact_count(self):
        with self.database.connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM personal_impacts").fetchone()[0]


if __name__ == "__main__":
    unittest.main(verbosity=2)
