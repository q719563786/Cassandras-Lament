"""第四、五波的行为契约测试。

这一批专门钉三件"此前说了但没做到"的事，以及一个反复发作的缺陷：

  A · `risk_boost` 真正参与概率运算（此前只是拼在展示文本里的装饰百分比）
  B · 「慷慨激昂」上调的是**关注度**，不是我方置信度（此前方向反了）
  C · 量级：有数字就说有、没有就说"未量化"，不许用严重的词替代没有的数字
  D · **字段静默丢失病的第 4 次**：证伪性闸在入账时用 observable_signals 判过命题，
      但落进账本时它被丢了；base_rate 更是从未落过库
  E · 候选卡必须带**真实**的 alert_level（confirm_candidate 此前硬编码 L3）
  F · forecasts 的白名单函数必须保留本轮新增字段

夹具说明（踩过的坑）：利益类别（`interests.ALLOWED_CATEGORIES`）与
影响类别（`impacts.ALLOWED_IMPACT_CATEGORIES`）是**两套不同的枚举**，
交叉用会得到空候选。这里用 利益=cashflow × 影响=finance（暴露度 1.0）。
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.forecasts import ForecastService, _normalized_card
from yuanjian_app.impacts import (
    MAX_OBSERVABLE_SIGNAL_CHARS,
    MAX_OBSERVABLE_SIGNALS,
    ImpactService,
    _extract_observable_signals,
)
from yuanjian_app.interests import InterestService

RANK = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}


class ObservableSignalHygieneTests(unittest.TestCase):
    """命题里的"可观测事实"必须是**人能拿去对照一条新闻**的短句。

    发现经过：端到端探针显示，`leading_indicators` 的尾部被拼上了规则引擎的内部记账
    文本（「｜规则引擎命中：…（领先信号权重 +10%）｜合计权重 +10%（已计入概率中心…）」），
    而信号抽取又把整段收下 —— 于是**命题里塞进了一段程序内部文本**，
    标题和判定依据长到读不下去。判定依据那一栏应当是"看什么新闻能判定"。
    """

    def test_内部记账文本被剥掉(self):
        signals = _extract_observable_signals(
            {
                "gyw": {
                    "leading_indicators": (
                        "领先指标：配套细则发布时间、试点名单"
                        "｜规则引擎命中：财政资金下达（领先信号权重 +10%）"
                        "｜合计权重 +10%（已计入概率中心，合计封顶 20%）"
                    )
                },
                "up_triggers": [],
                "down_triggers": [],
            }
        )

        self.assertNotIn("规则引擎命中", signals)
        self.assertNotIn("已计入概率中心", signals)
        self.assertIn("配套细则发布时间", signals)

    def test_结构化信号优先且不被截断(self):
        signals = _extract_observable_signals(
            {
                "gyw": {
                    "observable_signals": ["主页挂出泵类采购公告"],
                    "leading_indicators": "领先指标：公告发布",
                },
                "up_triggers": ["出现正式文件"],
                "down_triggers": [],
            }
        )

        self.assertTrue(signals.startswith("主页挂出泵类采购公告"), signals)

    def test_单条与总数都有上限(self):
        long_signal = "甲" * (MAX_OBSERVABLE_SIGNAL_CHARS * 3)
        signals = _extract_observable_signals(
            {
                "gyw": {"observable_signals": [long_signal, "乙" * 80, "丙" * 80, "丁", "戊", "己"]},
                "up_triggers": [],
                "down_triggers": [],
            }
        )
        parts = signals.split("；")

        self.assertLessEqual(len(parts), MAX_OBSERVABLE_SIGNALS)
        for part in parts:
            self.assertLessEqual(len(part), MAX_OBSERVABLE_SIGNAL_CHARS + 1, part)

    def test_空与占位值被丢弃(self):
        signals = _extract_observable_signals(
            {"gyw": {"observable_signals": ["", "待补充", "尚未补充"]}, "up_triggers": [""]}
        )

        self.assertEqual(signals, "")


class Wave45Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.interests = InterestService(self.database)
        self.forecasts = ForecastService(self.database)
        # importance=2 让基础分落在 L2，这样"上调一档"看得出来（若基础就是 L4，观察不到）
        self.interest = self.interests.create_object(
            {
                "name": "水泵业务现金流",
                "category": "cashflow",
                "importance": 2,
                "privacy_level": "P3",
            }
        )
        self.service = ImpactService(
            self.database,
            self.interests,
            self.forecasts,
            now=lambda: datetime(2026, 9, 17, 8, tzinfo=timezone.utc),
        )
        self.counter = 0

    def tearDown(self):
        self.temporary.cleanup()

    def add_judgment(
        self,
        evidence_level="E2",
        cluster_summary="预算 1200 万元，工期 180 天",
        **overrides,
    ):
        """造一条研判并落到临时库。返回 (cluster_id, judgment_id)。"""
        self.counter += 1
        suffix = f"{self.counter}"
        cluster_id = f"C-{suffix}"
        judgment_id = f"J-{suffix}"
        timestamp = "2026-09-17T08:00:00Z"
        gyw = {
            "stakeholders": "【推动方】市水务局；【阻力方】竞争者",
            "constraints": "预算与工期约束",
            "least_resistance_path": "先易后难",
            "counter_evidence": "可能流标",
            "leading_indicators": "主页挂出泵类采购公告",
            "beneficiaries": [],
            "cost_bearers": [],
            "historical_parallel": None,
            # 结构化可观测信号（校验器唯一强制过"能被未来新闻证伪"的字段）
            "observable_signals": ["主页挂出泵类采购公告", "投标截止日期顺延"],
        }
        result = {
            "fact_summary": "某市水务局发布泵类采购公告",
            "actors": ["河源市水务局"],
            "causal_chain": ["公告发布", "投标", "定标"],
            "uncertainties": ["细则未定"],
            "horizons": ["未来90天"],
            "probability_low": 0.5,
            "probability_high": 0.7,
            "confidence": 0.4,
            "supporting_source_ids": [],
            "counter_source_ids": [],
            "up_triggers": ["挂出正式招标公告"],
            "down_triggers": ["公告撤销"],
            "impact_categories": ["finance"],
            "analysis_status": "real",
            "gyw": gyw,
        }
        gyw.update(overrides.pop("gyw", {}))
        result.update(overrides)

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
                    cluster_id,
                    "某市水务局发布泵类采购公告",
                    cluster_summary,
                    timestamp,
                    timestamp,
                    evidence_level,
                    f"hash-{suffix}",
                    '["finance","policy"]',
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

    def candidate_of(self, pair):
        """pair = add_judgment() 的返回值。返回 (impact_row, candidate_card)。"""
        cluster_id, judgment_id = pair
        rows = self.service.map_judgment(cluster_id, judgment_id)
        self.assertTrue(rows, f"没有产出候选影响（{cluster_id}/{judgment_id}）")
        return rows[0], self.service.candidate_forecast(rows[0]["impact_id"])

    def ledger_text(self, forecast_id):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT content FROM forecast_versions WHERE forecast_id=?",
                (forecast_id,),
            ).fetchone()
        self.assertIsNotNone(row, "版本内容不存在")
        return row["content"]

    def confirm(self, pair, probability=0.5):
        row, candidate = self.candidate_of(pair)
        result = self.service.confirm_candidate(row["impact_id"], probability, by="user")
        return row, candidate, result


class RiskBoostParticipatesTests(Wave45Fixture):
    """A · risk_boost 必须真的进公式（审计 2.6：规则引擎此前是装饰性的）。"""

    def test_领先指标权重抬高概率中心(self):
        center = {}
        for label, gyw in (
            ("without", {}),
            (
                "with",
                {
                    "leading_indicator_hits": [
                        {
                            "pattern": "budget_allocation",
                            "signal": "财政资金下达",
                            "risk_boost": 0.15,
                        }
                    ],
                    "leading_boost": 0.15,
                },
            ),
        ):
            _row, cand = self.candidate_of(self.add_judgment(gyw=gyw))
            center[label] = (cand["probability_low"] + cand["probability_high"]) / 2
            if label == "with":
                self.assertEqual(cand["leading_boost"], 0.15)

        self.assertGreater(
            center["with"],
            center["without"],
            "领先指标权重没有进入概率中心 —— 那它还是装饰品",
        )

    def test_没有命中时权重为零(self):
        _row, cand = self.candidate_of(
            self.add_judgment(gyw={"leading_indicator_hits": [], "leading_boost": 0.0})
        )

        self.assertEqual(cand["leading_boost"], 0.0)

    def test_权重被限制在上限内(self):
        """8 条模式可以同时命中；不封顶就成了"关键词越多概率越高"。"""
        _row, cand = self.candidate_of(
            self.add_judgment(
                gyw={
                    "leading_indicator_hits": [
                        {"pattern": f"p{i}", "signal": "x", "risk_boost": 0.15}
                        for i in range(8)
                    ]
                }
            )
        )

        self.assertLessEqual(cand["leading_boost"], 0.20)

    def test_宽度不因领先指标而改变(self):
        """E 级只管宽度，领先指标只管中心 —— 两件事不能混（第二波的设计）。"""
        _r1, cand1 = self.candidate_of(self.add_judgment(evidence_level="E3", gyw={}))
        _r2, cand2 = self.candidate_of(
            self.add_judgment(
                evidence_level="E3",
                gyw={
                    "leading_indicator_hits": [
                        {"pattern": "x", "signal": "s", "risk_boost": 0.15}
                    ]
                },
            )
        )

        width1 = cand1["probability_high"] - cand1["probability_low"]
        width2 = cand2["probability_high"] - cand2["probability_low"]
        self.assertAlmostEqual(width1, width2, places=6)


class MilitantLanguageRaisesAttentionTests(Wave45Fixture):
    """B · 「慷慨激昂 = 内心已感知风险」→ 上调关注度，不是上调我方置信度。"""

    def test_命中风险信号会抬高关注度等级(self):
        row1, _ = self.candidate_of(self.add_judgment(gyw={}))
        row2, cand2 = self.candidate_of(
            self.add_judgment(gyw={"risk_signal_hit": ["坚决打赢", "攻坚战"]})
        )

        self.assertGreater(
            RANK[row2["alert_level"]],
            RANK[row1["alert_level"]],
            "慷慨激昂没有抬高关注度 —— 规则又变成装饰品了",
        )
        self.assertEqual(cand2["risk_signal_hit"], ["坚决打赢", "攻坚战"])

    def test_命中记录在案可追溯(self):
        _row, cand = self.candidate_of(
            self.add_judgment(gyw={"risk_signal_hit": ["零容忍"]})
        )

        self.assertEqual(cand["risk_signal_hit"], ["零容忍"])

    def test_未命中时等级不变(self):
        row1, _ = self.candidate_of(self.add_judgment(gyw={}))
        row2, _ = self.candidate_of(self.add_judgment(gyw={"risk_signal_hit": []}))

        self.assertEqual(row1["alert_level"], row2["alert_level"])

    def test_风险信号不得把E1顶上L4(self):
        """E1 是单源线索。README 与 PRIVACY.md 承诺它无论多重要都不得超过 L3，
        这条承诺必须在"上调之后"仍然成立。"""
        row, _cand = self.candidate_of(
            self.add_judgment(
                evidence_level="E1", gyw={"risk_signal_hit": ["不惜一切代价"]}
            )
        )

        self.assertNotEqual(row["alert_level"], "L4")


class MagnitudeTests(Wave45Fixture):
    """C · 量级：L 档是关注度，不是损失规模（审计 2.11）。"""

    def test_没有数字时如实写未量化(self):
        _row, cand = self.candidate_of(
            self.add_judgment(cluster_summary="情况正在发展", gyw={"constraints": "条件未明"})
        )

        self.assertEqual(cand["magnitude"]["numbers"], [])
        self.assertEqual(cand["magnitude"]["level"], "未量化")
        self.assertIn("未量化", cand["magnitude_line"])
        self.assertIn("不知道", cand["magnitude"]["basis"])

    def test_有数字时列出来但不下量级结论(self):
        _row, cand = self.candidate_of(
            self.add_judgment(cluster_summary="预算 1200 万元，工期 180 天")
        )

        self.assertTrue(cand["magnitude"]["numbers"])
        self.assertIn("有数字", cand["magnitude"]["level"])
        # 不得因为看见一个金额就说"影响很大"
        self.assertNotIn("严重", cand["magnitude_line"])

    def test_量级带范围(self):
        _row, cand = self.candidate_of(
            self.add_judgment(
                gyw={
                    "power_structure": {
                        "rule": "local_lead",
                        "execution_layer": "基层政府",
                        "veto_analysis": "有裁量权",
                        "delay_risk": "高",
                        "matched_orgs": ["河源市水务局"],
                        "basis": "x",
                    }
                }
            )
        )

        self.assertEqual(cand["magnitude"]["scope"], "地方/区域")


class LedgerFieldLossTests(Wave45Fixture):
    """D · 「字段静默丢失」的第 4 次发作。

    证伪性闸在**入账那一刻**用 observable_signals 判断命题能不能结算，
    可它此前根本没被传给 create_forecast —— 于是账本里没有"阈值或可观测事实"，
    到期时命题实际上无法机械核验。base_rate 同理，第二波加了字段却从未落库。
    """

    def setUp(self):
        super().setUp()
        # 让基准率有样本，才测得出"是否真的落库"
        self.forecasts.base_rate_for_category = lambda category: (0.42, 9)

    def test_可观测信号真的落进账本(self):
        _row, _cand, result = self.confirm(self.add_judgment())
        text = self.ledger_text(result["forecast_id"])

        self.assertIn("observable_signals:", text)
        self.assertIn("主页挂出泵类采购公告", text)

    def test_基准率真的落进账本(self):
        _row, _cand, result = self.confirm(self.add_judgment())
        text = self.ledger_text(result["forecast_id"])

        self.assertIn("base_rate: 0.42", text)
        self.assertIn("base_rate_sample: 9", text)
        self.assertNotIn("base_rate: null", text)

    def test_量级与风险信号也落进账本(self):
        _row, _cand, result = self.confirm(
            self.add_judgment(gyw={"risk_signal_hit": ["零容忍"]})
        )
        text = self.ledger_text(result["forecast_id"])

        self.assertIn("magnitude:", text)
        self.assertIn("risk_signal_keywords: 零容忍", text)

    def test_变异对照_字段确实来自候选卡(self):
        """证明上面几条不是"碰巧通过"：候选卡里没有的值，账本里必须是空的。"""
        _row, _cand, result = self.confirm(self.add_judgment())
        self.assertIn("magnitude:", self.ledger_text(result["forecast_id"]))

        card = _normalized_card(
            {
                "title": "截至2026-12-31：是否挂出公告",
                "resolution_criteria": "以主管部门公告核验",
                "probability": 0.5,
                "window_start": "2026-09-17",
                "window_end": "2026-12-31",
            }
        )
        self.assertEqual(card["magnitude"], "")
        self.assertEqual(card["observable_signals"], "")
        self.assertEqual(card["risk_signal_keywords"], "")

    def test_白名单函数保留本轮新增字段(self):
        """机械守卫：`_normalized_card` 是重建 dict 的白名单，
        不在返回表里的键会被静默丢掉（confirmed_by / observable_signals / base_rate
        都栽在这上面）。"""
        card = _normalized_card(
            {
                "title": "截至2026-12-31：是否挂出公告",
                "resolution_criteria": "以主管部门公告核验",
                "probability": 0.5,
                "window_start": "2026-09-17",
                "window_end": "2026-12-31",
                "observable_signals": "主页挂出公告",
                "base_rate": 0.42,
                "base_rate_sample": 9,
                "magnitude": "范围 地方/区域；量级 未量化",
                "risk_signal_keywords": "零容忍",
            }
        )

        self.assertEqual(card["observable_signals"], "主页挂出公告")
        self.assertEqual(card["base_rate"], 0.42)
        self.assertEqual(card["base_rate_sample"], 9)
        self.assertEqual(card["magnitude"], "范围 地方/区域；量级 未量化")
        self.assertEqual(card["risk_signal_keywords"], "零容忍")


class CandidateAlertLevelTests(Wave45Fixture):
    """E · 候选卡要带真实等级；confirm_candidate 不得硬编码 L3。"""

    def test_候选卡带真实告警等级(self):
        row, cand = self.candidate_of(self.add_judgment(evidence_level="E4"))

        self.assertEqual(cand["alert_level"], row["alert_level"])
        self.assertIsNotNone(cand["alert_level"])

    def test_入账沿用候选的真实等级(self):
        _row, cand, result = self.confirm(self.add_judgment(evidence_level="E4"))
        text = self.ledger_text(result["forecast_id"])

        self.assertIn(f"alert_level: {cand['alert_level']}", text)

    def test_变异对照_硬编码L3会被抓住(self):
        """如果候选的真实等级不是 L3，而落库后变成 L3，就说明又在硬编码。"""
        _row, cand, result = self.confirm(self.add_judgment(evidence_level="E4"))
        if cand["alert_level"] == "L3":
            self.skipTest("本条夹具产出的是 L3，无法区分是否硬编码")

        text = self.ledger_text(result["forecast_id"])
        self.assertIn(f"alert_level: {cand['alert_level']}", text)
        self.assertNotIn("alert_level: L3", text)


if __name__ == "__main__":
    unittest.main()
