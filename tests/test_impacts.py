import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.forecasts import ForecastService
from yuanjian_app.impacts import ImpactService, recompute_personal_impacts
from yuanjian_app.interests import InterestService
from yuanjian_app.judgments import build_public_bundle


class ImpactServiceTests(unittest.TestCase):
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

    def add_judgment(
        self,
        suffix,
        evidence_level,
        confidence=0.9,
        urgent=True,
        delay_risk="高",
        provider="local",
        cluster_title="医保政策调整",
    ):
        cluster_id = f"C-{suffix}"
        judgment_id = f"J-{suffix}"
        timestamp = "2026-08-11T08:00:00Z"
        gyw = {
            "stakeholders": "推动方：医保局；阻力方：财政、地方执行",
            "constraints": "资源约束：医保基金、财政补贴",
            "least_resistance_path": "最小阻力路径：试点城市先行",
            "counter_evidence": "反对证据：基金穿底风险",
            "leading_indicators": "领先指标：试点城市名单",
            # v1.4（R-08）：可观测信号**必须事件级特化** —— 至少一条要含本事件
            # 特有的实体（机构名/地名/数字），或与事件标题共享一段 ≥4 字的连续
            # 片段。夹具的信号原先只是类别模板短语（"试点城市名单"），
            # 在新闸门下会被正确地拦在账本之外。这里让它指向本事件本身。
            "observable_signals": ["医保政策调整的报销比例下调文件公布"],
            # v1.5：`judgment_local` 每条研判都会落 gyw.risk_signal_hit（未命中=空表）。
            # 夹具保持同形，否则按新契约会被当成"缺字段"。
            "risk_signal_hit": [],
        }
        if delay_risk is not None:
            # v1.5（改动 A）：L4 现在**必须有结构性抓手**（执行摩擦或风险信号）。
            # `judgment_local` 每条研判都会落 gyw.power_structure；夹具原先缺这一项，
            # 在新纪律下会被正确地降为 L3 —— 而那验不到"强 E3 可达 L4"这条契约。
            # 这里补上真实产物形状：发文在部委层（rule=ministry_lead），
            # delay_risk 取**证据中出现的最低执行层**（默认"高"=要落到市县执行）。
            # 传 delay_risk=None 表示"研判里没有结构化产物"，用于验证结构闸的降级路径。
            gyw["power_structure"] = {
                "rule": "ministry_lead",
                "execution_layer": "省级对口部门和市县执行",
                "veto_analysis": "省级有变通空间，市县有执行裁量权",
                "delay_risk": delay_risk,
                "matched_orgs": ["国家医疗保障局"],
                "basis": "夹具：部委发文，落到市县执行；delay 取最低执行层。",
            }
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
            "gyw": gyw,
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
                    cluster_title,
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
                    provider,
                    f"hash-{suffix}",
                    json.dumps(result, ensure_ascii=False),
                    timestamp,
                ),
            )
        return cluster_id, judgment_id

    def test_e1_is_capped_at_l3_while_strong_e3_can_reach_l4(self):
        low_cluster, low_judgment = self.add_judgment("low", "E1")
        high_cluster, high_judgment = self.add_judgment("high", "E3")

        low = self.service.map_judgment(low_cluster, low_judgment)[0]
        high = self.service.map_judgment(high_cluster, high_judgment)[0]

        self.assertIn(low["alert_level"], {"L1", "L2", "L3"})
        self.assertEqual(high["alert_level"], "L4")
        # v1.5：components 新增 4 个**回溯字段**（不参与 base_score，只用于重算与回溯）：
        # 利益侧基准 importance_base、事件侧结构强度 structural_intensity，以及结构信号
        # 原值 structural_rule / delay_risk；v1.5 收尾再加 structure_source（结构是谁给的）。
        # 契约变了，断言随之更新（不是放宽）。
        self.assertEqual(
            set(high["components"]),
            {
                "evidence", "confidence", "importance", "exposure", "urgency",
                "personal_relevance",
                "importance_base", "structural_intensity",
                "structural_rule", "delay_risk", "structure_source",
            },
        )
        self.assertAlmostEqual(high["components"]["evidence"], 0.75)
        # 结构信号如实进档，且 importance 是「利益侧基准 × 事件侧强度」的乘积（d=高 → 1.0）。
        self.assertEqual(high["components"]["delay_risk"], "高")
        self.assertAlmostEqual(high["components"]["importance_base"], 1.0)
        self.assertAlmostEqual(high["components"]["structural_intensity"], 1.0)
        self.assertAlmostEqual(high["components"]["importance"], 1.0)

    def test_l4_requires_structural_signal(self):
        """v1.5（改动 A）：L4 结构闸 —— 只有分数够、**且**在结构上有抓手
        （执行摩擦 delay∈{高,中} 或已出现风险信号）才放行。

        同一份强 E3 研判：有结构化产物 → L4；研判里没有结构化产物（missing）
        → 保守降为 L3，并在 components_json 留下降级依据以便回溯。
        """
        with_signal = self.service.map_judgment(*self.add_judgment("s-ok", "E3"))[0]
        missing = self.service.map_judgment(
            *self.add_judgment("s-missing", "E3", delay_risk=None)
        )[0]

        self.assertEqual(with_signal["alert_level"], "L4")
        self.assertEqual(missing["alert_level"], "L3")
        self.assertNotIn("l4_downgraded_by", with_signal["components"])
        self.assertEqual(
            missing["components"].get("l4_downgraded_by"), "no_structural_signal"
        )
        self.assertIsNone(missing["components"].get("l4_gate_delay_risk"))
        # 结构从哪来必须可回溯：本地研判自带 → 'judgment'；真的没有 → 'absent'。
        self.assertEqual(with_signal["components"]["structure_source"], "judgment")
        self.assertEqual(missing["components"]["structure_source"], "absent")

    def test_remote_judgment_without_power_structure_is_backfilled_locally(self):
        """③：远程 provider 的契约里没有 `power_structure`，缺了就用**本机同一个
        规则引擎**补算 —— 否则用户花额度换来的远程升级版会被 L4 结构闸**系统性
        降为 L3**（等于花钱买降级）。

        判据必须是同一个函数：这里让证据里出现「河源市水务局」，它应当被
        `analyze_power_structure` 认成地方执行层 → delay=高 → 结构闸放行 → L4。
        """
        cluster_title = "河源市水务局发布医保政策调整公告"
        remote = self.service.map_judgment(
            *self.add_judgment(
                "remote", "E3", delay_risk=None,
                provider="deepseek_chat", cluster_title=cluster_title,
            )
        )[0]

        self.assertEqual(remote["components"]["structure_source"], "local_backfill")
        self.assertEqual(remote["components"]["delay_risk"], "高")
        self.assertEqual(remote["components"]["structural_rule"], "local_lead")
        self.assertNotIn("l4_downgraded_by", remote["components"])
        self.assertEqual(remote["alert_level"], "L4")
        # 候选卡与定级必须看到**同一份**结构（否则"分级按补算、卡片按缺失"自相矛盾）
        self.assertEqual(remote["candidate"]["delay_risk"], "高")
        self.assertEqual(remote["candidate"]["power_structure_rule"], "local_lead")

    def test_remote_backfill_does_not_invent_a_structure_it_cannot_see(self):
        """补算不许编：证据里没有可识别机构时，如实回「未知」并按结构闸降为 L3。

        这条闸同时挡住两种跑偏 —— "为了保住 L4 就随手给个结构"，
        以及"远程一律降级"（这里降级是**因为它真的没有结构抓手**，不是因为它远程）。
        """
        remote = self.service.map_judgment(
            *self.add_judgment("remote-nothing", "E3", delay_risk=None,
                               provider="deepseek_chat")
        )[0]

        self.assertEqual(remote["components"]["structure_source"], "local_backfill")
        self.assertEqual(remote["components"]["delay_risk"], "未知")
        self.assertEqual(remote["components"]["structural_rule"], "unknown")
        self.assertEqual(remote["alert_level"], "L3")
        self.assertEqual(
            remote["components"]["l4_downgraded_by"], "no_structural_signal"
        )

    def test_recompute_is_a_no_op_on_rows_written_by_map_judgment(self):
        """回填与新事件走**同一个定级入口**，所以对新写出来的行必须一个字都不用改。

        若这条失败，说明两条路已经漂移 —— 用户会看到"回填出来的档位"和"新算的
        档位"对同一类事件给出不同答案。
        """
        self.service.map_judgment(*self.add_judgment("idem", "E3"))
        self.service.map_judgment(
            *self.add_judgment("idem-remote", "E3", delay_risk=None,
                               provider="deepseek_chat",
                               cluster_title="河源市水务局发布医保调整公告")
        )

        report = recompute_personal_impacts(self.database)

        self.assertEqual(report["updated"], 0)
        self.assertEqual(report["level_changed"], 0)
        self.assertEqual(report["unchanged"], report["total"])
        self.assertGreater(report["total"], 0)

    def test_structural_intensity_makes_importance_dynamic(self):
        """v1.5（改动 B）：importance 不再恒定 —— 同一利益对象，事件侧结构强度不同，
        有效 importance 与最终分数随之变化（利益侧基准不变，仍可回溯）。"""
        weak = self.service.map_judgment(
            *self.add_judgment("d-zhong", "E3", delay_risk="中")
        )[0]
        strong = self.service.map_judgment(
            *self.add_judgment("d-gao", "E3", delay_risk="高")
        )[0]

        self.assertAlmostEqual(weak["components"]["importance_base"], 1.0)
        self.assertAlmostEqual(strong["components"]["importance_base"], 1.0)
        self.assertAlmostEqual(weak["components"]["structural_intensity"], 0.95)
        self.assertAlmostEqual(strong["components"]["structural_intensity"], 1.0)
        self.assertLess(
            weak["components"]["importance"], strong["components"]["importance"]
        )
        self.assertLess(weak["impact_score"], strong["impact_score"])

    def test_private_mapping_never_changes_public_evidence_bundle(self):
        cluster_id, judgment_id = self.add_judgment("privacy", "E3")
        public_cluster = {
            "cluster_id": cluster_id,
            "title": "医保政策调整",
            "summary": "公开消息",
            "evidence_level": "E3",
            "categories": ["health"],
        }
        public_items = [
            {
                "source_id": "S-1",
                "title": "公开通知",
                "summary": "报销规则调整",
                "canonical_url": "https://news.example/policy",
                "published_at": "2026-08-11T00:00:00Z",
            }
        ]
        before = build_public_bundle(public_cluster, public_items).to_public_dict()

        self.service.map_judgment(cluster_id, judgment_id)
        after = build_public_bundle(public_cluster, public_items).to_public_dict()

        self.assertEqual(before, after)
        serialized = json.dumps(after, ensure_ascii=False)
        self.assertNotIn(self.health["name"], serialized)

    def test_l4_candidate_waits_for_the_user_instead_of_auto_confirming(self):
        """v1.2 用户决策：**L4 一律不自动确认**，必须由本人选概率。

        旧行为（P1 引入）是 map_judgment 阶段就用概率区间中值自动入账 ——
        用户从未选过概率，账本里却留下了一条"已确认"记录。
        审计指出这与 README「候选预测必须人工选择固定概率后，才能进入不可变
        预测账本」的承诺直接冲突，用户选择只对 L4 恢复人工门槛。
        """
        cluster_id, judgment_id = self.add_judgment("candidate", "E3")
        impact = self.service.map_judgment(cluster_id, judgment_id)[0]

        # 夹具必须产出 L4，否则这条测试验的不是 L4 的人工门槛。
        self.assertEqual(
            impact["alert_level"], "L4",
            "夹具应产出 L4 才能验证本条；若这里是 L3，说明删除范围过宽",
        )

        candidate = self.service.candidate_forecast(impact["impact_id"])
        # 第二波起：区间**中心**来自「基准率 + 信号」，**宽度**只由 E 级决定。
        # 所以不再断言旧的 E3 固定带 (0.55, 0.78) —— 那是被批掉的"概率=转载量"口径。
        # 改断言结构性事实：中心落在区间内、区间非退化、且结算标准非空。
        low, high = candidate["probability_low"], candidate["probability_high"]
        self.assertLess(low, high, "区间不能退化成一个点")
        self.assertTrue(0.0 <= low <= high <= 1.0)
        self.assertTrue(candidate["resolution_criteria"])

        # 映射阶段**不**自动入账（这是本次行为变更的核心）。
        forecasts, total = self.forecasts.list_forecasts()
        self.assertEqual(total, 0, "L4 候选不得在映射阶段自动进入账本")
        self.assertEqual(forecasts, [])

        # 手动确认仍然可用：固定档位、幂等。
        with self.assertRaisesRegex(ValueError, "固定档位"):
            self.service.confirm_candidate(impact["impact_id"], 0.73)

        # 首次确认走 create_forecast，返回 {forecast_id, version, duplicate}；
        # 概率要从账本读，不要假定返回值里有 probability。
        confirmed = self.service.confirm_candidate(impact["impact_id"], 0.65)
        self.assertEqual(confirmed["version"], 1)
        rows, _ = self.forecasts.list_forecasts()
        self.assertEqual(rows[0]["probability"], 0.65)
        # 二次确认走幂等分支（返回已存在的那条）。
        again = self.service.confirm_candidate(impact["impact_id"], 0.65)
        self.assertEqual(again["version"], 1, "重复确认同一档位应幂等")
        _, total_after = self.forecasts.list_forecasts()
        self.assertEqual(total_after, 1)

    def test_pending_candidates_surfaces_gyw_framework_from_judgment(self):
        """The Action Home deep-dive card reads candidate.gyw to render
        the 《登高望远》 stakeholder/constraint/least-resistance/counter/
        leading-indicator analysis. pending_candidates must surface the
        gyw sub-structure stored in the judgment's content_json."""
        cluster_id, judgment_id = self.add_judgment("gyw", "E1")
        self.service.map_judgment(cluster_id, judgment_id)

        candidates = self.service.pending_candidates()

        self.assertTrue(candidates)
        candidate = candidates[0]
        self.assertEqual(candidate["judgment_id"], judgment_id)
        self.assertEqual(candidate["cluster_id"], cluster_id)
        self.assertIsInstance(candidate["gyw"], dict)
        for field in (
            "stakeholders",
            "constraints",
            "least_resistance_path",
            "counter_evidence",
            "leading_indicators",
        ):
            self.assertTrue(
                candidate["gyw"].get(field, "").strip(),
                f"gyw.{field} missing or empty in pending_candidates output",
            )
        # And the source flag must be 'judgment' (real engine output),
        # not 'legacy-backfill'.
        self.assertEqual(candidate["gyw_source"], "judgment")
        # Also surface fact_summary / actors / causal_chain so the home
        # page can show the judgment's plain-language summary.
        self.assertTrue(candidate["fact_summary"])
        self.assertTrue(candidate["actors"])

    def test_pending_candidates_backfills_gyw_for_legacy_judgments(self):
        """Judgments that pre-date the GYW schema (v0.8 and earlier) have
        no gyw sub-structure in content_json. pending_candidates must
        fill the gap from a per-category template and mark gyw_source as
        'legacy-backfill' so the home page can label it accurately.
        The judgment table is immutable by design (UPDATE/DELETE triggers
        abort), so we bypass add_judgment() and INSERT a v0.8-shaped
        judgment directly via SQL — content_json missing the gyw key
        simulates pre-GYW data, and INSERT does not run validate_judgment.
        """
        cluster_id = "C-legacy"
        judgment_id = "J-legacy"
        timestamp = "2026-08-10T00:00:00Z"
        # Register interest + cluster + impact directly so the impact
        # table is consistent with the candidate we'll fetch.
        self.interests.create_object({
            "name": "测试现金流利益", "category": "cashflow",
            "importance": 3, "privacy_level": "P2",
        })
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO event_clusters(
                    cluster_id,title,summary,first_seen_at,last_seen_at,
                    evidence_level,evidence_hash,categories_json,
                    latest_judgment_id,created_at,updated_at
                ) VALUES (?,?, '',?,?,?,?,?,?,?,?)
                """,
                (cluster_id, "升级前旧 judgment 测试", timestamp, timestamp,
                 "E3", "hash-legacy", '["cashflow"]', judgment_id,
                 timestamp, timestamp),
            )
            # v0.8-shaped judgment — no gyw key.
            legacy_content = {
                "fact_summary": "升级前旧 judgment",
                "actors": [], "causal_chain": [], "uncertainties": [],
                "horizons": ["未来30天"],
                "probability_low": 0.4, "probability_high": 0.6, "confidence": 0.5,
                "supporting_source_ids": ["S-1"], "counter_source_ids": [],
                "up_triggers": [], "down_triggers": [],
                "impact_categories": ["cashflow"],
            }
            conn.execute(
                "INSERT INTO judgments VALUES (?,?,?,?,?,?)",
                (judgment_id, cluster_id, "local", "hash-legacy",
                 json.dumps(legacy_content, ensure_ascii=False), timestamp),
            )
            conn.execute(
                """
                INSERT INTO personal_impacts(
                    impact_id,cluster_id,judgment_id,interest_id,impact_score,
                    alert_level,components_json,reason,candidate_json,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("P-legacy", cluster_id, judgment_id, self.health["object_id"],
                 0.5, "L3", '{"confidence":0.5}', "旧 judgment 触发",
                 json.dumps({"title": "测试候选", "window_end": "2026-09-15"},
                            ensure_ascii=False),
                 timestamp, timestamp),
            )

        candidates = self.service.pending_candidates()
        candidate = next(c for c in candidates if c["judgment_id"] == judgment_id)
        self.assertEqual(candidate["gyw_source"], "legacy-backfill")
        for field in (
            "stakeholders",
            "constraints",
            "least_resistance_path",
            "counter_evidence",
            "leading_indicators",
        ):
            self.assertTrue(
                candidate["gyw"].get(field, "").strip(),
                f"backfilled gyw.{field} empty for legacy judgment",
            )

    def test_pending_candidates_uses_category_template(self):
        """Legacy judgments with a known category (work) get the work
        template; the template choice is driven by interest_id.category.
        Sanity check that the right per-category branch fires."""
        work = self.interests.create_object(
            {"name": "测试工作利益", "category": "work",
             "importance": 3, "privacy_level": "P2"}
        )
        cluster_id = "C-work"
        judgment_id = "J-work"
        timestamp = "2026-08-10T00:00:00Z"
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO event_clusters(
                    cluster_id,title,summary,first_seen_at,last_seen_at,
                    evidence_level,evidence_hash,categories_json,
                    latest_judgment_id,created_at,updated_at
                ) VALUES (?,?, '',?,?,?,?,?,?,?,?)
                """,
                (cluster_id, "工作类别测试", timestamp, timestamp,
                 "E2", "hash-work", '["work"]', judgment_id,
                 timestamp, timestamp),
            )
            content = {
                "fact_summary": "测试 work 类别",
                "actors": [], "causal_chain": [], "uncertainties": [],
                "horizons": ["未来30天"],
                "probability_low": 0.4, "probability_high": 0.6, "confidence": 0.5,
                "supporting_source_ids": ["S-1"], "counter_source_ids": [],
                "up_triggers": [], "down_triggers": [],
                "impact_categories": ["work"],
            }
            conn.execute(
                "INSERT INTO judgments VALUES (?,?,?,?,?,?)",
                (judgment_id, cluster_id, "local", "hash-work",
                 json.dumps(content, ensure_ascii=False), timestamp),
            )
            conn.execute(
                """
                INSERT INTO personal_impacts(
                    impact_id,cluster_id,judgment_id,interest_id,impact_score,
                    alert_level,components_json,reason,candidate_json,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("P-work", cluster_id, judgment_id, work["object_id"],
                 0.5, "L3", '{"confidence":0.5}', "work 测试",
                 json.dumps({"title": "测试候选", "window_end": "2026-09-15"},
                            ensure_ascii=False),
                 timestamp, timestamp),
            )

        candidates = self.service.pending_candidates()
        candidate = next(c for c in candidates if c["judgment_id"] == judgment_id)
        self.assertEqual(candidate["gyw_source"], "legacy-backfill")
        # The work template mentions '分阶段执行' as the least-resistance
        # path — that confirms the work branch was picked, not the default.
        self.assertIn("分阶段执行", candidate["gyw"]["least_resistance_path"])

    def test_backfill_gyw_defaults_for_unknown_category(self):
        """_backfill_gyw returns _GYW_BACKFILL_DEFAULT when no template
        matches the category. Direct unit test (not through the DB) — the
        default fallback path is hard to exercise via interests because
        the interests schema whitelists valid categories."""
        from yuanjian_app.impacts import _backfill_gyw
        analysis = _backfill_gyw("safety")
        for field in (
            "stakeholders",
            "constraints",
            "least_resistance_path",
            "counter_evidence",
            "leading_indicators",
        ):
            self.assertTrue(analysis.get(field, "").strip())
        # A known category returns the per-category template, not the default.
        cashflow = _backfill_gyw("cashflow")
        self.assertIn("拨付", cashflow["least_resistance_path"])


if __name__ == "__main__":
    unittest.main()
