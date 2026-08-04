from __future__ import annotations

import unittest

import pandas as pd

from data_import.topline_pipeline import classify_scope, load_rules


class ToplinePipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()

    def classify(self, app: str, ad_format: str, ait: str) -> dict:
        frame = pd.DataFrame(
            {
                "standard_app_name": [app],
                "standard_ad_format": [ad_format],
                "ait_roe": [ait],
            }
        )
        return {
            scope: classify_scope(frame, self.rules[scope]).iloc[0]
            for scope in ["overall", "xiaohongshu", "douyin"]
        }

    def test_influencer_has_priority_over_format(self):
        labels = self.classify("抖音", "Feeds", "Influencer")
        self.assertEqual(labels["overall"], "K")
        self.assertEqual(labels["douyin"], "K")

    def test_transaction_is_t_for_overall_and_douyin(self):
        labels = self.classify("抖音", "Feeds", "Transaction")
        self.assertEqual(labels["overall"], "T")
        self.assertEqual(labels["douyin"], "T")

    def test_xiaohongshu_transaction_falls_back_to_feeds(self):
        labels = self.classify("小红书", "Feeds-RTB-UD", "Transaction")
        self.assertEqual(labels["overall"], "T")
        self.assertEqual(labels["xiaohongshu"], "F")

    def test_platform_specific_labels_are_null_outside_platform(self):
        labels = self.classify("微信", "Feeds", "Awareness")
        self.assertEqual(labels["overall"], "F")
        self.assertTrue(pd.isna(labels["xiaohongshu"]))
        self.assertTrue(pd.isna(labels["douyin"]))

    def test_platform_contains_matches_combined_douyin_name(self):
        labels = self.classify("抖音/微信", "Banner", "Awareness")
        self.assertEqual(labels["douyin"], "B")

    def test_platform_specific_brand_rules(self):
        xhs = self.classify("小红书", "Opening", "Awareness")
        douyin = self.classify("抖音", "Opening", "Awareness")
        self.assertEqual(xhs["xiaohongshu"], "B")
        self.assertTrue(pd.isna(douyin["douyin"]))

    def test_search_and_unmatched(self):
        search = self.classify("小红书", "Search", "Awareness")
        keywords = self.classify("小红书", "Keywords", "Awareness")
        self.assertEqual(search["overall"], "S")
        self.assertEqual(search["xiaohongshu"], "S")
        self.assertTrue(pd.isna(keywords["overall"]))
        self.assertTrue(pd.isna(keywords["xiaohongshu"]))


if __name__ == "__main__":
    unittest.main()
