from __future__ import annotations

import unittest

from bot.platforms import (
    canonical_platform,
    normalized_platform_sql,
    platform_filter_sql,
    platform_values,
)
from bot.market_plan import build_market_plan
from bot.router import _sanitize_routed_brand, route
from bot.session import SessionState


class PlatformRegistryTest(unittest.TestCase):
    def test_shared_brand_guard_removes_common_question_tails(self):
        for suffix in (
            "的生意咋样", "的生意怎么样", "的生意如何", "的生意怎样",
            "的生意好不好", "的生意是什么情况", "的经营表现怎么样",
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(_sanitize_routed_brand(f"proya{suffix}"), "proya")

    def test_user_aliases_resolve_to_canonical_commerce_platforms(self):
        for value in ("天猫", "tmall", "TM"):
            self.assertEqual(canonical_platform(value), "TM")
        for value in ("京东", "jingdong", "JD"):
            self.assertEqual(canonical_platform(value), "JD")
        for value in ("抖音", "douyin", "DY"):
            self.assertEqual(canonical_platform(value), "DY")

    def test_shared_ec_table_uses_all_physical_aliases(self):
        self.assertEqual(
            platform_values("three_platform_store_rank_monthly", "天猫"),
            ("TM", "TMALL", "天猫"),
        )
        jd = platform_filter_sql("three_platform_store_rank_monthly", "jingdong")
        self.assertIn("'JD'", jd)
        self.assertIn("'JINGDONG'", jd)
        self.assertIn("'京东'", jd)

    def test_dedicated_table_is_implicit_and_has_no_platform_predicate(self):
        self.assertEqual(
            platform_filter_sql("dy_store_ranking_BFSS_day_jiashicang", "douyin"),
            "",
        )
        with self.assertRaises(ValueError):
            platform_filter_sql("dy_store_ranking_BFSS_day_jiashicang", "tmall")

    def test_normalized_expression_maps_every_ec_alias(self):
        expression = normalized_platform_sql("three_platforms_segmented_markets_monthly")
        for value in ("TMALL", "JINGDONG", "DOUYIN"):
            self.assertIn(value, expression)
        for canonical in ("TM", "JD", "DY"):
            self.assertIn(f"THEN '{canonical}'", expression)

    def test_ksi_has_its_own_content_platform_contract(self):
        douyin = platform_filter_sql("ai_bot_media_ksi_performance", "dy")
        self.assertIn("'DOUYIN'", douyin)
        self.assertIn("'抖音'", douyin)
        red = platform_filter_sql("ai_bot_media_ksi_performance", "小红书")
        self.assertIn("'XIAOHONGSHU'", red)

    def test_market_plan_accepts_english_platform_aliases(self):
        self.assertEqual(
            build_market_plan("2026年6月 tmall Top 3品牌").platform,
            "TM",
        )
        self.assertEqual(
            build_market_plan("2026年6月 jingdong Top 3品牌").platform,
            "JD",
        )
        self.assertEqual(
            build_market_plan("2026年6月 douyin Top 3品牌").platform,
            "DY",
        )

    def test_business_routes_accept_douyin_and_jingdong_aliases(self):
        self.assertEqual(
            route("2026年6月韩束douyin生意分析", SessionState()).type,
            "douyin_business_analysis",
        )
        self.assertEqual(
            route("2026年6月韩束jingdong生意分析", SessionState()).type,
            "jd_business_analysis",
        )

    def test_all_brand_routes_strip_colloquial_question_suffixes(self):
        cases = (
            ("proya 抖音2026年2月的生意咋样", "douyin_business_analysis"),
            ("proya 京东2026年2月的生意咋样", "jd_business_analysis"),
            ("proya 天猫2026年2月的生意咋样", "default_chain"),
            ("proya 2026年2月三平台生意咋样", "three_platform_competitor_analysis"),
            ("proya 2026年2月的生意咋样", "clarify_ec_platform"),
            ("proya 2026年2月的媒体投资咋样", "media_analysis"),
        )
        for question, route_type in cases:
            with self.subTest(question=question):
                result = route(question, SessionState())
                self.assertEqual(result.type, route_type)
                self.assertEqual(result.brand, "proya")
                self.assertEqual(result.period, "2026年2月")


if __name__ == "__main__":
    unittest.main()
