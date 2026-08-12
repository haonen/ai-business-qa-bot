from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from bot.jd_business_formatter import format_jd_business_report
from bot.router import route
from bot.session import SessionState
from bot.tools.query_jd_business import (
    JD_DAILY_TABLE,
    _combine_other,
    _fetch_category_slice,
    _paired_category_rows,
    _query_category_frames,
    _source_slices,
)
from bot.utils import parse_ec_period


class JdSourceSelectionTest(unittest.TestCase):
    def test_complete_month_uses_daily_table_for_category_reconciliation(self):
        rows = _source_slices("2026-06-01", "2026-06-30")
        self.assertEqual(rows[0]["source"], "daily")
        self.assertEqual(rows[0]["table"], JD_DAILY_TABLE)

    def test_partial_month_uses_daily_table(self):
        rows = _source_slices("2026-06-01", "2026-06-18")
        self.assertEqual(rows[0]["source"], "daily")
        self.assertEqual(rows[0]["table"], JD_DAILY_TABLE)

    def test_mixed_period_uses_daily_for_every_month(self):
        rows = _source_slices("2026-05-15", "2026-06-30")
        self.assertEqual([row["source"] for row in rows], ["daily", "daily"])

    @patch("bot.tools.query_jd_business.fetch_df", return_value=pd.DataFrame())
    def test_daily_sql_uses_confirmed_selfrun_table(self, fetch_df):
        _fetch_category_slice(
            table=JD_DAILY_TABLE,
            brand="PROYA",
            period_key="current",
            start="2026-06-01",
            end="2026-06-18",
            monthly=False,
        )
        sql = fetch_df.call_args.args[0]
        self.assertIn(JD_DAILY_TABLE, sql)
        self.assertNotIn("TRIM(platform)", sql)

    @patch("bot.tools.query_jd_business._fetch_category_slice")
    def test_complete_month_queries_daily_once_per_period(self, query_slice):
        current = pd.DataFrame([{
            "period_key": "current", "category_level_3": "面霜", "gmv": 100, "row_count": 1,
        }])
        prior = pd.DataFrame([{
            "period_key": "prior", "category_level_3": "面霜", "gmv": 80, "row_count": 1,
        }])
        query_slice.side_effect = [current, prior]
        frame, sources, missing = _query_category_frames("PROYA", {
            "current_start": "2026-06-01", "current_end": "2026-06-30",
            "prior_start": "2025-06-01", "prior_end": "2025-06-30",
        })
        self.assertFalse(frame.empty)
        self.assertFalse(missing)
        self.assertEqual([row["source"] for row in sources], ["daily", "daily"])
        self.assertTrue(all(call.kwargs["table"] == JD_DAILY_TABLE for call in query_slice.call_args_list))


class JdMetricTest(unittest.TestCase):
    def test_categories_reconcile_to_brand_total_and_share_uses_brand_total(self):
        current_values = [60, 15, 8, 6, 4, 3, 2, 2]
        prior_values = [50, 20, 10, 5, 5, 4, 3, 3]
        rows = []
        for index, (current, prior) in enumerate(zip(current_values, prior_values), 1):
            category = f"品类{index}"
            rows.extend([
                {"period_key": "current", "category_level_3": category, "gmv": current},
                {"period_key": "prior", "category_level_3": category, "gmv": prior},
            ])
        total, categories = _paired_category_rows(pd.DataFrame(rows))
        displayed = _combine_other(categories, total)
        self.assertEqual(total["gmv_current"], 100)
        self.assertEqual(categories[0]["weight"], 0.6)
        self.assertEqual(len(displayed), 7)
        self.assertEqual(displayed[-1]["category"], "其他")
        self.assertEqual(sum(row["gmv_current"] for row in displayed), total["gmv_current"])
        self.assertEqual(sum(row["gmv_prior"] for row in displayed), total["gmv_prior"])
        self.assertAlmostEqual(sum(row["weight"] for row in displayed), 1.0)


class JdReportAndRouteTest(unittest.TestCase):
    def test_explicit_trigger_routes_to_jd_report(self):
        routed = route("生成珀莱雅2026年6月京东品牌生意分析", SessionState())
        self.assertEqual(routed.type, "jd_business_analysis")
        self.assertEqual(routed.brand, "珀莱雅")
        self.assertEqual(routed.period, "2026年6月")

    def test_missing_period_asks_for_jd_period(self):
        routed = route("生成珀莱雅京东品牌生意分析", SessionState())
        self.assertEqual(routed.type, "clarify_jd_period")

    def test_recommended_full_sentence_extracts_only_the_brand(self):
        routed = route(
            "请做珀莱雅在2026年6月的京东自营生意分析，"
            "输出品牌整体GMV、同比和三级类目表现。",
            SessionState(),
        )
        self.assertEqual(routed.type, "jd_business_analysis")
        self.assertEqual(routed.brand, "珀莱雅")
        self.assertEqual(routed.period, "2026年6月")

    def test_same_month_short_range_and_quarter_are_supported(self):
        short_range = route("看一下珀莱雅7月1日至15日的京东GMV和品类表现", SessionState())
        quarter = route("生成兰蔻2026年Q2京东竞品生意报告", SessionState())
        self.assertEqual((short_range.type, short_range.brand, short_range.period), (
            "jd_business_analysis", "珀莱雅", "7月1日至15日",
        ))
        self.assertEqual((quarter.type, quarter.brand, quarter.period), (
            "jd_business_analysis", "兰蔻", "2026年Q2",
        ))
        parsed = parse_ec_period("2026年Q2", 2026)
        self.assertEqual(parsed["current_start"], "2026-04-01")
        self.assertEqual(parsed["current_end"], "2026-06-30")

    def test_market_and_media_questions_do_not_trigger_jd_report(self):
        market = route("2026年1-6月京东大盘怎么样", SessionState())
        media = route("分析2026年3月京东媒体花费", SessionState())
        self.assertEqual(market.type, "market_analysis")
        self.assertNotEqual(media.type, "jd_business_analysis")

    def test_report_unifies_formats_and_omits_links_and_source_notes(self):
        result = {
            "brand": "珀莱雅",
            "period": "2026年6月",
            "period_meta": {
                "current_label": "2026年6月1日—6月30日",
                "prior_label": "2025年6月1日—6月30日",
            },
            "brand_result": {
                "gmv_current": 714_300_000,
                "gmv_prior": 1_777_000_000,
                "evol": -0.598,
            },
            "all_categories": [{
                "category": "面部护理套装",
                "gmv_current": 168_500_000,
                "gmv_prior": 300_892_857,
                "evol": -0.44,
                "weight": 0.236,
                "weight_change": -0.15,
            }],
            "categories": [{
                "category": "面部护理套装",
                "gmv_current": 168_500_000,
                "gmv_prior": 300_892_857,
                "evol": -0.44,
                "weight": 0.236,
                "weight_change": -0.15,
            }, {
                "category": "洁面",
                "gmv_current": 17_500_000,
                "gmv_prior": 12_152_778,
                "evol": 0.44,
                "weight": 0.0245,
                "weight_change": -0.0001,
            }],
        }
        report = format_jd_business_report(result)
        self.assertIn("# 京东品牌生意分析", report)
        self.assertIn("同比-60%", report)
        self.assertIn("+44%", report)
        self.assertIn("0pp", report)
        self.assertIn("下降15pp至24%", report)
        self.assertNotIn("下降+15pp", report)
        self.assertNotIn("Top链接", report)
        self.assertNotIn("商品链接", report)
        self.assertNotIn("数据口径", report)
        self.assertNotIn("three_platform", report)


if __name__ == "__main__":
    unittest.main()
