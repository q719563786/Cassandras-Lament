import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.forecasts import ForecastService
from yuanjian_app.impacts import ImpactService
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

    def add_judgment(self, suffix, evidence_level, confidence=0.9, urgent=True):
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

    def test_e1_is_capped_at_l3_while_strong_e3_can_reach_l4(self):
        low_cluster, low_judgment = self.add_judgment("low", "E1")
        high_cluster, high_judgment = self.add_judgment("high", "E3")

        low = self.service.map_judgment(low_cluster, low_judgment)[0]
        high = self.service.map_judgment(high_cluster, high_judgment)[0]

        self.assertIn(low["alert_level"], {"L1", "L2", "L3"})
        self.assertEqual(high["alert_level"], "L4")
        self.assertEqual(
            set(high["components"]),
            {"evidence", "confidence", "importance", "exposure", "urgency"},
        )
        self.assertAlmostEqual(high["components"]["evidence"], 0.75)

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

    def test_candidate_requires_human_fixed_probability_before_forecast_exists(self):
        cluster_id, judgment_id = self.add_judgment("candidate", "E3")
        impact = self.service.map_judgment(cluster_id, judgment_id)[0]

        candidate = self.service.candidate_forecast(impact["impact_id"])

        self.assertEqual(self.forecasts.list_forecasts()[0], [])
        self.assertEqual(candidate["probability_low"], 0.55)
        self.assertEqual(candidate["probability_high"], 0.78)
        self.assertTrue(candidate["resolution_criteria"])
        with self.assertRaisesRegex(ValueError, "固定档位"):
            self.service.confirm_candidate(impact["impact_id"], 0.73)
        self.assertEqual(self.forecasts.list_forecasts()[0], [])

        confirmed = self.service.confirm_candidate(impact["impact_id"], 0.65)

        self.assertEqual(confirmed["version"], 1)
        self.assertEqual(self.forecasts.list_forecasts()[0][0]["probability"], 0.65)

    def test_pending_candidates_surfaces_gyw_framework_from_judgment(self):
        """The Action Home deep-dive card reads candidate.gyw to render
        the 《登高望远》 stakeholder/constraint/least-resistance/counter/
        leading-indicator analysis. pending_candidates must surface the
        gyw sub-structure stored in the judgment's content_json."""
        cluster_id, judgment_id = self.add_judgment("gyw", "E3")
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
