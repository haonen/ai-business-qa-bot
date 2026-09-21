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
    _resolve_s2_daily_brand,
    _resolve_s2_monthly_brand,
    _query_s2_channels,
    _query_s2_categories,
    _query_s4_products,
    _source_slices,
    query_douyin_business,
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
    @patch("bot.tools.query_douyin_business._infer_series_mapping", return_value={})
    def test_series_mapping_input_is_gmv_ranked_and_bounded(self, infer_mapping):
        rows = []
        for index in range(75):
            rows.append({
                "period_key": "current", "item_id": str(index),
                "product_name": f"商品{index:02d}", "product_url": f"u{index}",
                "category_level_4": "彩妆", "sales_gmv": index + 1,
                "kol_gmv": 0, "storelive_gmv": 0,
            })
        with patch.dict("os.environ", {"DOUYIN_SERIES_MAX_TITLES": "60"}):
            _product_analysis(pd.DataFrame(rows), brand_candidates=["OKCS"])
        titles = infer_mapping.call_args.args[0]
        self.assertEqual(len(titles), 60)
        self.assertEqual(titles[0], "商品74")
        self.assertNotIn("商品00", titles)

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

    def test_channel_mix_uses_s2_brand_denominator(self):
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
        result = _product_analysis(frame, brand_candidates=["韩束"])
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
    def test_complete_month_uses_normal_monthly_business_date(self, fetch_df):
        _query_s2_categories("KANS", self.PERIOD)
        monthly_sql = fetch_df.call_args_list[0].args[0]
        self.assertIn("three_platform_store_rank_monthly", monthly_sql)
        self.assertIn("CAST(bus_date AS DATE)", monthly_sql)
        self.assertNotIn("LPAD(DAY(bus_date)", monthly_sql)
        self.assertIn("category_EN_level_1", monthly_sql)
        self.assertIn("'skincare'", monthly_sql)
        self.assertIn("'hair'", monthly_sql)
        self.assertIn("'makeup (exclude fragrance)'", monthly_sql)

    @patch("bot.tools.query_douyin_business.fetch_df", return_value=pd.DataFrame())
    def test_s2_queries_are_batched_by_source_not_month(self, fetch_df):
        period = {
            "current_start": "2026-01-01", "current_end": "2026-06-30",
            "prior_start": "2025-01-01", "prior_end": "2025-06-30",
        }
        _query_s2_categories("KANS", period)
        self.assertEqual(fetch_df.call_count, 1)
        fetch_df.reset_mock()
        _query_s2_channels("KANS", period)
        self.assertEqual(fetch_df.call_count, 1)

    @patch("bot.tools.query_douyin_business.fetch_df", return_value=pd.DataFrame())
    def test_partial_month_daily_store_is_limited_to_ttl_beauty(self, fetch_df):
        period = {
            "current_start": "2026-07-01", "current_end": "2026-07-19",
            "prior_start": "2025-07-01", "prior_end": "2025-07-19",
        }
        _query_s2_categories("PROYA", period)
        category_sql = fetch_df.call_args.args[0]
        self.assertIn("dy_store_ranking_BFSS_day_jiashicang", category_sql)
        self.assertIn("category_EN_level_1", category_sql)
        for category in ("skincare", "makeup", "makeup + fragrance", "hair"):
            self.assertIn(f"'{category}'", category_sql)

        fetch_df.reset_mock()
        _query_s2_channels("PROYA", period)
        channel_sql = fetch_df.call_args.args[0]
        self.assertIn("category_EN_level_1", channel_sql)
        self.assertNotIn("male skincare", channel_sql)

    @patch(
        "bot.tools.query_douyin_business.configured_brand_alias",
        return_value=None,
    )
    @patch(
        "bot.tools.query_douyin_business.generate_brand_variants",
        return_value=("美宝莲", "Maybelline"),
    )
    @patch("bot.tools.query_douyin_business.fetch_df")
    def test_monthly_brand_is_validated_against_monthly_fact_table(
        self, fetch_df, _variants, _configured
    ):
        fetch_df.return_value = pd.DataFrame([{"source_brand": "Maybelline"}])
        result = _resolve_s2_monthly_brand("美宝莲", "美宝莲", self.PERIOD)
        self.assertEqual(result, "Maybelline")
        sql = fetch_df.call_args.args[0]
        params = fetch_df.call_args.args[1]
        self.assertIn("three_platform_store_rank_monthly", sql)
        self.assertIn("category_EN_level_1", sql)
        self.assertIn("Maybelline", params.values())

    @patch(
        "bot.tools.query_douyin_business.configured_brand_alias",
        return_value="Maybelline",
    )
    @patch("bot.tools.query_douyin_business.fetch_df")
    def test_configured_monthly_brand_does_not_probe_fact_table(self, fetch_df, _configured):
        result = _resolve_s2_monthly_brand("美宝莲", "美宝莲", self.PERIOD)
        self.assertEqual(result, "Maybelline")
        fetch_df.assert_not_called()

    @patch(
        "bot.tools.query_douyin_business.configured_brand_alias",
        return_value="PROYA",
    )
    @patch("bot.tools.query_douyin_business.fetch_df")
    def test_configured_daily_brand_does_not_probe_fact_table(self, fetch_df, _configured):
        result = _resolve_s2_daily_brand("珀莱雅", "珀莱雅", self.PERIOD)
        self.assertEqual(result, "PROYA")
        fetch_df.assert_not_called()

    @patch("bot.tools.query_douyin_business.fetch_df", return_value=pd.DataFrame())
    def test_complete_month_s2_channel_sql_uses_monthly_table(self, fetch_df):
        _query_s2_channels("KANS", self.PERIOD)
        sql = fetch_df.call_args_list[0].args[0]
        self.assertIn("three_platform_store_rank_monthly", sql)
        self.assertIn("monthly_dedup", sql)
        self.assertIn("kol_gmv", sql)
        self.assertIn("storelive_gmv", sql)
        self.assertIn("brand_name", sql)
        self.assertIn("category_EN_level_1", sql)
        self.assertNotIn("dy_store_ranking_BFSS_day_jiashicang", sql)

    @patch("bot.tools.query_douyin_business.fetch_df", return_value=pd.DataFrame())
    def test_s4_sql_uses_confirmed_fields(self, fetch_df):
        _query_s4_products("韩束", self.PERIOD)
        sql = fetch_df.call_args.args[0]
        params = fetch_df.call_args.args[1]
        for field in ("商品ID", "商品品牌", "key_driver", "商品四级分类", "业务日期", "销售额"):
            self.assertIn(field, sql)
        self.assertNotIn("品牌自营直播GMV", sql)
        self.assertNotIn("达人推广直播GMV", sql)
        self.assertIn("ai_bot_dy_product_link", sql)
        self.assertIn("NULL AS product_url", sql)
        self.assertNotIn("CAST(`业务日期` AS DATE)", sql)
        self.assertIn("WHERE `商品品牌` = :brand", sql)
        self.assertNotIn("TRIM(`商品品牌`) = :brand", sql)
        self.assertEqual(params["current_start"], "2026-06-01")
        self.assertEqual(params["current_end"], "2026-06-30")
        self.assertEqual(params["prior_start"], "2025-06-01")
        self.assertEqual(params["prior_end"], "2025-06-30")

    @patch("bot.tools.query_douyin_business.resolve_source_brand", return_value={"brand": "美宝莲"})
    @patch("bot.tools.query_douyin_business._query_s2_categories", side_effect=RuntimeError("db sql secret"))
    def test_core_database_errors_are_not_exposed_to_user(self, _query_categories, _resolve):
        result = query_douyin_business("美宝莲", "2026年1-6月")
        self.assertEqual(result["error"], "execution_error")
        self.assertNotIn("db sql secret", result["message"])
        self.assertIn("请稍后重试", result["message"])

    @patch("bot.tools.query_douyin_business.resolve_source_brand", return_value={"brand": "美宝莲"})
    @patch("bot.tools.query_douyin_business._query_s4_products", side_effect=AssertionError("must not query product table"))
    @patch("bot.tools.query_douyin_business._query_s2_channels", return_value=pd.DataFrame())
    @patch("bot.tools.query_douyin_business._query_s2_categories")
    def test_douyin_report_does_not_query_product_table(
        self, query_categories, _query_channels, _query_products, _resolve
    ):
        query_categories.return_value = (
            pd.DataFrame([
                {"period_key": "current", "category_level_4": "彩妆", "gmv": 100},
                {"period_key": "prior", "category_level_4": "彩妆", "gmv": 80},
            ]),
            [{"table": "three_platform_store_rank_monthly"}],
            [],
        )
        result = query_douyin_business("美宝莲", "2026年1-6月")
        self.assertNotIn("error", result)
        self.assertEqual(result["brand_result"]["gmv_current"], 100)
        _query_products.assert_not_called()
        self.assertFalse(any("商品" in item for item in result["limitations"]))

    @patch("bot.tools.query_douyin_business.fetch_df", return_value=pd.DataFrame())
    def test_monthly_category_uses_real_monthly_schema(self, fetch_df):
        _query_s2_categories("美宝莲", self.PERIOD)
        sql = fetch_df.call_args_list[0].args[0]
        self.assertIn("TRIM(category_CN)", sql)
        self.assertIn("clear_category_status", sql)
        self.assertNotIn("TRIM(category_level_4)", sql)


class DouyinReportAndRouteTest(unittest.TestCase):
    def test_empty_channel_product_modules_are_not_rendered(self):
        result = {
            "brand": "OKCS",
            "period": "2026年1月至3月",
            "period_meta": {
                "current_start": "2026-01-01", "current_end": "2026-03-31",
                "prior_start": "2025-01-01", "prior_end": "2025-03-31",
            },
            "brand_result": {"gmv_current": 100, "gmv_prior": 80, "evol": 0.25},
            "categories": [],
            "selected_category": "彩妆",
            "product_series": [],
            "channel_result": {},
            "channel_products": {},
            "limitations": ["商品日表查询超时或连接中断，未展示产品系列和商品下钻。"],
            "sources": [{"table": "three_platform_store_rank_monthly"}],
        }
        report = format_douyin_business_report(result)
        self.assertNotIn("kol_live × 货品", report)
        self.assertNotIn("store_live × 货品", report)
        self.assertNotIn("short_other × 货品", report)
        self.assertNotIn("Top 5集中度：—", report)
        self.assertIn("商品日表查询超时", report)

    def test_business_question_without_platform_asks_for_platform(self):
        routed = route("美宝莲2026年1-6月生意怎么样", SessionState())
        self.assertEqual(routed.type, "clarify_ec_platform")
        self.assertEqual(routed.brand, "美宝莲")
        self.assertEqual(routed.period, "2026年1-6月")

    def test_douyin_reply_resumes_platform_clarification(self):
        state = SessionState(pending_request={
            "intent": "business_platform_selection",
            "brand": "美宝莲",
            "period": "2026年1-6月",
            "brand_aliases": ["美宝莲"],
        })
        routed = route("抖音", state)
        self.assertEqual(routed.type, "douyin_business_analysis")
        self.assertEqual(routed.brand, "美宝莲")
        self.assertEqual(routed.period, "2026年1-6月")
        self.assertEqual(routed.platform, "DY")

    def test_business_performance_wording_routes_to_douyin(self):
        routed = route("请重新分析美宝莲2026年1月至6月在抖音的生意表现", SessionState())
        self.assertEqual(routed.type, "douyin_business_analysis")
        self.assertEqual(routed.brand, "美宝莲")
        self.assertEqual(routed.period, "2026年1月至6月")
        self.assertEqual(routed.platform, "DY")

    def test_colloquial_douyin_question_keeps_only_clean_brand(self):
        routed = route("proya 抖音2026年2月的生意咋样", SessionState())
        self.assertEqual(routed.type, "douyin_business_analysis")
        self.assertEqual(routed.brand, "proya")
        self.assertEqual(routed.period, "2026年2月")

    def test_explicit_douyin_request_overrides_stale_default_pending_request(self):
        state = SessionState()
        state.pending_request = {
            "intent": "default_analysis",
            "brand": "韩束",
            "brand_aliases": ["韩束"],
        }
        routed = route("请重新分析美宝莲2026年1月至6月在抖音的生意表现", state)
        self.assertEqual(routed.type, "douyin_business_analysis")
        self.assertEqual(routed.brand, "美宝莲")

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
            "sources": [{"table": "dy_store_ranking_BFSS_day_jiashicang"}, {"table": "three_platform_store_rank_monthly"}, {"table": "ai_bot_dy_product_link"}],
        }
        report = format_douyin_business_report(result)
        self.assertIn("品牌整体GMV", report)
        self.assertIn("本期品牌GMV占比", report)
        self.assertNotIn("本期品牌商品GMV占比", report)
        self.assertNotIn("产品系列", report)
        self.assertNotIn("Top 5商品", report)
        self.assertNotIn("Total Market", report)
        for phrase in ("营销效果", "收割人群", "核心卖点", "大众消费基础"):
            self.assertNotIn(phrase, report)


if __name__ == "__main__":
    unittest.main()
