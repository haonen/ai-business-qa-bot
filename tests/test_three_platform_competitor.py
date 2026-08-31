from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from bot.router import route
from bot.session import SessionState
from bot.three_platform_competitor_formatter import format_three_platform_competitor_report
from bot.tools.query_three_platform_competitor import (
    _CATEGORY_COLUMN_CACHE,
    _channel_rows,
    _fetch_brand_category_slice,
    _fetch_douyin_channel_slice,
    _fetch_market_slice,
    _overall_rows,
    _query_brand_categories_with_reference_fallback,
    _query_market,
    _source_slices,
    _top_categories,
)
from bot.utils import parse_ec_period


class ThreePlatformRouteTest(unittest.TestCase):
    def test_standard_trigger_extracts_brand_and_period(self):
        routed = route(
            "请按竞品三平台生意分析模板，分析珀莱雅，2026年Q2，同比去年同期。",
            SessionState(),
        )
        self.assertEqual(routed.type, "three_platform_competitor_analysis")
        self.assertEqual(routed.brand, "珀莱雅")
        self.assertEqual(routed.period, "2026年Q2")

    def test_all_platform_natural_language_trigger(self):
        routed = route(
            "看一下珀莱雅2026年Q2在天猫、抖音、京东的GMV、份额、类目和渠道表现",
            SessionState(),
        )
        self.assertEqual(routed.type, "three_platform_competitor_analysis")
        self.assertEqual(routed.brand, "珀莱雅")

    def test_short_three_platform_business_phrase_uses_template(self):
        routed = route(
            "分析Flower Lure的三平台生意，2026年1-3月",
            SessionState(),
        )
        self.assertEqual(routed.type, "three_platform_competitor_analysis")
        self.assertEqual(routed.brand, "FlowerLure")
        self.assertEqual(routed.period, "2026年1-3月")
        self.assertEqual(routed.platform, "TTL")

    def test_three_platform_reply_resumes_platform_clarification(self):
        state = SessionState(pending_request={
            "intent": "business_platform_selection",
            "brand": "FlowerLure",
            "period": "2026年1-3月",
            "brand_aliases": ["Flower Lure"],
        })
        routed = route("三平台", state)
        self.assertEqual(routed.type, "three_platform_competitor_analysis")
        self.assertEqual(routed.brand, "FlowerLure")
        self.assertEqual(routed.period, "2026年1-3月")
        self.assertEqual(routed.platform, "TTL")

    def test_missing_inputs_use_template_specific_clarification(self):
        missing_brand = route("请按三平台生意分析模板分析2026年Q2", SessionState())
        missing_period = route("请按三平台生意分析模板分析珀莱雅", SessionState())
        self.assertEqual(missing_brand.type, "clarify_three_platform_brand")
        self.assertEqual(missing_period.type, "clarify_three_platform_period")

    def test_single_platform_request_keeps_existing_route(self):
        routed = route("生成韩束2026年6月抖音生意分析报告", SessionState())
        self.assertEqual(routed.type, "douyin_business_analysis")


class ThreePlatformSourceContractTest(unittest.TestCase):
    def setUp(self):
        _CATEGORY_COLUMN_CACHE.clear()

    @patch(
        "bot.tools.query_three_platform_competitor.english_brand_for_chinese",
        return_value="PROYA",
    )
    @patch("bot.tools.query_three_platform_competitor._query_brand_categories")
    def test_chinese_brand_retries_english_only_after_direct_query_is_empty(
        self, query_categories, reference_lookup,
    ):
        empty = pd.DataFrame(columns=["period_key", "platform", "category", "gmv"])
        matched = pd.DataFrame([{
            "period_key": "current", "platform": "TM", "category": "面霜", "gmv": 100,
        }])
        query_categories.side_effect = [
            (empty, [{"table": "monthly"}], [{
                "period": "current", "month": "2026-01", "platform": "TM",
                "source": "monthly",
            }]),
            (matched, [{"table": "monthly"}], []),
        ]

        source_brand, method, frame, sources, missing = (
            _query_brand_categories_with_reference_fallback("珀莱雅", {})
        )

        self.assertEqual(query_categories.call_args_list[0].args[0], "珀莱雅")
        self.assertEqual(query_categories.call_args_list[1].args[0], "PROYA")
        self.assertEqual(
            query_categories.call_args_list[1].kwargs["slice_keys"],
            {("current", "2026-01", "TM", "monthly")},
        )
        self.assertEqual(source_brand, "PROYA")
        self.assertEqual(method, "cn_en_reference")
        self.assertFalse(frame.empty)
        self.assertFalse(missing)
        self.assertEqual(sources[-1]["query_brand"], "PROYA")
        reference_lookup.assert_called_once_with("珀莱雅")

    @patch(
        "bot.tools.query_three_platform_competitor.english_brand_for_chinese",
        return_value="Florasis",
    )
    @patch("bot.tools.query_three_platform_competitor._query_brand_categories")
    def test_partial_chinese_hits_fill_only_missing_slices_with_english(
        self, query_categories, _reference_lookup,
    ):
        direct = pd.DataFrame([{
            "period_key": "current", "platform": "TM", "category": "彩妆", "gmv": 20,
        }])
        fallback = pd.DataFrame([{
            "period_key": "current", "platform": "DY", "category": "彩妆", "gmv": 80,
        }])
        missing = [{
            "period": "current", "month": "2026-02", "platform": "DY", "source": "monthly",
        }]
        query_categories.side_effect = [
            (direct, [{"platform": "TM"}], missing),
            (fallback, [{"platform": "DY"}], []),
        ]

        source_brand, method, frame, sources, remaining = (
            _query_brand_categories_with_reference_fallback("花西子", {})
        )

        self.assertEqual(source_brand, "Florasis")
        self.assertEqual(method, "direct+cn_en_reference")
        self.assertFalse(remaining)
        self.assertEqual(float(frame["gmv"].sum()), 100)
        self.assertEqual(len(sources), 2)
        self.assertEqual(
            query_categories.call_args_list[1].kwargs["slice_keys"],
            {("current", "2026-02", "DY", "monthly")},
        )

    @patch(
        "bot.tools.query_three_platform_competitor.english_brand_for_chinese",
        return_value="PROYA",
    )
    @patch("bot.tools.query_three_platform_competitor._query_brand_categories")
    def test_direct_brand_hit_does_not_use_reference_fallback(
        self, query_categories, reference_lookup,
    ):
        matched = pd.DataFrame([{
            "period_key": "current", "platform": "TM", "category": "面霜", "gmv": 100,
        }])
        query_categories.return_value = (matched, [], [])

        source_brand, method, frame, _, _ = (
            _query_brand_categories_with_reference_fallback("珀莱雅", {})
        )

        self.assertEqual(source_brand, "珀莱雅")
        self.assertEqual(method, "direct")
        self.assertFalse(frame.empty)
        query_categories.assert_called_once_with("珀莱雅", {})
        reference_lookup.assert_not_called()

    def test_full_month_uses_monthly_and_partial_month_uses_platform_daily(self):
        full = _source_slices("2026-06-01", "2026-06-30", "TM")
        partial = _source_slices("2026-06-01", "2026-06-18", "JD")
        self.assertEqual(full[0]["table"], "three_platform_store_rank_monthly")
        self.assertEqual(partial[0]["table"], "jd_store_ranking_selfrun_day_jiashicang")

    @patch("bot.tools.query_three_platform_competitor.fetch_df", return_value=pd.DataFrame())
    def test_monthly_uses_platform_category_level_from_category_cn(self, fetch_df):
        for platform, level in (("TM", 3), ("DY", 4), ("JD", 3)):
            item = _source_slices("2026-06-01", "2026-06-30", platform)[0]
            _fetch_brand_category_slice(brand="珀莱雅", period_key="current", item=item)
            sql = fetch_df.call_args.args[0]
            self.assertIn(
                f"SUBSTRING_INDEX(SUBSTRING_INDEX(TRIM(category_CN), '-', {level}), '-', -1)",
                sql,
            )
            self.assertIn("SUM(", sql)
            self.assertNotIn("monthly_dedup", sql)

    @patch("bot.tools.query_three_platform_competitor.fetch_df", return_value=pd.DataFrame())
    def test_monthly_brand_query_uses_confirmed_beauty_scope_and_strict_platform(self, fetch_df):
        item = _source_slices("2026-01-01", "2026-01-31", "TM")[0]
        _fetch_brand_category_slice(brand="PROYA", period_key="current", item=item)
        sql = fetch_df.call_args.args[0]
        self.assertIn("UPPER(TRIM(brand_name)) = UPPER(:brand)", sql)
        self.assertIn("UPPER(TRIM(platform)) = :platform_key", sql)
        self.assertIn("category_EN_level_1", sql)
        self.assertIn("'skincare'", sql)
        self.assertIn("'hair'", sql)
        self.assertIn("'makeup (exclude fragrance)'", sql)

    @patch("bot.tools.query_three_platform_competitor.fetch_df", return_value=pd.DataFrame())
    def test_partial_month_daily_category_is_not_split_again(self, fetch_df):
        item = _source_slices("2026-06-01", "2026-06-18", "DY")[0]
        _fetch_brand_category_slice(brand="PROYA", period_key="current", item=item)
        sql = fetch_df.call_args.args[0]
        self.assertIn("TRIM(category_level_4)", sql)
        self.assertNotIn("SUBSTRING_INDEX", sql)

    @patch("bot.tools.query_three_platform_competitor.fetch_df", return_value=pd.DataFrame())
    def test_monthly_douyin_channels_use_same_beauty_scope(self, fetch_df):
        item = _source_slices("2026-01-01", "2026-01-31", "DY")[0]
        _fetch_douyin_channel_slice(brand="PROYA", period_key="current", item=item)
        sql = fetch_df.call_args.args[0]
        self.assertIn("UPPER(TRIM(platform)) = 'DY'", sql)
        self.assertIn("category_EN_level_1", sql)
        self.assertIn("SUM(", sql)

    @patch("bot.tools.query_three_platform_competitor.fetch_df")
    def test_monthly_selects_category_cn_when_schema_is_available(self, fetch_df):
        fetch_df.side_effect = [
            pd.DataFrame({"column_name": [
                "bus_date", "platform", "brand_name", "gmv", "category_CN",
            ]}),
            pd.DataFrame([{
                "period_key": "current", "platform": "TM", "category": "面霜",
                "gmv": 100, "row_count": 1,
            }]),
        ]
        item = _source_slices("2026-01-01", "2026-01-31", "TM")[0]
        result = _fetch_brand_category_slice(
            brand="珀莱雅", period_key="current", item=item,
        )
        sql = fetch_df.call_args_list[1].args[0]
        self.assertIn(
            "SUBSTRING_INDEX(SUBSTRING_INDEX(TRIM(category_CN), '-', 3), '-', -1)",
            sql,
        )
        self.assertNotIn("TRIM(category_level_3)", sql)
        self.assertIn("COUNT(1) AS row_count", sql)
        self.assertEqual(float(result.iloc[0]["gmv"]), 100)

    @patch("bot.tools.query_three_platform_competitor.fetch_df")
    def test_monthly_defaults_to_category_cn_without_schema_access(self, fetch_df):
        fetch_df.side_effect = [
            RuntimeError("information_schema denied"),
            pd.DataFrame([{
                "period_key": "current", "platform": "TM", "category": "面霜",
                "gmv": 100, "row_count": 1,
            }]),
        ]
        item = _source_slices("2026-01-01", "2026-01-31", "TM")[0]
        result = _fetch_brand_category_slice(
            brand="珀莱雅", period_key="current", item=item,
        )
        query_sql = fetch_df.call_args_list[1].args[0]
        self.assertIn(
            "SUBSTRING_INDEX(SUBSTRING_INDEX(TRIM(category_CN), '-', 3), '-', -1)",
            query_sql,
        )
        self.assertEqual(float(result.iloc[0]["gmv"]), 100)

    @patch("bot.tools.query_three_platform_competitor.fetch_df", return_value=pd.DataFrame())
    def test_beauty_market_and_pure_mass_field_contract(self, fetch_df):
        item = {"start": "2026-06-01", "end": "2026-06-30", "full_month": True}
        _fetch_market_slice(segment="Beauty Market", period_key="current", item=item)
        beauty_sql = fetch_df.call_args.args[0]
        _fetch_market_slice(segment="pure mass", period_key="current", item=item)
        mass_sql = fetch_df.call_args.args[0]
        self.assertIn("global_segment", beauty_sql)
        self.assertIn("TRIM(global_segment)", mass_sql)
        self.assertNotIn("TRIM(segment)", mass_sql)
        self.assertIn("UPPER(TRIM(category_EN)) = 'TOTAL BEAUTY'", beauty_sql)
        self.assertIn("UPPER(TRIM(category_EN)) = 'TOTAL BEAUTY'", mass_sql)
        self.assertIn("CAST(bus_date AS DATE)", beauty_sql)
        self.assertNotIn("DAY(bus_date)", beauty_sql)

    @patch("bot.tools.query_three_platform_competitor.fetch_df", return_value=pd.DataFrame())
    def test_daily_market_does_not_assume_monthly_total_beauty_contract(self, fetch_df):
        item = {"start": "2026-06-01", "end": "2026-06-18", "full_month": False}
        _fetch_market_slice(segment="Beauty Market", period_key="current", item=item)
        self.assertNotIn("TRIM(category_EN)", fetch_df.call_args.args[0])

    @patch("bot.tools.query_three_platform_competitor.fetch_df")
    def test_market_q1_queries_each_natural_month_without_day_remapping(self, fetch_df):
        fetch_df.return_value = pd.DataFrame([
            {"period_key": "current", "platform": platform, "gmv": 100, "row_count": 1}
            for platform in ("TM", "DY", "JD")
        ])
        frame, sources, missing = _query_market("Beauty Market", {
            "current_start": "2026-01-01", "current_end": "2026-03-31",
            "prior_start": "2025-01-01", "prior_end": "2025-03-31",
        })
        self.assertFalse(frame.empty)
        self.assertFalse(missing)
        self.assertEqual(fetch_df.call_count, 6)
        ranges = [
            (call.args[1]["slice_start"], call.args[1]["slice_end"])
            for call in fetch_df.call_args_list
        ]
        self.assertIn(("2026-02-01", "2026-02-28"), ranges)
        self.assertIn(("2026-03-01", "2026-03-31"), ranges)
        for call in fetch_df.call_args_list:
            self.assertIn("CAST(bus_date AS DATE)", call.args[0])
            self.assertNotIn("DAY(bus_date)", call.args[0])


class ThreePlatformMetricTest(unittest.TestCase):
    def test_top_five_selected_by_current_gmv_and_prior_is_matched(self):
        rows = []
        for index in range(1, 7):
            rows.append({"period_key": "current", "platform": "TM", "category": f"类目{index}", "gmv": 70 - index * 10})
            rows.append({"period_key": "prior", "platform": "TM", "category": f"类目{index}", "gmv": index * 5})
        frame = pd.DataFrame(rows)
        result = _top_categories(frame)
        self.assertEqual(len(result["TM"]), 5)
        self.assertEqual(result["TM"][0]["category"], "类目1")
        self.assertEqual(result["TM"][0]["gmv_prior"], 5)
        self.assertNotIn("类目6", [row["category"] for row in result["TM"]])

    def test_ttl_share_uses_summed_brand_and_market_not_average_share(self):
        brand = {
            "current": {"TM": 10, "DY": 20, "JD": 30, "TTL": 60},
            "prior": {"TM": 8, "DY": 16, "JD": 24, "TTL": 48},
        }
        market = {
            "current": {"TM": 100, "DY": 400, "JD": 500, "TTL": 1000},
            "prior": {"TM": 80, "DY": 320, "JD": 400, "TTL": 800},
        }
        ttl = _overall_rows(brand, market, None)[0]
        self.assertEqual(ttl["beauty_share"], 0.06)

    def test_channel_formula_and_negative_guard(self):
        valid = _channel_rows(pd.DataFrame([
            {"period_key": "current", "brand_gmv": 100, "kol_gmv": 30, "storelive_gmv": 20},
            {"period_key": "prior", "brand_gmv": 80, "kol_gmv": 20, "storelive_gmv": 20},
        ]))
        self.assertEqual(valid["rows"][2]["gmv_current"], 50)
        invalid = _channel_rows(pd.DataFrame([
            {"period_key": "current", "brand_gmv": 100, "kol_gmv": 80, "storelive_gmv": 30},
        ]))
        self.assertEqual(invalid["error"], "negative_short_other")

    def test_leap_year_prior_ends_on_february_28(self):
        parsed = parse_ec_period("2024年2月", 2024)
        self.assertEqual(parsed["current_end"], "2024-02-29")
        self.assertEqual(parsed["prior_end"], "2023-02-28")


class ThreePlatformFormatterTest(unittest.TestCase):
    def test_report_contains_fixed_sections_top5_and_new_growth(self):
        result = {
            "brand": "珀莱雅",
            "period": "2026年Q2",
            "period_meta": {
                "current_label": "2026年4月1日—6月30日",
                "prior_label": "2025年4月1日—6月30日",
                "current_start": "2026-04-01", "current_end": "2026-06-30",
                "prior_start": "2025-04-01", "prior_end": "2025-06-30",
            },
            "is_pure_mass": True,
            "overall": [{
                "platform": key, "gmv_current": 100, "gmv_prior": 0, "evol": None,
                "beauty_share": 0.1, "beauty_share_change": 0.01,
                "pure_mass_share": 0.2, "pure_mass_share_change": 0.02,
            } for key in ("TTL", "TM", "DY", "JD")],
            "categories": {key: [] for key in ("TM", "DY", "JD")},
            "channels": [{
                "channel": "KOL直播", "gmv_current": 100, "gmv_prior": 0,
                "evol": None, "gmv_change": 100, "weight": 1, "weight_change": 1,
            }],
        }
        report = format_three_platform_competitor_report(result)
        self.assertIn("# 珀莱雅三平台生意分析", report)
        self.assertIn("三平台品牌GMV", report)
        self.assertIn("天猫平台｜类目GMV Top 5", report)
        self.assertIn("抖音平台｜类目GMV Top 5", report)
        self.assertIn("京东平台｜类目GMV Top 5", report)
        self.assertIn("| 类目 | 本期GMV |", report)
        self.assertNotIn("三级类目", report)
        self.assertNotIn("四级类目", report)
        self.assertIn("新增长", report)
        self.assertIn("Pure Mass份额", report)


if __name__ == "__main__":
    unittest.main()
