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


if __name__ == "__main__":
    unittest.main()
