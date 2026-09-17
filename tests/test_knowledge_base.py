"""知识库模块的契约测试。

这个模块此前是唯一零测试模块。它的内容分两类：
- 知识文本常量（KNOWLEDGE_*）与规则表（LEADING_INDICATOR_PATTERNS 等）
- 四个纯函数：领先指标识别、风险信号识别、多路径推演、权力结构分析

测试只钉结构与行为，不钉具体措辞——那些文本是会随方法论更新而改的。

═══ v1.3 更新说明（审计 2.5 / 2.7 / 七步#7）═══

本轮对知识库做了三处**有意的语义变更**，因此下列断言随之更新（不是放宽断言）：

1. **天外实体侧移出通用注入**（审计 2.7）：它此前和另外八个库一起进入
   *每一次* 研判。现在只在相关事件上注入。因此"每个 KNOWLEDGE_* 都必须
   并入 ALL_KNOWLEDGE"这条断言改为"必须并入 ALL_KNOWLEDGE **或**
   CONDITIONAL_KNOWLEDGE"——守卫"加了新库忘了并进去"的用意保持不变。

2. **权力结构放弃单字子串匹配**：旧 rules 用 `marker in text`，
   于是 `"国家"` 命中"国家统计局/国家利益"、`"市"` 命中"市场/城市"、
   `"部"` 命中"部分/部门" → **执行阻力恒判「低」**。
   现在改为"策展机构全称 + 地方政府文法"两条高精度通道，
   `delay_risk` 由**证据里出现的最低执行层**决定，而不是发文机关。
   因此 `国务院 → 低` 变成 `国务院 → 中`（发文到落地之间隔着部委与省市），
   而 `国务院 + 各市水务局 → 高`（新增的关键断言）。

3. **多路径推演不再给数字概率**：黑天鹅那个固定 5-10% 尤其不能有——
   给它数字等于声称视线覆盖了视野之外。现在 `probability` 恒为 None，
   并带 `probability_note` 说明理由。字段名同时与前端对齐。
"""

import re
import unittest

from yuanjian_app import knowledge_base


class KnowledgeConstantsTests(unittest.TestCase):
    def test_every_knowledge_block_is_reachable_by_the_injector(self):
        """所有 KNOWLEDGE_* 文本块都必须能被注入，否则模型永远看不到它。

        常量模块最容易出的错就是"加了一个新库、忘了并进去"，而且不会有任何报错。
        v1.3 起注入分两档：通用（ALL_KNOWLEDGE）与条件（CONDITIONAL_KNOWLEDGE），
        所以两者任一命中即可 —— 但**必须命中一个**。
        """
        blocks = {
            name: value
            for name, value in vars(knowledge_base).items()
            if name.startswith("KNOWLEDGE_") and isinstance(value, str)
        }

        self.assertGreaterEqual(len(blocks), 8, "知识块数量明显变少了")
        conditional = "".join(knowledge_base.CONDITIONAL_KNOWLEDGE.values())
        for name, value in blocks.items():
            self.assertTrue(value.strip(), f"{name} 是空的")
            reachable = (
                value in knowledge_base.ALL_KNOWLEDGE
                or value in conditional
                or value in knowledge_base.KNOWLEDGE_STATUS
            )
            # KNOWLEDGE_STATUS 不是文本注入，单独核对（见下一条断言）
            self.assertTrue(
                reachable or name in knowledge_base.KNOWLEDGE_STATUS,
                f"{name} 既不在 ALL_KNOWLEDGE 也不在 CONDITIONAL_KNOWLEDGE",
            )

    def test_每个知识块都有状态与反证方向(self):
        """审计 2.7：假设必须带状态机，否则会以"必须遵守的判断逻辑"的身份注入。"""
        blocks = [
            name
            for name in vars(knowledge_base)
            if name.startswith("KNOWLEDGE_")
            and isinstance(getattr(knowledge_base, name), str)
        ]

        for name in blocks:
            with self.subTest(block=name):
                self.assertIn(name, knowledge_base.KNOWLEDGE_STATUS, f"{name} 没有状态条目")
                meta = knowledge_base.KNOWLEDGE_STATUS[name]
                self.assertIn(meta["state"], ("unverified", "supported_by_source"))
                self.assertTrue(meta["limitation"].strip(), f"{name} 没写反证方向")

    def test_rule_tables_have_the_fields_the_functions_read(self):
        for key, config in knowledge_base.LEADING_INDICATOR_PATTERNS.items():
            self.assertEqual(
                sorted(config), ["keywords", "risk_boost", "signal"], f"{key} 字段不齐"
            )
            self.assertIsInstance(config["risk_boost"], float)
            self.assertTrue(config["keywords"], f"{key} 没有关键词")

        # v1.3：规则表不再有 markers —— 机构识别改由策展全称清单 + 地方政府文法完成
        for key, rule in knowledge_base.POWER_STRUCTURE_RULES.items():
            self.assertEqual(
                sorted(rule),
                ["execution_layer", "risk_of_delay", "veto_power"],
                f"{key} 字段不齐",
            )

    def test_假设注入文本本身声明了它不是事实(self):
        block = knowledge_base.hypothesis_block("某市水务招标公告", "")

        self.assertIn("候选假设，不是事实", block)
        self.assertIn("证据优先", block)
        self.assertIn("不得为了让结论符合假设而裁剪证据", block)
        # 通用注入里应当带上八个库的状态与限制（天外实体侧是条件注入，不在其中）
        for name in knowledge_base.ALL_KNOWLEDGE_BLOCK_NAMES:
            if name == "KNOWLEDGE_EXTRATERRESTRIAL":
                continue
            meta = knowledge_base.KNOWLEDGE_STATUS[name]
            with self.subTest(block=name):
                self.assertIn(meta["state"], block)
                self.assertIn(meta["limitation"], block)

    def test_天外实体侧注入时也带上状态与限制(self):
        block = knowledge_base.hypothesis_block("UAP 解密文件公布", "")

        meta = knowledge_base.KNOWLEDGE_STATUS["KNOWLEDGE_EXTRATERRESTRIAL"]
        self.assertIn(meta["state"], block)
        self.assertIn(meta["limitation"], block)

    def test_旧措辞必须消失(self):
        """「必须用以下逻辑判断」正是审计批的那句——它在放大用户的模型。"""
        block = knowledge_base.hypothesis_block("某市水务招标公告", "")

        self.assertNotIn("必须用以下逻辑判断", block)

    def test_天外实体侧只在相关事件上注入(self):
        plain = knowledge_base.hypothesis_block("某市水务局发布招标公告", "预算1200万元")
        related = knowledge_base.hypothesis_block("五角大楼公布 UAP 解密文件", "")

        self.assertNotIn("天外实体侧", plain)
        self.assertIn("天外实体侧", related)
        # 它是"按需注入"，注入时要说明这一点，避免被当成默认透镜
        self.assertIn("不是默认透镜", related)

    def test_每个知识块名字都在可核对清单里(self):
        for name in knowledge_base.ALL_KNOWLEDGE_BLOCK_NAMES:
            self.assertIsInstance(getattr(knowledge_base, name), str)


class LeadingIndicatorTests(unittest.TestCase):
    def test_matches_a_known_pattern_and_reports_its_boost(self):
        matches = knowledge_base.detect_leading_indicators("央行宣布降息 25 个基点", "")

        keys = {item["pattern"] for item in matches}
        self.assertIn("rate_change", keys)
        rate = next(item for item in matches if item["pattern"] == "rate_change")
        self.assertEqual(rate["risk_boost"], 0.15)
        self.assertIn("3-9", rate["signal"])

    def test_text_is_taken_from_title_and_summary_together(self):
        self.assertTrue(knowledge_base.detect_leading_indicators("标题", "本次为公开征求意见"))
        self.assertTrue(knowledge_base.detect_leading_indicators("三地入选改革试点", ""))

    def test_matching_is_case_insensitive(self):
        lower = knowledge_base.detect_leading_indicators("cpi 同比回落", "")
        upper = knowledge_base.detect_leading_indicators("CPI 同比回落", "")

        self.assertEqual(
            [item["pattern"] for item in lower], [item["pattern"] for item in upper]
        )
        self.assertTrue(lower)

    def test_multiple_patterns_can_match_at_once(self):
        matches = knowledge_base.detect_leading_indicators("关税调整与降准同日公布", "")
        keys = {item["pattern"] for item in matches}

        self.assertIn("trade_action", keys)
        self.assertIn("rate_change", keys)

    def test_unrelated_text_matches_nothing(self):
        self.assertEqual(knowledge_base.detect_leading_indicators("今天天气不错", ""), [])

    def test_合计权重有上限(self):
        """8 条模式可以同时命中，不封顶就有 0.87 的推力 —— 那就成了"关键词越多概率越高"。"""
        every_pattern = " ".join(
            kw
            for config in knowledge_base.LEADING_INDICATOR_PATTERNS.values()
            for kw in config["keywords"]
        )
        matches = knowledge_base.detect_leading_indicators(every_pattern, "")

        self.assertGreaterEqual(len(matches), 6)
        raw = sum(item["risk_boost"] for item in matches)
        self.assertGreater(raw, knowledge_base.MAX_LEADING_BOOST)
        self.assertEqual(
            knowledge_base.total_leading_boost(matches),
            knowledge_base.MAX_LEADING_BOOST,
        )


class RiskSignalTests(unittest.TestCase):
    def test_militant_language_is_flagged(self):
        for text in ("坚决打赢脱贫攻坚战", "不惜一切代价推进", "启动攻坚战"):
            with self.subTest(text=text):
                self.assertTrue(knowledge_base.detect_risk_signals(text, ""))

    def test_keyword_in_summary_only_is_still_flagged(self):
        self.assertTrue(knowledge_base.detect_risk_signals("普通标题", "要严防死守"))

    def test_neutral_language_is_not_flagged(self):
        self.assertFalse(knowledge_base.detect_risk_signals("市水务局发布招标公告", "预算 1200 万元"))

    def test_返回的是命中的具体关键词(self):
        """v1.3：返回值由 bool 改为命中词列表。

        理由：界面上要显示"因为哪个词"，否则用户看到"风险上调"却无从判断
        这句话凭什么。空列表表示未命中（仍可当布尔用）。
        """
        hits = knowledge_base.detect_risk_signals("坚决打赢攻坚战", "")

        self.assertIsInstance(hits, list)
        self.assertEqual(hits, ["坚决打赢", "攻坚战"])
        self.assertEqual(knowledge_base.detect_risk_signals("市水务局招标公告", "预算1200万元"), [])


class ScenarioPathTests(unittest.TestCase):
    def test_known_event_type_returns_three_labelled_paths(self):
        paths = knowledge_base.generate_scenario_paths("policy", ["水利部"], "政策文本")

        self.assertEqual(len(paths), 3)
        self.assertEqual(
            [item["path_type"] for item in paths],
            ["most_likely", "secondary", "black_swan"],
        )
        for item in paths:
            self.assertEqual(
                sorted(item),
                ["description", "label", "path_type", "probability", "probability_note", "trigger"],
            )
            self.assertTrue(item["description"].strip())
            self.assertTrue(item["trigger"].strip())

    def test_不给数字概率(self):
        """v1.3：本机模板无权给路径赋概率。

        旧版给三条路径分别写 60-70% / 20-30% / 5-10%。后两个数字是编的；
        黑天鹅那个尤其不能有 —— 给它数字等于声称视线覆盖了视野之外。
        """
        paths = knowledge_base.generate_scenario_paths("policy", ["水利部"], "")

        for item in paths:
            self.assertIsNone(item["probability"], item["path_type"])
            self.assertTrue(item["probability_note"].strip())
        swan = next(item for item in paths if item["path_type"] == "black_swan")
        self.assertIn("视线之外", swan["probability_note"])

    def test_不再出现编造的数字区间(self):
        paths = knowledge_base.generate_scenario_paths("monetary", ["央行"], "")
        blob = " ".join(str(item) for item in paths)

        for fabricated in ("5-10%", "60-70%", "55-65%", "20-30%", "25-35%"):
            self.assertNotIn(fabricated, blob, f"仍然在编造概率区间：{fabricated}")

    def test_pusher_comes_from_the_first_institution(self):
        paths = knowledge_base.generate_scenario_paths("policy", ["某市发改委"], "")

        self.assertIn("某市发改委", paths[0]["description"])

    def test_missing_institutions_use_a_readable_fallback(self):
        paths = knowledge_base.generate_scenario_paths("policy", [], "")

        self.assertIn("事件发起方", paths[0]["description"])
        self.assertNotIn("None", paths[0]["description"])

    def test_unknown_event_type_falls_back_to_generic_paths(self):
        paths = knowledge_base.generate_scenario_paths("不存在的类型", ["甲"], "")

        self.assertEqual(len(paths), 3)
        self.assertEqual(
            [item["path_type"] for item in paths],
            ["most_likely", "secondary", "black_swan"],
        )

    def test_字段名与前端渲染器一致(self):
        """v1.2 的缺陷：后端给 label/path/probability("60-70%")，
        前端读 path_type/description/probability(数字) —— 两边对不上，
        于是「多路径推演」面板永远渲染不出来。这条断言锁住字段名。"""
        frontend = (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "src" / "yuanjian_app" / "static" / "js" / "views" / "cluster.js"
        ).read_text(encoding="utf-8")

        for field in ("path_type", "description", "trigger", "probability_note", "label"):
            self.assertIn(f"s.{field}", frontend, f"前端没有读 {field}")
        self.assertIn("不给概率", frontend)


class PowerStructureTests(unittest.TestCase):
    # ── 旧代码的四个误判：必须消失 ──────────────────────────
    def test_裸国家二字不再触发中央级(self):
        """旧 markers 含 "国家"，而"国家统计局/国家利益/国家重点"到处都是。"""
        for text in ("国家利益受到关注", "国家重点工程名单", "国家级开发区"):
            with self.subTest(text=text):
                self.assertEqual(knowledge_base.analyze_power_structure([], text)["rule"], "unknown")

    def test_市场城市地区不再触发地方级(self):
        """旧 markers 含单字 "市"/"区"，于是"市场/城市/地区"全部命中。"""
        for text in ("部分城市市场活跃", "某地区发展", "城市更新行动"):
            with self.subTest(text=text):
                self.assertEqual(knowledge_base.analyze_power_structure([], text)["rule"], "unknown")

    def test_部门部分不再触发部委级(self):
        for text in ("部分部门已完成部署", "全部部署到位"):
            with self.subTest(text=text):
                self.assertEqual(knowledge_base.analyze_power_structure([], text)["rule"], "unknown")

    # ── 真实机构必须仍能识别 ────────────────────────────────
    def test_真实机构名仍能识别(self):
        cases = {
            "国务院印发方案": ("central_direct", "国务院"),
            "水利部发布通知": ("ministry_lead", "水利部"),
            "国家统计局公布数据": ("ministry_lead", "国家统计局"),
            "河源市水务局发布招标公告": ("local_lead", "河源市水务局"),
            "广东省人民政府办公厅印发": ("local_lead", "广东省人民政府"),
            "海关总署发布公告": ("vertical_agency", "海关总署"),
            "央行宣布降息": ("vertical_agency", "央行"),
        }
        for text, (rule, org) in cases.items():
            with self.subTest(text=text):
                result = knowledge_base.analyze_power_structure([], text)
                self.assertEqual(result["rule"], rule)
                self.assertIn(org, result["matched_orgs"])

    def test_中央发文本身不再等于执行阻力低(self):
        """国务院的发文要经部委→省→市县，中间隔着一整条执行链。"""
        result = knowledge_base.analyze_power_structure([], "国务院印发方案")

        self.assertEqual(result["rule"], "central_direct")
        self.assertEqual(result["delay_risk"], "中")

    def test_核心修正_中央发文加地方落地判高阻力(self):
        """这是审计七步#7 的正题：旧代码只看发文方，于是阻力"恒判低"。

        方法论的核心恰恰是"改革威胁执行者利益就会停" —— 一件事由国务院发文、
        但明确要求各市水务局落实，阻力在落实那一层，不在发文那一层。
        """
        result = knowledge_base.analyze_power_structure(
            [], "国务院印发方案，要求各市水务局于年底前落实"
        )

        self.assertEqual(result["rule"], "central_direct")
        self.assertEqual(result["delay_risk"], "高", "执行阻力又按发文机关判了")
        self.assertIn("各市水务局", result["matched_orgs"])

    def test_只有垂直管理机构才判低阻力(self):
        result = knowledge_base.analyze_power_structure([], "海关总署发布公告")

        self.assertEqual(result["delay_risk"], "低")
        self.assertIn("垂直", result["basis"])

    def test_delay_risk_来自最低执行层而不是发文层(self):
        """同一个发文机关，加上地方执行层之后阻力必须上升。"""
        without = knowledge_base.analyze_power_structure([], "国务院印发方案")
        with_local = knowledge_base.analyze_power_structure(
            [], "国务院印发方案，由河源市水务局具体实施"
        )
        rank = {"低": 0, "中": 1, "高": 2, "未知": -1}

        self.assertGreater(rank[with_local["delay_risk"]], rank[without["delay_risk"]])

    def test_机构名出现在参数列表里也算(self):
        result = knowledge_base.analyze_power_structure(["河源市水务局"], "")

        self.assertEqual(result["rule"], "local_lead")
        self.assertEqual(result["delay_risk"], "高")

    def test_判读依据可核对(self):
        """用户要能看到"凭什么这么判"，否则规则出错时无从发现。"""
        result = knowledge_base.analyze_power_structure([], "河源市水务局发布招标公告")

        self.assertIn("河源市水务局", result["basis"])
        self.assertIn("执行层", result["basis"])

    def test_unknown_structure_says_so_instead_of_guessing(self):
        result = knowledge_base.analyze_power_structure([], "行业协会发布报告")

        self.assertEqual(result["rule"], "unknown")
        self.assertEqual(result["delay_risk"], "未知")
        self.assertEqual(result["execution_layer"], "待确认")
        self.assertEqual(result["matched_orgs"], [])
        self.assertIn("不猜", result["basis"])

    def test_不再用单字子串匹配机构(self):
        """机械守卫：源码里不允许再出现 `marker in text` 式的单字机构匹配。"""
        source = (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "src" / "yuanjian_app" / "knowledge_base.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn('"国家", "部"', source)
        # 裸的单字标记不允许再出现在机构判定里
        self.assertIsNone(re.search(r'"(?:部|市|区|县|省)"\s*,\s*"', source))


if __name__ == "__main__":
    unittest.main()
