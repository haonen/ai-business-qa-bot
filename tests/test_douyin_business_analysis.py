from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from bot.douyin_business_formatter import format_douyin_business_report
from bot.router import route
from bot.session import SessionState
from bot.tools.query_douyin_business import (
    _category_result,
    _channel_overview,
    _product_analysis,
    _query_s2_channels,
    _query_s2_categories,
    _query_s4_products,
    _source_slices,
)


class DouyinSourceSelectionTest(unittest.TestCase):
    def test_complete_month_uses_monthly_table(self):
        rows = _source_slices("2026-06-01", "2026-06-30")
        self.assertEqual(rows[0]["source"], "monthly")
        self.assertEqual(rows[0]["table"], "three_platform_store_rank_monthly")

    def test_partial_month_uses_daily_table(self):
        rows = _source_slices("2026-06-01", "2026-06-18")
        self.assertEqual(rows[0]["source"], "daily")
        self.assertEqual(rows[0]["table"], "dy_store_ranking_BFSS_day_jiashicang")

    def test_mixed_period_selects_each_month_independently(self):
        rows = _source_slices("2026-05-15", "2026-06-30")
        self.assertEqual([row["source"] for row in rows], ["daily", "monthly"])


class DouyinMetricTest(unittest.TestCase):
    def test_category_output_and_top_one_selection(self):
        frame = pd.DataFrame([
            {"period_key": "current", "category_level_4": "面霜", "gmv": 600},
            {"period_key": "current", "category_level_4": "精华", "gmv": 400},
            {"period_key": "prior", "category_level_4": "面霜", "gmv": 500},
            {"period_key": "prior", "category_level_4": "精华", "gmv": 500},
        ])
        result = _category_result(frame)
        self.assertEqual(result["selected_category"], "面霜")
        self.assertEqual(result["categories"][0]["evol"], 0.2)
        self.assertEqual(result["categories"][0]["weight"], 0.6)
        self.assertAlmostEqual(result["categories"][0]["weight_change"], 0.1)

    def test_channel_mix_uses_s2_daily_brand_denominator(self):
        frame = pd.DataFrame([
            {"period_key": "current", "brand_gmv": 1000, "kol_gmv": 400, "storelive_gmv": 350},
            {"period_key": "prior", "brand_gmv": 800, "kol_gmv": 300, "storelive_gmv": 300},
        ])
        result = _channel_overview(frame)
        by_key = {row["channel_key"]: row for row in result["rows"]}
        self.assertEqual(by_key["short_other"]["gmv_current"], 250)
        self.assertEqual(by_key["short_other"]["weight"], 0.25)
        self.assertEqual(sum(row["weight"] for row in result["rows"]), 1.0)

    def test_s4_series_and_top_links_use_channel_denominator(self):
        frame = pd.DataFrame([
            {"period_key": "current", "item_id": "1", "product_name": "韩束红蛮腰套装", "product_url": "u1", "category_level_4": "面霜", "sales_gmv": 700, "kol_gmv": 500, "storelive_gmv": 100},
            {"period_key": "current", "item_id": "2", "product_name": "韩束白蛮腰水乳", "product_url": "u2", "category_level_4": "面霜", "sales_gmv": 300, "kol_gmv": 100, "storelive_gmv": 100},
            {"period_key": "prior", "item_id": "1", "product_name": "韩束红蛮腰套装", "product_url": "u1", "category_level_4": "面霜", "sales_gmv": 500, "kol_gmv": 300, "storelive_gmv": 100},
            {"period_key": "prior", "item_id": "2", "product_name": "韩束白蛮腰水乳", "product_url": "u2", "category_level_4": "面霜", "sales_gmv": 500, "kol_gmv": 100, "storelive_gmv": 200},
        ])
        result = _product_analysis(frame, selected_category="面霜", brand_candidates=["韩束"])
        self.assertEqual(result["series"][0]["series"], "红蛮腰")
        kol = result["channels"]["kol_live"]
        self.assertEqual(kol["top_series"]["series"], "红蛮腰")
        self.assertAlmostEqual(kol["top_links"][0]["weight"], 500 / 600, places=4)
        self.assertEqual(kol["top5_concentration"], 1.0)


class DouyinSqlContractTest(unittest.TestCase):
    PERIOD = {
        "current_start": "2026-06-01", "current_end": "2026-06-30",
        "prior_start": "2025-06-01", "prior_end": "2025-06-30",
    }

    @patch("bot.tools.query_douyin_business.fetch_df", return_value=pd.DataFrame())
    def test_complete_month_restores_monthly_business_date(self, fetch_df):
        _query_s2_categories("KANS", self.PERIOD)
        monthly_sql = fetch_df.call_args_list[0].args[0]
        self.assertIn("three_platform_store_rank_monthly", monthly_sql)
        self.assertIn("LPAD(DAY(bus_date)", monthly_sql)

    @patch("bot.tools.query_douyin_business.fetch_df", return_value=pd.DataFrame())
    def test_s2_channel_sql_uses_confirmed_fields(self, fetch_df):
        _query_s2_channels("KANS", self.PERIOD)
        sql = fetch_df.call_args.args[0]
        self.assertIn("dy_store_ranking_BFSS_day_jiashicang", sql)
        self.assertIn("KOL_gmv", sql)
        self.assertIn("storelive_gmv", sql)
        self.assertIn("brand_name", sql)

    @patch("bot.tools.query_douyin_business.fetch_df", return_value=pd.DataFrame())
    def test_s4_sql_uses_confirmed_fields(self, fetch_df):
        _query_s4_products("韩束", self.PERIOD)
        sql = fetch_df.call_args.args[0]
        for field in ("商品ID", "商品品牌", "商品url", "商品四级分类", "业务日期", "销售额", "达人推广直播GMV", "品牌自营直播GMV"):
            self.assertIn(field, sql)
        self.assertIn("dy_goodssales_rank_day_jiashicang", sql)


class DouyinReportAndRouteTest(unittest.TestCase):
    def test_business_situation_with_explicit_douyin_never_defaults_to_tmall(self):
        routed = route("分析一下美宝莲在抖音平台2026年1-6月生意情况。", SessionState())
        self.assertEqual(routed.type, "douyin_business_analysis")
        self.assertEqual(routed.brand, "美宝莲")
        self.assertEqual(routed.period, "2026年1-6月")

    def test_explicit_trigger_routes_to_douyin_report(self):
        routed = route("生成韩束2026年6月抖音生意分析报告", SessionState())
        self.assertEqual(routed.type, "douyin_business_analysis")
        self.assertEqual(routed.brand, "韩束")
        self.assertEqual(routed.period, "2026年6月")

    def test_missing_period_asks_for_douyin_period(self):
        routed = route("生成韩束抖音生意分析报告", SessionState())
        self.assertEqual(routed.type, "clarify_douyin_period")

    def test_report_contains_only_confirmed_quantitative_sections(self):
        result = {
            "brand": "韩束",
            "period": "2026年6月",
            "period_meta": {
                "current_label": "2026年6月1日—6月30日",
                "current_start": "2026-06-01", "current_end": "2026-06-30",
                "prior_start": "2025-06-01", "prior_end": "2025-06-30",
            },
            "brand_result": {"gmv_current": 1000, "gmv_prior": 800, "evol": 0.25},
            "categories": [{"category": "面霜", "gmv_current": 600, "gmv_prior": 500, "evol": 0.2, "weight": 0.6, "weight_change": 0.1}],
            "selected_category": "面霜",
            "product_series": [{"series": "红蛮腰", "gmv_current": 500, "gmv_prior": 400, "evol": 0.25, "weight": 0.8333, "weight_change": 0.0333}],
            "channel_result": {
                "rows": [{"channel": "KOL直播", "gmv_current": 400, "gmv_prior": 300, "gmv_change": 100, "evol": 0.3333, "weight": 0.4, "weight_change": 0.025}],
                "business_leader": {"channel": "KOL直播", "gmv_current": 400, "weight": 0.4},
                "growth_leader": {"channel": "KOL直播", "gmv_change": 100},
                "all_declining": False,
            },
            "channel_products": {},
            "sources": [{"table": "dy_store_ranking_BFSS_day_jiashicang"}, {"table": "three_platform_store_rank_monthly"}, {"table": "dy_goodssales_rank_day_jiashicang"}],
        }
        report = format_douyin_business_report(result)
        self.assertIn("品牌整体GMV", report)
        self.assertIn("本期品牌GMV占比", report)
        self.assertIn("本期类目内占比", report)
        self.assertNotIn("Total Market", report)
        for phrase in ("营销效果", "收割人群", "核心卖点", "大众消费基础"):
            self.assertNotIn(phrase, report)


if __name__ == "__main__":
    unittest.main()
