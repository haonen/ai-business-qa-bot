from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from bot.chains.default_chain import run_default_chain
from bot.feishu_doc import markdown_to_items
from bot.formatter import (
    format_report,
    render_module_category,
    render_module_drilldown,
    render_module_overall,
)
from bot.router import IntentResult, route
from bot.session import SessionState
from bot import media_brand
from bot.tools import common
from bot.tools.query_driver import query_driver
from bot.tools.query_ecip_tmall_gmv import query_ecip_tmall_gmv
from bot.utils import parse_ec_period
from data_import.tmall_brand_index_pipeline import alias_variants, _index_rows_for_fact_rows


class EcPeriodTest(unittest.TestCase):
    def test_arbitrary_date_range_uses_prior_calendar_dates(self):
        result = parse_ec_period("2026年7月1日到7月19日", 2026)
        self.assertEqual(result["current_start"], "2026-07-01")
        self.assertEqual(result["current_end"], "2026-07-19")
        self.assertEqual(result["prior_start"], "2025-07-01")
        self.assertEqual(result["prior_end"], "2025-07-19")

    def test_month_without_year_uses_latest_data_year(self):
        result = parse_ec_period("6月", 2026)
        self.assertEqual(result["current_start"], "2026-06-01")
        self.assertEqual(result["current_end"], "2026-06-30")

    def test_campaign_can_specify_year(self):
        result = parse_ec_period("2025年618", 2026)
        self.assertEqual(result["current_start"], "2025-05-13")
        self.assertEqual(result["prior_start"], "2024-05-13")


class EcContextTest(unittest.TestCase):
    def setUp(self):
        common._EC_CONTEXT_CACHE.clear()
        common._EC_SKU_CACHE.clear()

    @patch("bot.tools.common.resolve_source_brand")
    @patch("bot.tools.common.fetch_one")
    def test_coverage_and_row_checks_use_forced_index_lookups(self, fetch_one, resolve_brand):
        resolve_brand.return_value = {
            "brand": "KANS",
            "match_method": "dictionary_exact",
        }
        fetch_one.side_effect = [
            {"max_date": "2026-07-19"},
            {"row_exists": 1},
            {"row_exists": 1},
        ]
        common.ec_query_context(
            "韩束",
            "2026年7月1日到7月10日",
            brand_aliases=["韩束", "KANS"],
        )
        resolve_brand.assert_called_once_with(
            "韩束",
            "tmall",
            brand_aliases=["韩束", "KANS"],
        )
        latest_sql = fetch_one.call_args_list[0].args[0]
        current_sql = fetch_one.call_args_list[1].args[0]
        prior_sql = fetch_one.call_args_list[2].args[0]
        self.assertIn("FORCE INDEX (idx_tmall_brand_date)", latest_sql)
        self.assertIn("ORDER BY bus_date DESC", latest_sql)
        self.assertIn("LIMIT 1", current_sql)
        self.assertIn("LIMIT 1", prior_sql)

    @patch("bot.tools.common.resolve_source_brand")
    @patch("bot.tools.common.fetch_one")
    def test_rejects_period_after_latest_date(self, fetch_one, resolve_brand):
        resolve_brand.return_value = {
            "brand": "PROYA",
            "match_method": "dictionary_exact",
        }
        fetch_one.return_value = {"max_date": "2026-07-19"}
        with self.assertRaisesRegex(common.EcDataError, "2026-07-19"):
            common.ec_query_context("珀莱雅", "2026年7月")

    @patch("bot.tools.common.resolve_source_brand")
    @patch("bot.tools.common.fetch_one")
    def test_missing_prior_stops_report(self, fetch_one, resolve_brand):
        resolve_brand.return_value = {
            "brand": "PROYA",
            "match_method": "dictionary_exact",
        }
        fetch_one.side_effect = [
            {"max_date": "2026-07-19"},
            {"row_exists": 1},
            {},
        ]
        with self.assertRaisesRegex(common.EcDataError, "去年同期没有"):
            common.ec_query_context("珀莱雅", "2026年7月1日到7月19日")

    @patch("bot.tools.common.resolve_source_brand")
    @patch("bot.tools.common.fetch_one")
    @patch("bot.tools.common.fetch_df")
    def test_filter_uses_new_table_and_explicit_fields(self, fetch_df, fetch_one, resolve_brand):
        resolve_brand.return_value = {"brand": "PROYA", "match_method": "dictionary_exact"}
        fetch_one.side_effect = [
            {"max_date": "2026-07-19"},
            {"row_exists": 1},
            {"row_exists": 1},
        ]
        fetch_df.return_value = pd.DataFrame([
            {
                "bus_date": "2026-07-01",
                "category_cn": "面霜",
                "product_title": "商品",
                "gmv": 100,
                "unit": 1,
                "key_driver": "渠道A",
            }
        ])
        common.filter_sku("珀莱雅", "2026年7月1日到7月19日")
        common.filter_sku("珀莱雅", "2026年7月1日到7月19日", category="面霜")
        sql = fetch_df.call_args.args[0]
        self.assertIn("ai_bot_tmall_product_link", sql)
        self.assertIn("MAX(category_CN) AS category_cn", sql)
        self.assertIn("key_driver", sql)
        self.assertIn("GROUP BY item_id, key_driver", sql)
        self.assertIn("UNION ALL", sql)
        self.assertNotIn("sku_sales", sql)
        self.assertNotIn("SELECT *", sql.upper())
        self.assertEqual(fetch_df.call_count, 1)


class EcBrandResolutionTest(unittest.TestCase):
    def setUp(self):
        media_brand._SOURCE_RESOLUTION_CACHE.clear()

    @patch("bot.media_brand._load_aliases", return_value={})
    @patch("bot.media_brand.fetch_one", return_value={})
    @patch("bot.media_brand._tmall_brand_index_matches", return_value=())
    @patch("bot.media_brand._dictionary_rows", return_value=[])
    @patch("bot.media_brand._generate_brand_variants", return_value=("珀莱雅", "PROYA"))
    @patch("bot.media_brand._tmall_exact_fact_candidates", return_value=("PROYA",))
    def test_tmall_uses_indexed_fact_fallback_when_dictionary_is_stale(
        self,
        fact_candidates,
        _variants,
        _dictionary,
        _tmall_index,
        _cache,
        _aliases,
    ):
        result = media_brand.resolve_source_brand("珀莱雅", "tmall")
        self.assertEqual(result["brand"], "PROYA")
        self.assertEqual(result["match_method"], "fact_exact_alias")
        fact_candidates.assert_called_once_with(("珀莱雅", "PROYA"))

    @patch("bot.media_brand._write_source_resolution_cache")
    @patch("bot.media_brand._generate_brand_variants")
    @patch("bot.media_brand._load_aliases", return_value={})
    @patch("bot.media_brand.fetch_one", return_value={})
    @patch("bot.media_brand._tmall_brand_index_matches", return_value=())
    @patch("bot.media_brand._dictionary_rows")
    def test_router_alias_avoids_second_llm_even_when_names_are_cross_language(
        self,
        dictionary_rows,
        _tmall_index,
        _cache,
        _aliases,
        generate_variants,
        _write_cache,
    ):
        dictionary_rows.return_value = [{
            "source_name": "tmall",
            "source_brand": "PROYA",
            "normalized_brand": "proya",
        }]
        result = media_brand.resolve_source_brand(
            "珀莱雅",
            "tmall",
            brand_aliases=["珀莱雅", "PROYA"],
        )
        self.assertEqual(result["brand"], "PROYA")
        self.assertEqual(result["match_method"], "dictionary_exact")
        generate_variants.assert_not_called()

    @patch("bot.media_brand._read_source_resolution_cache")
    @patch("bot.media_brand._tmall_brand_index_matches", return_value=())
    @patch("bot.media_brand._dictionary_rows")
    def test_source_cache_is_used_without_dictionary_or_llm(
        self, dictionary_rows, _tmall_index, source_cache
    ):
        source_cache.return_value = {
            "source_brand": "GENERIC DB BRAND",
            "match_method": "dictionary_exact",
            "confidence": None,
        }
        result = media_brand.resolve_source_brand("任意中文品牌", "tmall")
        self.assertEqual(result["brand"], "GENERIC DB BRAND")
        self.assertEqual(result["match_method"], "source_cache")
        dictionary_rows.assert_not_called()

    @patch("bot.media_brand._write_source_resolution_cache")
    @patch("bot.media_brand._read_source_resolution_cache")
    @patch("bot.media_brand._tmall_brand_index_matches")
    @patch("bot.media_brand._dictionary_rows")
    @patch("bot.media_brand._generate_brand_variants")
    def test_chinese_store_index_wins_before_qwen_and_dictionary(
        self,
        generate_variants,
        dictionary_rows,
        tmall_index,
        source_cache,
        _write_cache,
    ):
        source_cache.return_value = {
            "source_brand": "WRONG OLD CACHE",
            "match_method": "llm:0.95",
        }
        tmall_index.side_effect = [(
            {
                "source_brand": "PROYA",
                "observation_count": 100,
                "alias_sources": "store_cn",
            },
        ), ()]
        result = media_brand.resolve_source_brand(
            "珀莱雅",
            "tmall",
            brand_aliases=["珀莱雅", "WRONG ENGLISH"],
        )
        self.assertEqual(result["brand"], "PROYA")
        self.assertEqual(result["match_method"], "tmall_store_exact")
        dictionary_rows.assert_not_called()
        generate_variants.assert_not_called()

    @patch("bot.media_brand._tmall_brand_index_matches")
    @patch("bot.media_brand._read_source_resolution_cache")
    def test_ambiguous_store_index_can_use_validated_cache(self, source_cache, tmall_index):
        source_cache.return_value = {
            "source_brand": "BRAND B",
            "match_method": "tmall_index_llm:0.98",
        }
        tmall_index.side_effect = [(
            {"source_brand": "BRAND A", "alias_sources": "store_cn"},
            {"source_brand": "BRAND B", "alias_sources": "store_cn"},
        ), ()]
        result = media_brand.resolve_source_brand("同名品牌", "tmall")
        self.assertEqual(result["brand"], "BRAND B")
        self.assertEqual(result["match_method"], "source_cache_validated")


class TmallBrandIndexPipelineTest(unittest.TestCase):
    def test_store_alias_builds_chinese_and_english_brand_variants(self):
        self.assertIn("珀莱雅", alias_variants("PROYA珀莱雅官方旗舰店", store_name=True))
        self.assertIn("proya", alias_variants("PROYA珀莱雅官方旗舰店", store_name=True))
        self.assertIn(
            "flowerknows",
            alias_variants("Flower Knows Official Flagship Store", store_name=True),
        )

    def test_fact_rows_create_store_and_brand_index_entries(self):
        rows = _index_rows_for_fact_rows([{
            "brand_name": "PROYA",
            "store_cn": "珀莱雅官方旗舰店",
            "store_en": "PROYA Official Store",
            "observation_count": 12,
        }])
        aliases = {(row["normalized_alias"], row["alias_source"]) for row in rows}
        self.assertIn(("proya", "brand_name"), aliases)
        self.assertIn(("珀莱雅", "store_cn"), aliases)
        self.assertIn(("proya", "store_en"), aliases)


class EcRouteTest(unittest.TestCase):
    @patch("bot.router.classify_user_intent")
    def test_default_analysis_without_period_clarifies(self, classify):
        classify.return_value = IntentResult(
            intent="default_analysis",
            brand="珀莱雅",
            brand_cn="珀莱雅",
            brand_en="PROYA",
            brand_aliases=["珀莱雅", "PROYA"],
            period=None,
            confidence="high",
        )
        result = route("分析珀莱雅的生意", SessionState())
        self.assertEqual(result.type, "clarify_period")
        self.assertEqual(result.brand, "珀莱雅")
        self.assertEqual(result.brand_aliases, ["珀莱雅", "PROYA"])

    def test_period_reply_resumes_pending_request(self):
        state = SessionState(pending_request={
            "intent": "default_analysis",
            "brand": "珀莱雅",
            "brand_aliases": ["珀莱雅", "PROYA"],
        })
        result = route("2026年7月1日到7月19日", state)
        self.assertEqual(result.type, "default_chain")
        self.assertEqual(result.brand, "珀莱雅")
        self.assertEqual(result.brand_aliases, ["珀莱雅", "PROYA"])
        self.assertEqual(result.period, "2026年7月1日到7月19日")


class EcDriverTest(unittest.TestCase):
    @patch("bot.tools.query_driver._series_keywords_for_brand", return_value={"系列A": {}})
    @patch("bot.tools.query_driver.filter_sku")
    def test_key_drivers_are_dynamic(self, filter_sku, _series_map):
        df = pd.DataFrame([
            {"bus_date": "2025-06-01", "item_id": "1", "product_title": "系列A", "category_cn": "面霜", "key_driver": "渠道A", "gmv": 80, "unit": 8},
            {"bus_date": "2026-06-01", "item_id": "1", "product_title": "系列A", "category_cn": "面霜", "key_driver": "渠道A", "gmv": 100, "unit": 10},
            {"bus_date": "2025-06-01", "item_id": "2", "product_title": "系列B", "category_cn": "面霜", "key_driver": "新渠道", "gmv": 20, "unit": 2},
            {"bus_date": "2026-06-01", "item_id": "2", "product_title": "系列B", "category_cn": "面霜", "key_driver": "新渠道", "gmv": 50, "unit": 5},
        ])
        df["bus_date"] = pd.to_datetime(df["bus_date"])
        df.attrs["ec_context"] = {
            "source_brand": "PROYA",
            "current_start": "2026-06-01",
            "current_end": "2026-06-30",
            "prior_start": "2025-06-01",
            "prior_end": "2025-06-30",
        }
        filter_sku.return_value = df
        result = query_driver("珀莱雅", "2026年6月")
        self.assertNotIn("error", result)
        self.assertEqual(
            [row["key_driver"] for row in result["driver_summary"]["drivers"]],
            ["渠道A", "新渠道"],
        )
        self.assertNotIn("live_reference", result)


class EcDefaultChainBrandTest(unittest.TestCase):
    @patch("bot.chains.default_chain.format_report", return_value="report")
    @patch("bot.chains.default_chain.build_fraud_result", return_value={})
    @patch("bot.chains.default_chain.query_driver", return_value={})
    @patch("bot.chains.default_chain.query_sku_list", return_value={"product_lines": []})
    @patch("bot.chains.default_chain.query_series", return_value={"series": []})
    @patch(
        "bot.chains.default_chain.query_ecip_tmall_gmv",
        return_value={"total": {"gmv_current": 100, "gmv_prior": 90, "evol": .1111}},
    )
    @patch("bot.chains.default_chain.query_category")
    def test_display_brand_and_source_brand_stay_separate(
        self,
        query_category,
        query_ecip,
        query_series,
        query_sku,
        query_driver_mock,
        fraud,
        _format,
    ):
        query_category.return_value = {
            "brand": "珀莱雅",
            "source_brand": "PROYA",
            "categories": [{"category_cn": "面霜", "weight": 1, "evol": 0.1}],
        }
        aliases = ["珀莱雅", "PROYA"]
        result = run_default_chain(
            "珀莱雅",
            "2026年7月1日到7月10日",
            brand_aliases=aliases,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["meta"]["brand"], "珀莱雅")
        self.assertEqual(result["meta"]["tmall_brand"], "PROYA")
        self.assertEqual(result["meta"]["brand_aliases"], aliases)
        for mock_call in (
            query_category,
            query_ecip,
            query_series,
            query_sku,
            query_driver_mock,
            fraud,
        ):
            self.assertEqual(mock_call.call_args.kwargs["brand_aliases"], aliases)


class EcipTmallGmvTest(unittest.TestCase):
    @patch("bot.tools.query_ecip_tmall_gmv.fetch_df")
    @patch("bot.tools.query_ecip_tmall_gmv.ec_query_context")
    def test_ttl_gmv_uses_ecip_mass_and_three_business_categories(
        self,
        context,
        fetch_df,
    ):
        context.return_value = {
            "input_brand": "珀莱雅",
            "source_brand": "PROYA",
            "current_start": "2026-07-01",
            "current_end": "2026-07-10",
            "prior_start": "2025-07-01",
            "prior_end": "2025-07-10",
        }
        monthly = pd.DataFrame([
            {"period_key": "current", "source_month": "2026-07", "row_count": 1, "gmv": 9999},
            {"period_key": "prior", "source_month": "2025-07", "row_count": 1, "gmv": 8888},
        ])
        daily = pd.DataFrame([
            {"period_key": "current", "source_month": "2026-07", "row_count": 30, "gmv": 600},
            {"period_key": "prior", "source_month": "2025-07", "row_count": 20, "gmv": 400},
        ])
        fetch_df.side_effect = [monthly, daily]
        result = query_ecip_tmall_gmv(
            "珀莱雅",
            "2026年7月1日到7月10日",
            brand_aliases=["珀莱雅", "PROYA"],
        )
        self.assertEqual(result["total"]["gmv_current"], 600)
        self.assertEqual(result["total"]["gmv_prior"], 400)
        self.assertEqual(result["total"]["evol"], .5)
        monthly_sql = fetch_df.call_args_list[0].args[0]
        daily_sql = fetch_df.call_args_list[1].args[0]
        params = fetch_df.call_args_list[1].args[1]
        self.assertIn("three_platform_store_rank_monthly", monthly_sql)
        self.assertIn("UPPER(TRIM(platform)) IN ('TM', 'TMALL')", monthly_sql)
        self.assertIn("tmall_store_ranking_day_jiashicang", daily_sql)
        for sql in (monthly_sql, daily_sql):
            self.assertIn("category_EN_level_1", sql)
            self.assertIn("'Skincare'", sql)
            self.assertIn("'Hair'", sql)
            self.assertIn("'Makeup + Fragrance'", sql)
            self.assertNotIn("'Fragrance'", sql)
        self.assertEqual(params["current_start_slash"], "2026/07/01")
        self.assertEqual(params["prior_start_slash"], "2025/07/01")
        self.assertEqual(result["coverage"]["daily_used"], ["2025-07", "2026-07"])
        self.assertEqual(result["coverage"]["monthly_used"], [])

    @patch("bot.tools.query_ecip_tmall_gmv.fetch_df")
    @patch("bot.tools.query_ecip_tmall_gmv.ec_query_context")
    def test_full_month_uses_monthly_and_partial_month_uses_daily(self, context, fetch_df):
        context.return_value = {
            "input_brand": "珀莱雅", "source_brand": "PROYA",
            "current_start": "2026-01-01", "current_end": "2026-02-10",
            "prior_start": "2025-01-01", "prior_end": "2025-02-10",
        }
        fetch_df.side_effect = [
            pd.DataFrame([
                {"period_key": "current", "source_month": "2026-01", "row_count": 1, "gmv": 1000},
                {"period_key": "current", "source_month": "2026-02", "row_count": 1, "gmv": 9999},
                {"period_key": "prior", "source_month": "2025-01", "row_count": 1, "gmv": 800},
                {"period_key": "prior", "source_month": "2025-02", "row_count": 1, "gmv": 8888},
            ]),
            pd.DataFrame([
                {"period_key": "current", "source_month": "2026-01", "row_count": 31, "gmv": 900},
                {"period_key": "current", "source_month": "2026-02", "row_count": 10, "gmv": 100},
                {"period_key": "prior", "source_month": "2025-01", "row_count": 31, "gmv": 700},
                {"period_key": "prior", "source_month": "2025-02", "row_count": 10, "gmv": 80},
            ]),
        ]
        result = query_ecip_tmall_gmv("珀莱雅", "2026年1月1日到2月10日")
        self.assertEqual(result["total"]["gmv_current"], 1100)
        self.assertEqual(result["total"]["gmv_prior"], 880)
        self.assertEqual(result["total"]["evol"], .25)
        self.assertEqual(result["coverage"]["monthly_used"], ["2025-01", "2026-01"])
        self.assertEqual(result["coverage"]["daily_used"], ["2025-02", "2026-02"])

    @patch("bot.tools.query_ecip_tmall_gmv.fetch_df")
    @patch("bot.tools.query_ecip_tmall_gmv.ec_query_context")
    def test_full_month_without_monthly_data_falls_back_to_daily(self, context, fetch_df):
        context.return_value = {
            "input_brand": "珀莱雅", "source_brand": "PROYA",
            "current_start": "2026-03-01", "current_end": "2026-03-31",
            "prior_start": "2025-03-01", "prior_end": "2025-03-31",
        }
        fetch_df.side_effect = [
            pd.DataFrame(columns=["period_key", "source_month", "row_count", "gmv"]),
            pd.DataFrame([
                {"period_key": "current", "source_month": "2026-03", "row_count": 31, "gmv": 300},
                {"period_key": "prior", "source_month": "2025-03", "row_count": 31, "gmv": 250},
            ]),
        ]
        result = query_ecip_tmall_gmv("珀莱雅", "2026年3月")
        self.assertEqual(result["total"]["gmv_current"], 300)
        self.assertEqual(result["total"]["gmv_prior"], 250)
        self.assertEqual(result["total"]["evol"], .2)
        self.assertEqual(result["coverage"]["monthly_used"], [])
        self.assertEqual(result["coverage"]["daily_used"], ["2025-03", "2026-03"])

    def test_overall_module_uses_ecip_total_not_product_link_total(self):
        rendered = render_module_overall(
            {
                "brand": "珀莱雅",
                "period_meta": {
                    "current_label": "2026年7月1日—7月10日",
                    "prior_label": "2025年7月1日—7月10日",
                },
                "total": {"gmv_current": 100, "gmv_prior": 80, "evol": .25},
            },
            ttl_result={
                "total": {"gmv_current": 600, "gmv_prior": 400, "evol": .5},
            },
        )
        self.assertIn("天猫总GMV为0.6K", rendered)
        self.assertIn("同比+50%", rendered)
        self.assertNotIn("天猫总GMV为0.1K", rendered)

    def test_report_lists_two_data_sources(self):
        rendered = format_report(
            category_result={
                "brand": "珀莱雅",
                "period": "2026年7月1日到7月10日",
                "period_meta": {"source_max_date": "2026-07-19"},
                "total": {"gmv_current": 0, "gmv_prior": 0, "evol": None},
                "categories": [],
            },
            driver_result={"error": "no_data", "message": "无数据"},
            sku_result={"error": "no_data", "message": "无数据"},
            ttl_result={"total": {"gmv_current": 0, "gmv_prior": 0, "evol": None}},
        )
        self.assertIn("天猫品牌旗舰店链接：百库驾驶舱-天猫-商品-日表", rendered)
        self.assertIn(
            "TTL GMV：ECIP MASS Pure Mass Market Ranking (TTL Beauty)",
            rendered,
        )


class EcReportSeriesDisclaimerTest(unittest.TestCase):
    @patch("bot.formatter._gen_bullets_loose", return_value="• 面霜占比最高。")
    def test_category_summary_does_not_claim_ai_generated_core_series(self, _bullets):
        category_result = {
            "total": {"gmv_current": 100, "gmv_prior": 90, "evol": 0.1111},
            "categories": [{
                "category_cn": "面霜",
                "gmv_current": 100,
                "gmv_prior": 90,
                "weight": 1,
                "evol": 0.1111,
            }],
        }
        sku_result = {
            "product_lines": [{"product_line": "AI归纳系列"}],
            "top_skus": [{"product_title": "Top商品链接"}],
        }
        result = render_module_category(category_result, "面霜", sku_result)
        self.assertNotIn("核心系列", result)
        self.assertNotIn("AI归纳系列", result)
        self.assertIn("Top链接为「Top商品链接」", result)

    def test_series_section_contains_gray_ai_disclaimer(self):
        result = render_module_drilldown(
            {
                "product_lines": [{
                    "product_line": "归纳系列",
                    "gmv_current": 100,
                    "gmv_prior": 90,
                    "evol": 0.1111,
                }],
                "top_skus": [{
                    "item_id": "1",
                    "product_title": "商品链接",
                    "key_driver": "渠道",
                    "gmv_current": 100,
                    "gmv_prior": 90,
                    "unit_current": 1,
                    "unit_prior": 1,
                    "weight": 1,
                    "evol": 0.1111,
                }],
            },
            "面霜",
            "",
        )
        note = "> _产品系列由AI根据产品链接归纳总结，存在误差。_"
        self.assertIn(note, result)
        blocks = markdown_to_items(note)
        run = blocks[0].text.elements[0].text_run
        self.assertTrue(run.text_element_style.italic)
        self.assertEqual(run.text_element_style.text_color, 7)


if __name__ == "__main__":
    unittest.main()
