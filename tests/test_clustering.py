import unittest
from datetime import datetime, timedelta, timezone

from yuanjian_app.clustering import (
    ClusterText,
    similarity,
    should_merge,
    text_features,
)


class ClusteringTests(unittest.TestCase):
    def test_text_features_keep_chinese_english_and_numbers(self):
        features = text_features("广东医保 API Cost 上调至70%，2026-08-11实施")

        self.assertIn("广东", features)
        self.assertIn("api", features)
        self.assertIn("70%", features)
        self.assertIn("2026-08-11", features)

    def test_similarity_handles_empty_and_identical_text(self):
        self.assertEqual(similarity("", ""), 0.0)
        self.assertEqual(similarity("同一条消息", "同一条消息"), 1.0)
        self.assertAlmostEqual(similarity("甲乙丙", "甲乙丁"), 1 / 3)

    def test_same_story_merges_when_number_and_subject_agree(self):
        observed_at = datetime(2026, 8, 11, 1, tzinfo=timezone.utc)
        left = ClusterText(
            title="广东调整医保报销政策",
            summary="报销比例提高至70%",
            observed_at=observed_at,
        )
        right = ClusterText(
            title="广东医保新规：报销比例升至70%",
            summary="政策本月实施",
            observed_at=observed_at + timedelta(hours=3),
        )

        decision = should_merge(left, right)

        self.assertTrue(decision.merge)
        self.assertIn("70%", decision.shared_numbers)
        self.assertGreater(decision.score, 0)

    def test_common_location_does_not_merge_unrelated_topics(self):
        observed_at = datetime(2026, 8, 11, tzinfo=timezone.utc)
        left = ClusterText("广东发布教育规划", "新增学校学位", observed_at)
        right = ClusterText("广东发布黄金消费数据", "金价与销量变化", observed_at)

        self.assertFalse(should_merge(left, right).merge)

    def test_items_more_than_72_hours_apart_do_not_merge(self):
        observed_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
        left = ClusterText("广东医保报销比例升至70%", "政策实施", observed_at)
        right = ClusterText(
            "广东医保报销比例升至70%",
            "政策实施",
            observed_at + timedelta(hours=73),
        )

        decision = should_merge(left, right)

        self.assertFalse(decision.merge)
        self.assertEqual(decision.reason, "outside_time_window")

    def test_text_features_are_cached_across_repeated_comparisons(self):
        """同一段文本在聚类里会被反复比对，特征提取必须命中缓存。

        调用模式天然重复：`should_merge()` 每比较一对就算两次
        （`similarity` 一次、`_shared_subject_features` 一次），而每个新条目
        又要把全部活跃簇重算一遍。缓存前，同一个簇的标题+摘要会被反复分词。
        """
        unique = "缓存命中验证专用文本-9102"
        text_features.cache_clear()

        first = text_features(unique)
        misses_after_first = text_features.cache_info().misses

        second = text_features(unique)

        # 相同输入返回同一个对象，且第二次没有产生新的 miss。
        self.assertIs(first, second)
        self.assertEqual(text_features.cache_info().misses, misses_after_first)

        # 空文本走同一条缓存路径，也不能每次都重新计算。
        text_features("")
        empty_misses = text_features.cache_info().misses
        text_features("")
        self.assertEqual(text_features.cache_info().misses, empty_misses)

    def test_should_merge_reuses_cached_features_for_both_sides(self):
        """一对比较里的四次特征提取只应产生两个缓存项（左右各一个）。"""
        observed_at = datetime(2026, 8, 11, 8, tzinfo=timezone.utc)
        left = ClusterText("广东医保报销比例升至70%", "政策实施", observed_at)
        right = ClusterText("广东医保报销比例升至70%", "政策已实施", observed_at + timedelta(hours=2))

        text_features.cache_clear()
        should_merge(left, right)
        after_first = text_features.cache_info().misses

        should_merge(left, right)

        self.assertEqual(text_features.cache_info().misses, after_first)


if __name__ == "__main__":
    unittest.main()
