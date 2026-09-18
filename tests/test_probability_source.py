"""设计审计第二波断言：概率来源（base_rate 基准率 / E 级只控宽度 / 校准口径 / 落库）。

只动 tests/**。所有断言针对「用户决策后的目标行为」，而非当前 src 的临时状态。
- 已落地的行为写普通断言（应绿）；
- 每条核心断言的变异对照见 build-artifacts/qa_probability_source_mutations.py
  （拷贝 src 到临时目录跑，绝不改真实 src）。

核心设计（architect 第二波）：
1) 基准率 base_rate 来自账本自身的同类别历史结算命中率（base_rate_for_category），
   样本 < 5 时必须为 None（绝不编造默认值顶上）；且 base_rate 不能由 probability 反推。
2) E 级只控制区间*宽度*，概率*中心*由 base_rate + 信号调整决定
   （信号不含来源数量——正是审计在批的失真源）。
3) 校准口径：base_rate 为 None 的预测仍计入 resolved_total，但不进 brier
   （excluded_total 相应增加），不可变账本行始终存在。
4) 落库：带 base_rate 的预测必须真的写进 forecast_versions 的内容里，
   而不是只传了个参数（_normalized_card 重建 dict、只保留白名单字段，
   不在返回表里的键会被静默丢弃——这是第三次必须能抓住的坑）。
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.forecasts import ForecastService, parse_frontmatter
from yuanjian_app.impacts import ImpactService
from yuanjian_app.interests import InterestService


TS = "2026-08-11T08:00:00Z"


def _settleable_forecast_dict(forecast_id, category, probability):
    """构造一条可结算（四要素齐备）的 forecast 入参，用于建历史/落库。"""
    return {
        "forecast_id": forecast_id,
        "title": f"截至2026-09-15：「本地利益」是否因「{category}事件」受到可观测影响",
        "resolution_criteria": (
            "判定依据：截至 2026-09-15，以官方公布的文件为准，对照可观测事实核验；"
            "任一事实在该日期前发生即记为发生。核验以官方公告、主管部门文件及本地可核验记录为准。"
        ),
        "window_start": "2026-08-11",
        "window_end": "2026-09-15",
        "probability": probability,
        "category": category,
        # v1.4（R-08）：可观测信号必须**事件级特化** —— 至少一条要含本事件特有的
        # 实体，或与该命题标题里的新闻原标题共享一段 ≥4 字的连续片段。
        # 夹具原先写的是"主管部门公布正式文件"这类类别模板短语，在默认路径下
        # 事件类型相同就拿到同样的措辞 —— 那正是新闸门要拦的"分类平均命题"。
        "observable_signals": f"{category}事件的主管部门公布正式文件；本地记录可核验",
        "confirmed_by": "user",
    }


class ProbabilitySourceAuditTests(unittest.TestCase):
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
    # helpers
    # ------------------------------------------------------------------ #
    def _seed_history(self, category, n, hits):
        """造 n 条同 category 的已结算记录（confirmed_by='user'），其中 hits 条 occurred。

        仅用于喂出 base_rate_for_category 需要的同类别历史；这些行本身也会进入
        calibration_summary 的 resolutions，但与本文件断言关注的“新建预测”分开。
        """
        for i in range(n):
            fid = f"F-SEED-{category}-{i}"
            self.forecasts.create_forecast(_settleable_forecast_dict(fid, category, 0.65))
        for i in range(n):
            fid = f"F-SEED-{category}-{i}"
            outcome = "occurred" if i < hits else "not_occurred"
            self.forecasts.resolve(fid, outcome, "2026-09-16", "seed")

    def _base_rate_of(self, forecast_id):
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT base_rate, base_rate_sample FROM forecasts WHERE forecast_id=?",
                (forecast_id,),
            ).fetchone()
        return (row["base_rate"], row["base_rate_sample"])

    def _content_base_rate_of(self, forecast_id):
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT content FROM forecast_versions WHERE forecast_id=? ORDER BY version DESC LIMIT 1",
                (forecast_id,),
            ).fetchone()
        fields = parse_frontmatter(row["content"]) if row else {}
        raw = fields.get("base_rate")
        if raw is None or raw == "" or raw == "null":
            return None
        return float(raw)

    def _candidate_for(self, evidence_level, strong_signal):
        """走真实的 _candidate，断言 E 级只控宽度、中心来自 base_rate+信号。

        (a) 单源强命题：E1 + 强信号（紧迫 + 具体触发 + 领先指标）
        (b) 多源平庸公告：E4 + 弱信号（无紧迫、无具体触发、无领先指标，但来源多）
        """
        cluster = {"cluster_id": "C", "title": "医保政策调整", "evidence_level": evidence_level}
        judgment = {
            "impact_categories": ["health"],
            "horizons": ["未来7天"] if strong_signal else ["未来90天"],
            "up_triggers": ["正式生效文件公布"] if strong_signal else [],
            "down_triggers": ["政策延期或叫停"] if strong_signal else [],
            "gyw": {"leading_indicators": "试点城市名单公布" if strong_signal else ""},
            "causal_chain": ["政策变化", "自付成本变化"],
            "supporting_source_ids": ["S-1"] + ([] if strong_signal else ["S-2", "S-3"]),
            "uncertainties": ["执行细则待公布"],
        }
        return self.service._candidate(cluster, judgment, self.health, "P-" + evidence_level)

    # ================================================================== #
    # A · 基准率（base_rate 来自账本自身历史，样本 < 5 为 None，不反推）
    # ================================================================== #
    def test_a_base_rate_equals_historical_hit_rate(self):
        """A 已落地：同 category ≥5 条已结算 → base_rate == 实际命中率，
        base_rate_sample == 样本数。"""
        self._seed_history("catA", 5, hits=3)  # 命中率 3/5 = 0.6
        result = self.forecasts.create_forecast(
            _settleable_forecast_dict("F-A1", "catA", 0.65)
        )
        rate, sample = self._base_rate_of(result["forecast_id"])
        self.assertEqual(sample, 5)
        self.assertAlmostEqual(rate, 0.6, places=4)

    def test_a_base_rate_none_when_sample_lt_5(self):
        """A 已落地：同 category <5 条已结算 → base_rate is None（不是编造的数字），
        但 base_rate_sample 仍如实记样本数（3）。"""
        self._seed_history("catB", 3, hits=2)
        result = self.forecasts.create_forecast(
            _settleable_forecast_dict("F-A2", "catB", 0.65)
        )
        rate, sample = self._base_rate_of(result["forecast_id"])
        self.assertIsNone(rate)
        self.assertEqual(sample, 3)

    def test_a_base_rate_not_derived_from_probability(self):
        """A 已落地（顺序性）：两条 probability 相同、但历史命中率不同的预测，
        其 base_rate 必须不同 —— 证明它不是从 probability 反推的。

        catC：5/5 命中（rate=1.0）；catD：1/5 命中（rate=0.2）。两条新预测都用 0.65。
        """
        self._seed_history("catC", 5, hits=5)
        self._seed_history("catD", 5, hits=1)
        r_c = self.forecasts.create_forecast(_settleable_forecast_dict("F-AC", "catC", 0.65))
        r_d = self.forecasts.create_forecast(_settleable_forecast_dict("F-AD", "catD", 0.65))
        rate_c, _ = self._base_rate_of(r_c["forecast_id"])
        rate_d, _ = self._base_rate_of(r_d["forecast_id"])
        self.assertNotEqual(rate_c, rate_d)
        self.assertAlmostEqual(rate_c, 1.0, places=4)
        self.assertAlmostEqual(rate_d, 0.2, places=4)

    # ================================================================== #
    # B · E 级只控宽度（核心对照，成对）
    # ================================================================== #
    def test_b_e_level_only_controls_width(self):
        """B 已落地：E 级只决定区间*宽度*（证据越弱越宽）。

        (a) E1（单源）宽度 > (b) E4（多源）宽度。
        与 test_b_multi_source_does_not_raise_center 成对；变异对照把中心改回
        E 级决定时，本断言仍绿、中心断言变红（隔离）。
        """
        a = self._candidate_for("E1", strong_signal=True)
        b = self._candidate_for("E4", strong_signal=False)
        width_a = a["probability_high"] - a["probability_low"]
        width_b = b["probability_high"] - b["probability_low"]
        # E1 最宽、E4 最窄：宽度严格递减。
        self.assertLess(width_b, width_a)
        # (a) 单源强命题允许“宽区间”。
        self.assertGreaterEqual(width_a, 0.18)

    def test_b_multi_source_does_not_raise_center(self):
        """B 已落地（核心）：多源平庸公告（E4 + 弱信号）中心不得因来源多而抬高。

        断言：(b) 区间宽度 < (a) 宽度，且 (b) 中心 ≤ (a) 中心。
        这要求中心来自 base_rate+信号，而非 E 级（来源数量）。E4 多源但弱信号，
        其中心必须 ≤ E1 单源但强信号的中心。
        变异对照：把“中心由 E 级决定”的旧逻辑（E1→0.40 / E4→0.75）放回去，
        本断言变红（0.75 ≤ 0.40 不成立）。
        """
        a = self._candidate_for("E1", strong_signal=True)
        b = self._candidate_for("E4", strong_signal=False)
        center_a = (a["probability_low"] + a["probability_high"]) / 2
        center_b = (b["probability_low"] + b["probability_high"]) / 2
        width_a = a["probability_high"] - a["probability_low"]
        width_b = b["probability_high"] - b["probability_low"]
        # (b) 区间窄
        self.assertLess(width_b, width_a)
        # (b) 中心不得因来源多而抬高
        self.assertLessEqual(center_b, center_a)
        # (a) 单源强命题允许“高中心”
        self.assertGreater(center_a, 0.5)

    # ================================================================== #
    # C · 与第一波口径一致（base_rate=None 仍计 resolved_total，不进 brier，行存在）
    # ================================================================== #
    def test_c_base_rate_none_excluded_from_brier_row_persists(self):
        """C 已落地：base_rate 为 None 的预测（样本不足/无基准率）仍计入 resolved_total，
        但不进 brier（excluded_total 相应 +1）；且该账本行仍然存在（不可变承诺没破）。"""
        # 全新 category，无历史 → base_rate=None。
        fid = "F-CNONE"
        self.forecasts.create_forecast(_settleable_forecast_dict(fid, "catNone", 0.65))
        self.forecasts.resolve(fid, "occurred", "2026-09-16", "note")
        summ = self.forecasts.calibration_summary()
        self.assertEqual(summ["resolved_total"], 1)
        self.assertEqual(summ["excluded_total"], 1)
        self.assertIsNone(summ["brier"])
        # 反证：账本行仍在。
        with self.database.connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM forecasts WHERE forecast_id=?", (fid,)
            ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_c_base_rate_none_only_excluded_one(self):
        """C 已落地（对照）：有基准率的可结算预测进 brier，base_rate=None 的才被排除。

        建一个可校准类别（≥5 历史，base_rate=0.8）的一条已结算预测 + 一条
        base_rate=None 预测。resolved_total=2，excluded_total=1（只排除 None 那条），
        brier 只用可校准那条算出来（非 None）。
        """
        self._seed_history("catCal", 5, hits=4)  # rate 0.8，可校准
        fid_cal = "F-CAL"
        self.forecasts.create_forecast(_settleable_forecast_dict(fid_cal, "catCal", 0.65))
        self.forecasts.resolve(fid_cal, "occurred", "2026-09-16", "note")
        fid_none = "F-CNONE2"
        self.forecasts.create_forecast(_settleable_forecast_dict(fid_none, "catFresh", 0.65))
        self.forecasts.resolve(fid_none, "occurred", "2026-09-16", "note")
        summ = self.forecasts.calibration_summary()
        # ⚠ 种子历史本身就是**已结算行**，必须计入总数：5 条 seed + 本次 2 条 = 7。
        # （初稿把这里写成 2，等于把种子当成了不算数的夹具，但它们在账本里是真的行。）
        self.assertEqual(summ["resolved_total"], 7)
        # 种子那 5 条在**创建当时**类别里还没有基准率（样本 < 5），故 base_rate 为 None；
        # 加上 catFresh 那条 —— 共 6 条不可校准。
        # 这不是过度排除，而是当时没有基准率就不参与校准的直接结果。
        self.assertEqual(summ["excluded_total"], 6)
        # 反向的核心：有基准率的那条**确实进了** Brier，没有被一起排除。
        self.assertIsNotNone(summ["brier"])
        rate_cal, _ = self._base_rate_of(fid_cal)
        self.assertAlmostEqual(rate_cal, 0.8, places=4)

    # ================================================================== #
    # D · 落库（最关键：base_rate 必须真写进 forecast_versions 内容，而非只传参）
    # ================================================================== #
    def test_d_normalized_card_drops_unknown_keys(self):
        """D 已落地（警示/机制）：_normalized_card 重建 dict 只保留白名单字段，
        不在返回表里的键会被静默丢弃。这正说明 D 主测试为什么必须查“落库内容”。"""
        from yuanjian_app.forecasts import _normalized_card

        card = _normalized_card(
            {
                "title": "t",
                "resolution_criteria": "c",
                "window_start": "2026-08-11",
                "window_end": "2026-09-15",
                "probability": 0.65,
                # 一个不在白名单里的幽灵键
                "ghost_field": "x",
            }
        )
        self.assertNotIn("ghost_field", card)

    def test_d_base_rate_persisted_into_forecast_versions(self):
        """D 已落地（最关键，第三次必须抓住的坑）：带 base_rate 的预测真的写进
        forecast_versions 的内容里，而不是只传了个参数。

        建一个可校准类别（base_rate=0.8），新建预测应把 0.8 渲染进内容；若
        _normalized_card 白名单或 _render_card 漏掉 base_rate，这里立刻变红。
        同时查 forecasts 表列与内容两处，确保“落库”而非“只传参”。
        """
        self._seed_history("catD2", 5, hits=4)  # rate 0.8
        fid = "F-DMAIN"
        self.forecasts.create_forecast(_settleable_forecast_dict(fid, "catD2", 0.65))
        col_rate, col_sample = self._base_rate_of(fid)
        content_rate = self._content_base_rate_of(fid)
        self.assertAlmostEqual(col_rate, 0.8, places=4)
        self.assertEqual(col_sample, 5)
        self.assertAlmostEqual(content_rate, 0.8, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
