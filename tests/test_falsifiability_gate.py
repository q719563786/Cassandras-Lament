"""v1.4 · S2（R-06 / R-07 / R-08 / R-09）：把证伪性闸从"形状"变成"实质"。

四个病，一个共同点 —— **闸门看起来比实际严**：

  R-06 · `_settlement_ready` 查四件事，其中两件**结构上不可能失败**：日期查的是
        `title + criteria` 里有无 `\\d{4}-\\d{2}-\\d{2}`，而 title 由 `_candidate`
        自己写成 `f"截至{end_date}：…"`；判定依据查 criteria 里有无
        `(核验|官方|公告|文件|记录|来源)`，而 criteria 是同函数硬编码的文本。
        **那一段在校验它自己刚写下的字。**

  R-07 · 禁用词检查跑在 `title + criteria` 上，而 title 内嵌了新闻原标题。
        一条标题写了"降息**可能影响**楼市"的新闻会让整条候选被拒 ——
        闸门把"分析者的含糊"和"记者的措辞"当成了同一件事。

  R-08 · 闸门唯一实质可失败的分支是"可观测条件非空"，而在默认（本地）路径下
        `observable_signals` **永远是事件类型的现成短语**（查表），任何 policy 类
        事件都拿到同样那五条。后果：**闸门在默认路径下全部放行**，账本里装的是
        "分类平均命题"——形式可结算，实质不含本事件信息。

  R-09 · 结算判据是对最多 4 条信号取**或**，且"在该日期前发生"包含窗口开始前的
        既成事实。四条各 30% 独立 → 或运算后约 76%，命题天然偏"发生"；而政策类
        模板信号里的"…发布配套实施细则"往往在事件被采集时已经存在 ——
        命题从建立那天起就已经成立，**零证伪风险**。
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.forecasts import ForecastService
from yuanjian_app.impacts import ImpactService, _settlement_ready
from yuanjian_app.interests import InterestService


NOW = datetime(2026, 9, 18, 8, tzinfo=timezone.utc)
TS = "2026-09-18T08:00:00Z"


class GateTests(unittest.TestCase):
    """夹具说明：利益类别（`interests.ALLOWED_CATEGORIES`）与影响类别
    （`impacts.ALLOWED_IMPACT_CATEGORIES`）是**两套不同的枚举**，交叉用会静默
    得到空候选。这里用 利益=cashflow × 影响=finance（暴露度 1.0）。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.interests = InterestService(self.database)
        self.forecasts = ForecastService(self.database)
        self.interest = self.interests.create_object(
            {
                "name": "水泵业务现金流",
                "category": "cashflow",
                "importance": 5,
                "privacy_level": "P3",
            }
        )
        # 第二个利益：importance=2。`auto_confirm_all` 只自动确认 **L3**（L4 一律
        # 留给人），而 importance=5 的候选在新阈值下基本都落在 L4 —— 没有这条低
        # 权重的利益，就没有任何 L3 候选可供"自动确认路径也走同一道闸"这条断言。
        self.secondary = self.interests.create_object(
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

    def add_judgment(
        self,
        cluster_title="河源市水务局发布泵类采购公告",
        cluster_summary="预算 1200 万元，工期 180 天",
        signals=("河源市水务局官网挂出泵类采购公告",),
        leading="河源市水务局主页挂出泵类采购公告",
        up_triggers=("挂出正式招标公告",),
        down_triggers=("公告撤销",),
        evidence_level="E3",
        **overrides,
    ):
        self.counter += 1
        suffix = str(self.counter)
        cluster_id = f"C-{suffix}"
        judgment_id = f"J-{suffix}"
        result = {
            "fact_summary": "河源市水务局发布泵类采购公告",
            "actors": ["河源市水务局"],
            "causal_chain": ["公告发布", "投标", "定标"],
            "uncertainties": ["细则未定"],
            "horizons": ["未来90天"],
            "probability_low": 0.5,
            "probability_high": 0.7,
            "confidence": 0.4,
            "supporting_source_ids": ["S-1"],
            "counter_source_ids": [],
            "up_triggers": list(up_triggers),
            "down_triggers": list(down_triggers),
            "impact_categories": ["finance"],
            "analysis_status": "real",
            "gyw": {
                "stakeholders": "【推动方】市水务局；【阻力方】竞争者",
                "constraints": "预算与工期约束",
                "least_resistance_path": "先易后难",
                "counter_evidence": "可能流标",
                # ⚠ `_extract_observable_signals` 会把 leading_indicators 与
                # up/down triggers **一并并进** observable_signals。所以要测
                # "信号全是类别模板短语"，这三处都得一起给成通用的 —— 只改
                # gyw.observable_signals 是不够的（那一处改了，leading 又把机构名带回来）。
                "leading_indicators": leading,
                "observable_signals": list(signals),
            },
        }
        gyw_override = overrides.pop("gyw", None)
        if gyw_override is not None:
            result["gyw"] = gyw_override
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
                    cluster_id, cluster_title, cluster_summary, TS, TS,
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

    def candidate_of(self, pair):
        rows = self.service.map_judgment(*pair)
        self.assertTrue(
            rows,
            "没有产出候选影响——若这里为空，说明 R-16 的关注度闸把它挡在了生成之前，"
            "本测试就测不到证伪性闸了",
        )
        return rows[0], self.service.candidate_forecast(rows[0]["impact_id"])

    # ------------------------------------------------------------------ #
    # R-06
    # ------------------------------------------------------------------ #
    def test_gate_no_longer_checks_date_or_criteria_keywords(self):
        """最硬的证明：把日期与判定依据**都拿掉**，只要信号可观测且特化，闸门仍然放行 ——
        说明那两项检查真的不存在了（否则这里会以"缺少可核验日期"被拒）。
        """
        ok, reason = _settlement_ready(
            {
                "title": "某件事会不会发生",  # 没有任何 \d{4}-\d{2}-\d{2}
                "resolution_criteria": "看着办",  # 没有"核验/官方/公告/文件/记录/来源"
                "observable_signals": "河源市水务局挂出泵类采购公告",
                "source_headline": "河源市水务局发布泵类采购公告",
            }
        )

        self.assertTrue(ok, f"闸门仍在查日期/判定依据：{reason}")

    def test_gate_still_rejects_missing_observable_condition(self):
        """反过来钉住：真正该拦的那一项必须拦得住。"""
        ok, reason = _settlement_ready(
            {
                "title": "截至2026-12-31：某件事是否发生",
                "resolution_criteria": "以官方公告核验，出现条目即记为发生",
                "observable_signals": "",
            }
        )

        self.assertFalse(ok)
        self.assertIn("可观测条件", reason)

    def test_gate_rejects_candidate_without_a_determinable_main_signal(self):
        ok, reason = _settlement_ready(
            {
                "title": "t",
                "resolution_criteria": "c",
                "observable_signals": "；；",
            }
        )
        self.assertFalse(ok)
        self.assertIn("主信号", reason)

    # ------------------------------------------------------------------ #
    # R-07
    # ------------------------------------------------------------------ #
    def test_news_headline_with_banned_phrase_does_not_block_the_candidate(self):
        """新闻原标题里有"可能影响"，候选**不该**被误拒。

        含糊的是**我们的命题**，不是被报道的那句话。
        """
        pair = self.add_judgment(
            cluster_title="机构：降息可能影响楼市",
            cluster_summary="某机构发布报告，提到降息可能影响楼市",
            signals=("河源市水务局官网挂出泵类采购公告",),
        )
        _row, candidate = self.candidate_of(pair)

        self.assertIn("可能影响", candidate["title"], "夹具必须真的把这句话嵌进标题里")
        ok, reason = _settlement_ready(candidate)

        self.assertTrue(ok, f"新闻标题里的措辞把候选误拒了：{reason}")

    def test_banned_phrase_in_generated_signal_still_blocks(self):
        """生成的那部分含禁用虚词 → 必须拦（否则闸门就真的没了）。"""
        ok, reason = _settlement_ready(
            {
                "title": "某命题",
                "resolution_criteria": "以公告核验",
                "observable_signals": "河源市水务局挂出泵类采购公告；待补充",
                "source_headline": "河源市水务局发布泵类采购公告",
            }
        )

        self.assertFalse(ok)
        self.assertIn("不可结算表述", reason)

    def test_banned_phrase_in_generated_template_still_blocks(self):
        ok, reason = _settlement_ready(
            {
                "title": "截至2026-12-31：是否产生实际影响",
                "resolution_criteria": "以公告核验",
                "observable_signals": "河源市水务局挂出泵类采购公告",
                "source_headline": "河源市水务局发布泵类采购公告",
            }
        )

        self.assertFalse(ok)
        self.assertIn("实际影响", reason)

    # ------------------------------------------------------------------ #
    # R-08
    # ------------------------------------------------------------------ #
    def test_category_template_signals_are_rejected_as_unspecialized(self):
        """闸门在默认路径下的**全部放行**，就是靠这条堵上的。

        信号全是类别模板短语（无机构名/地名/数字、与本事件标题无共同长片段）→
        必须被拒，且原因指向"未特化"。
        """
        ok, reason = _settlement_ready(
            {
                "title": "截至2027-03-17：「水泵业务现金流」是否因「某市水务局发布泵类采购公告」受到可观测影响",
                "resolution_criteria": "以公告核验",
                "observable_signals": (
                    "配套实施细则发布；首批试点名单公布；执行部门预算调整"
                ),
                "source_headline": "某市水务局发布泵类采购公告",
            }
        )

        self.assertFalse(ok)
        self.assertIn("未特化", reason)

    def test_injecting_the_event_entity_into_one_signal_passes(self):
        """同一条候选，把机构名注入其中一条信号 → 通过（证明上面那条断言真的
        是被"特化"这一条绊住的，而不是别的原因）。"""
        ok, reason = _settlement_ready(
            {
                "title": "截至2027-03-17：「水泵业务现金流」是否因「某市水务局发布泵类采购公告」受到可观测影响",
                "resolution_criteria": "以公告核验",
                "observable_signals": (
                    "某市水务局发布配套实施细则；首批试点名单公布"
                ),
                "source_headline": "某市水务局发布泵类采购公告",
            }
        )

        self.assertTrue(ok, reason)

    def test_shared_event_fragment_also_counts_as_specialized(self):
        """「专有名词」那一档：与事件标题共享一段 ≥4 字的连续片段也算特化。

        但要避开"发布 / 公告 / 通知"这类任何事件都会出现的通用词。
        """
        specialized, reason = _settlement_ready(
            {
                "title": "t",
                "resolution_criteria": "c",
                "observable_signals": "泵类采购公告相关投标截止日期顺延",
                "source_headline": "某市水务局发布泵类采购公告",
            }
        )
        generic, generic_reason = _settlement_ready(
            {
                "title": "t",
                "resolution_criteria": "c",
                "observable_signals": "相关部门发布通知",
                "source_headline": "某市水务局发布泵类采购公告",
            }
        )

        self.assertTrue(specialized, reason)
        self.assertFalse(generic, generic_reason)

    def test_gate_applies_to_auto_confirm_path(self):
        """`auto_confirm_all` 也走同一道闸：模板信号的候选不得自动入账。"""
        pair = self.add_judgment(
            signals=("配套实施细则发布", "首批试点名单公布"),
            leading="配套细则与执行进度",
            up_triggers=("执行部门行动通报",),
            down_triggers=("政策延期",),
        )
        cluster_id, _judgment_id = pair
        self.service.map_judgment(cluster_id, _judgment_id)

        result = self.service.auto_confirm_all()

        self.assertEqual(result["confirmed"], 0)
        _, total = self.forecasts.list_forecasts()
        self.assertEqual(total, 0)
        self.assertTrue(
            any("未特化" in message for message in result["errors"]),
            result["errors"],
        )

    def test_gate_applies_to_manual_confirm_path(self):
        pair = self.add_judgment(
            signals=("配套实施细则发布",),
            leading="配套细则与执行进度",
            up_triggers=("执行部门行动通报",),
            down_triggers=("政策延期",),
        )
        row, _candidate = self.candidate_of(pair)

        with self.assertRaisesRegex(ValueError, "未特化"):
            self.service.confirm_candidate(row["impact_id"], 0.65)

    def test_pending_candidates_expose_the_gate_reason_to_the_ui(self):
        """候选卡上要能直接看到"为什么停在待补充"，不能等点了确认才弹错。"""
        pair = self.add_judgment(
            signals=("配套实施细则发布",),
            leading="配套细则与执行进度",
            up_triggers=("执行部门行动通报",),
            down_triggers=("政策延期",),
        )
        self.service.map_judgment(*pair)

        candidates = self.service.pending_candidates()

        self.assertTrue(candidates)
        blocked = candidates[0]
        self.assertFalse(blocked["settleable"])
        self.assertIn("未特化", blocked["settle_block_reason"])

    # ------------------------------------------------------------------ #
    # R-09
    # ------------------------------------------------------------------ #
    def test_resolution_criteria_states_main_signal_and_window_constraint(self):
        pair = self.add_judgment()
        _row, candidate = self.candidate_of(pair)

        criteria = candidate["resolution_criteria"]

        self.assertIn("主信号", criteria)
        self.assertIn("首次被观测到", criteria)
        self.assertIn(candidate["window_start"], criteria)
        self.assertIn(candidate["window_end"], criteria)
        self.assertIn("不等同于命中", criteria)
        # 主信号必须是信号清单的第一条（结算只认它）。
        self.assertEqual(
            candidate["main_signal"],
            candidate["observable_signals"].split("；")[0],
        )
        # 旧口径必须消失：对多条信号取"或"的措辞不能再出现。
        self.assertNotIn("任一事实", criteria)

    def test_preexisting_main_signal_is_rejected(self):
        """主信号在窗口开始前就已成立 → 零证伪风险，不得记为可结算命中。"""
        pair = self.add_judgment(
            cluster_title="河源市水务局配套实施细则已正式出台",
            cluster_summary="河源市水务局已正式出台配套实施细则，即日起执行",
            signals=("河源市水务局出台配套实施细则",),
        )
        _row, candidate = self.candidate_of(pair)

        self.assertTrue(
            candidate["main_signal_preexisting"],
            "证据里明写'已正式出台'，主信号必须被判为既成事实",
        )
        ok, reason = _settlement_ready(candidate)
        self.assertFalse(ok)
        self.assertIn("窗口开始前已成立", reason)

        with self.assertRaisesRegex(ValueError, "窗口开始前已成立"):
            self.service.confirm_candidate(_row["impact_id"], 0.65)

    def test_future_signal_is_not_treated_as_preexisting(self):
        """对照组：普通新闻措辞（没有既成事实标记）不得被判为既成事实 ——
        否则闸门会变成"什么都拦"，也就等于没有闸门。"""
        pair = self.add_judgment()
        _row, candidate = self.candidate_of(pair)

        self.assertFalse(candidate["main_signal_preexisting"])

    def test_news_headline_bearing_a_preexisting_marker_leaks_into_the_check(self):
        """已知边界（写下来，不假装它不存在）：

        既成事实检查读的是**事件簇文本**（标题/摘要/事实摘要），而不是逐条信号的
        出处。所以一条**报道"某事已发生"**的新闻，会让该事件的所有候选都带上
        "已成立"的标记。这是**有意偏保守**的方向：那种事件本来就更适合当"已发生的
        事实"记，而不是拿去当预测命题。真实代价是这类事件会停在待补充。
        """
        pair = self.add_judgment(
            cluster_title="河源市水务局已于昨日发布泵类采购公告",
            cluster_summary="河源市水务局已于昨日发布泵类采购公告",
            signals=("河源市水务局发布后续补充公告",),
        )
        _row, candidate = self.candidate_of(pair)

        self.assertTrue(candidate["main_signal_preexisting"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
