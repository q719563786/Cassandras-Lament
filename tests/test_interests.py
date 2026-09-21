import tempfile
import unittest
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.interests import InterestService


class InterestServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "yuanjian.db")
        self.database.initialize()
        self.service = InterestService(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_map_contains_generic_interest_filters(self):
        self.service.ensure_defaults()

        items = self.service.list_objects()

        # v1.4：新增默认项「前沿与天外」（类目 opportunity），默认项由 7 增至 8。
        # 类目集合不变（仍复用这 7 个类目，未新增类目）。
        self.assertEqual(len(items), 8)
        self.assertEqual(
            {item["category"] for item in items},
            {"health", "cashflow", "work", "policy", "family", "assets", "opportunity"},
        )
        self.assertIn("前沿与天外", {item["name"] for item in items})

    def test_create_object_validates_category_and_importance(self):
        created = self.service.create_object(
            {"name": "长期职业选择", "category": "work", "importance": 4, "privacy_level": "P2"}
        )

        self.assertEqual(created["name"], "长期职业选择")
        with self.assertRaisesRegex(ValueError, "类别"):
            self.service.create_object(
                {"name": "非法", "category": "unknown", "importance": 3, "privacy_level": "P1"}
            )

    def test_create_link_requires_existing_distinct_objects(self):
        self.service.ensure_defaults()
        items = self.service.list_objects()

        link = self.service.create_link(
            {
                "source_id": items[0]["object_id"],
                "target_id": items[1]["object_id"],
                "relationship": "影响",
                "impact_direction": "negative",
                "strength": 4,
            }
        )

        self.assertEqual(link["strength"], 4)
        with self.assertRaisesRegex(ValueError, "自身"):
            self.service.create_link(
                {
                    "source_id": items[0]["object_id"],
                    "target_id": items[0]["object_id"],
                    "relationship": "影响",
                    "impact_direction": "negative",
                    "strength": 4,
                }
            )


if __name__ == "__main__":
    unittest.main()
