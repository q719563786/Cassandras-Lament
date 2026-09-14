"""知识库模块的契约测试。

这个模块此前是唯一零测试模块（349 行）。它的内容分两类：
- 知识文本常量（KNOWLEDGE_*）与规则表（LEADING_INDICATOR_PATTERNS 等）
- 四个纯函数：领先指标识别、风险信号识别、多路径推演、权力结构分析

测试只钉结构与行为，不钉具体措辞——那些文本是会随方法论更新而改的。
"""

import unittest

from yuanjian_app import knowledge_base


class KnowledgeConstantsTests(unittest.TestCase):
    def test_every_knowledge_block_is_joined_into_all_knowledge(self):
        """所有 KNOWLEDGE_* 文本块都必须并入 ALL_KNOWLEDGE。

        常量模块最容易出的错就是"加了一个新库、忘了并进去"，那样模型
        永远看不到它，而且不会有任何报错。这条测试专门防它。
        """
        blocks = {
            name: value
            for name, value in vars(knowledge_base).items()
            if name.startswith("KNOWLEDGE_") and isinstance(value, str)
        }

        self.assertGreaterEqual(len(blocks), 8, "知识块数量明显变少了")
        for name, value in blocks.items():
            self.assertTrue(value.strip(), f"{name} 是空的")
            self.assertIn(value, knowledge_base.ALL_KNOWLEDGE, f"{name} 未并入 ALL_KNOWLEDGE")

    def test_rule_tables_have_the_fields_the_functions_read(self):
        for key, config in knowledge_base.LEADING_INDICATOR_PATTERNS.items():
            self.assertEqual(
                sorted(config), ["keywords", "risk_boost", "signal"], f"{key} 字段不齐"
            )
            self.assertIsInstance(config["risk_boost"], float)
            self.assertTrue(config["keywords"], f"{key} 没有关键词")

        for key, rule in knowledge_base.POWER_STRUCTURE_RULES.items():
            self.assertEqual(
                sorted(rule),
                ["execution_layer", "markers", "risk_of_delay", "veto_power"],
                f"{key} 字段不齐",
            )


class LeadingIndicatorTests(unittest.TestCase):
    def test_matches_a_known_pattern_and_reports_its_boost(self):
        matches = knowledge_base.detect_leading_indicators("央行宣布降息 25 个基点", "")

        keys = {item["pattern"] for item in matches}
        self.assertIn("rate_change", keys)
        rate = next(item for item in matches if item["pattern"] == "rate_change")
        self.assertEqual(rate["risk_boost"], 0.15)
        self.assertIn("3-9", rate["signal"])

    def test_text_is_taken_from_title_and_summary_together(self):
        # 关键词只出现在摘要里也必须能命中
        self.assertTrue(knowledge_base.detect_leading_indicators("标题", "本次为公开征求意见"))
        # 关键词只出现在标题里同样要命中
        self.assertTrue(knowledge_base.detect_leading_indicators("三地入选改革试点", ""))

    def test_matching_is_case_insensitive(self):
        lower = knowledge_base.detect_leading_indicators("cpi 同比回落", "")
        upper = knowledge_base.detect_leading_indicators("CPI 同比回落", "")

        self.assertEqual(
            [item["pattern"] for item in lower], [item["pattern"] for item in upper]
        )
        self.assertTrue(lower)

    def test_multiple_patterns_can_match_at_once(self):
        matches = knowledge_base.detect_leading_indicators(
            "关税调整与降准同日公布", ""
        )
        keys = {item["pattern"] for item in matches}

        self.assertIn("trade_action", keys)
        self.assertIn("rate_change", keys)

    def test_unrelated_text_matches_nothing(self):
        self.assertEqual(knowledge_base.detect_leading_indicators("今天天气不错", ""), [])


class RiskSignalTests(unittest.TestCase):
    def test_militant_language_is_flagged(self):
        for text in ("坚决打赢脱贫攻坚战", "不惜一切代价推进", "启动攻坚战"):
            with self.subTest(text=text):
                self.assertTrue(knowledge_base.detect_risk_signals(text, ""))

    def test_keyword_in_summary_only_is_still_flagged(self):
        self.assertTrue(knowledge_base.detect_risk_signals("普通标题", "要严防死守"))

    def test_neutral_language_is_not_flagged(self):
        self.assertFalse(knowledge_base.detect_risk_signals("市水务局发布招标公告", "预算 1200 万元"))


class ScenarioPathTests(unittest.TestCase):
    def test_known_event_type_returns_three_labelled_paths(self):
        paths = knowledge_base.generate_scenario_paths("policy", ["某部委"], "政策文本")

        self.assertEqual(len(paths), 3)
        self.assertEqual([item["label"] for item in paths], ["最可能", "次可能", "黑天鹅"])
        for item in paths:
            self.assertEqual(sorted(item), ["label", "path", "probability", "trigger"])
            self.assertTrue(item["path"].strip())
            self.assertTrue(item["probability"].strip())

    def test_pusher_comes_from_the_first_institution(self):
        paths = knowledge_base.generate_scenario_paths("policy", ["某市发改委"], "")

        self.assertIn("某市发改委", paths[0]["path"])

    def test_missing_institutions_use_a_readable_fallback(self):
        paths = knowledge_base.generate_scenario_paths("policy", [], "")

        # 不能出现 "None" 这种把程序细节漏到用户面前的东西
        self.assertIn("事件发起方", paths[0]["path"])
        self.assertNotIn("None", paths[0]["path"])

    def test_unknown_event_type_falls_back_to_generic_paths(self):
        paths = knowledge_base.generate_scenario_paths("不存在的类型", ["甲"], "")

        self.assertEqual(len(paths), 3)
        self.assertEqual([item["label"] for item in paths], ["最可能", "次可能", "黑天鹅"])


class PowerStructureTests(unittest.TestCase):
    def test_central_marker_gives_low_delay_risk(self):
        result = knowledge_base.analyze_power_structure(["国务院"], "")

        self.assertEqual(result["rule"], "central_direct")
        self.assertEqual(result["delay_risk"], "低")
        self.assertEqual(sorted(result), ["delay_risk", "execution_layer", "rule", "veto_analysis"])

    def test_ministry_and_local_markers_map_to_their_rules(self):
        ministry = knowledge_base.analyze_power_structure(["某总局"], "")
        local = knowledge_base.analyze_power_structure(["某县政府"], "")

        self.assertEqual(ministry["rule"], "ministry_lead")
        self.assertEqual(ministry["delay_risk"], "中")
        self.assertEqual(local["rule"], "local_lead")
        self.assertEqual(local["delay_risk"], "高")

    def test_marker_in_the_text_counts_too(self):
        # 机构列表为空，但正文里出现了标记词，同样要能判定
        result = knowledge_base.analyze_power_structure([], "由全国人大审议")

        self.assertEqual(result["rule"], "central_direct")

    def test_unknown_structure_says_so_instead_of_guessing(self):
        result = knowledge_base.analyze_power_structure(["某行业协会"], "无相关标记")

        self.assertEqual(result["rule"], "unknown")
        self.assertEqual(result["delay_risk"], "未知")
        self.assertEqual(result["execution_layer"], "待确认")


if __name__ == "__main__":
    unittest.main()
