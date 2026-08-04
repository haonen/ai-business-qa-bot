from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
import time

import pandas as pd

from bot.chains.media_chain import run_media_chain
from bot.chains.default_chain import run_default_chain
from bot.feishu_doc import _compact_wide_table, markdown_to_items
from bot.media_formatter import (
    _dimension_bullets,
    _mix_bullets,
    _render_kol_platform,
    _render_media,
    format_media_report,
)
from bot.media_brand import (
    ResolvedBrands,
    _select_source_mappings,
    _validate_selected_brand,
    resolve_media_brand,
)
from bot.media_period import normalize_media_period_hint, parse_media_period
from bot.router import IntentResult, route
from bot.session import SessionState
from bot.tools.query_kol_performance import query_kol_performance
from bot.tools.query_ec_nso import query_ec_nso
from bot.tools.query_media_investment import query_media_investment
from bot.tools.query_douyin_gmv import query_douyin_gmv
from bot.tools.query_social_search import query_social_search
from bot.utils import detect_brand_hint, normalize_period_hint, parse_period


class MediaPeriodTest(unittest.TestCase):
    def test_single_month_uses_ytd_search(self):
        parsed = parse_media_period("2026年3月")
        self.assertEqual(parsed.focus_start, "2026-03-01")
        self.assertEqual(parsed.focus_end, "2026-03-31")
        self.assertEqual(parsed.prior_start, "2025-03-01")
        self.assertEqual(parsed.search_start, "2026-01-01")
        self.assertEqual(parsed.search_end, "2026-03-31")

    def test_month_range(self):
        parsed = parse_media_period("2026年1-4月")
        self.assertEqual(parsed.focus_end, "2026-04-30")
        self.assertEqual(parsed.display, "2026年1–4月")


class SharedPeriodTest(unittest.TestCase):
    def test_tmall_single_month(self):
        parsed = parse_period("5月")
        self.assertEqual(parsed["y2026"], ("2026-05-01", "2026-05-31"))
        self.assertEqual(parsed["y2025"], ("2025-05-01", "2025-05-31"))

    def test_tmall_month_range(self):
        parsed = parse_period("1-4月")
        self.assertEqual(parsed["y2026"], ("2026-01-01", "2026-04-30"))
        self.assertEqual(parsed["y2025"], ("2025-01-01", "2025-04-30"))

    def test_shared_period_and_brand_detection(self):
        text = "谷雨5月的生意如何"
        self.assertEqual(normalize_period_hint(text), "5月")
        self.assertEqual(detect_brand_hint(text), "谷雨")

    def test_month_without_year_defaults_to_2026(self):
        hint = normalize_media_period_hint("谷雨5月的媒体投资如何")
        self.assertEqual(hint, "5月")
        parsed = parse_media_period(hint)
        self.assertEqual(parsed.focus_start, "2026-05-01")
        self.assertEqual(parsed.focus_end, "2026-05-31")

    def test_month_range_without_year_defaults_to_2026(self):
        hint = normalize_media_period_hint("分析谷雨1-4月BET")
        self.assertEqual(hint, "1-4月")
        parsed = parse_media_period(hint)
        self.assertEqual(parsed.focus_start, "2026-01-01")
        self.assertEqual(parsed.focus_end, "2026-04-30")

    def test_extract_full_date_range_before_iso_month(self):
        value = normalize_media_period_hint("分析2026-05-01~2026-05-31媒体投资")
        self.assertEqual(value, "2026-05-01~2026-05-31")

    def test_reject_non_2026(self):
        with self.assertRaises(ValueError):
            parse_media_period("2025年3月")

    def test_inherited_day_range_converts_to_covered_months(self):
        parsed = parse_media_period("2026年5月13日到6月3日")
        self.assertEqual(parsed.focus_start, "2026-05-01")
        self.assertEqual(parsed.focus_end, "2026-06-30")


class MediaRouterTest(unittest.TestCase):
    @patch("bot.router.classify_user_intent")
    def test_qwen_media_parse_returns_bilingual_brand_parameters(self, mock_classify):
        mock_classify.return_value = IntentResult(
            intent="media_analysis",
            brand="韩束",
            brand_cn="韩束",
            brand_en="KANS",
            brand_aliases=["韩束", "KANS", "Kans"],
            period="4月",
            media_scope="media_investment",
            confidence="high",
        )
        result = route("韩束4月的媒体投资是什么样的", SessionState())
        self.assertEqual(result.type, "media_analysis")
        self.assertEqual(result.brand, "韩束")
        self.assertEqual(result.brand_aliases, ["韩束", "KANS", "Kans"])
        self.assertEqual(result.period, "4月")
        self.assertEqual(result.media_scope, "media_investment")

    def test_media_route_without_followup_prefix(self):
        result = route("分析2026年3月谷雨的媒体投资", SessionState())
        self.assertEqual(result.type, "media_analysis")
        self.assertEqual(result.brand, "谷雨")
        self.assertEqual(result.period, "2026年3月")

    def test_media_route_month_without_year_does_not_pollute_brand(self):
        result = route("谷雨5月的媒体投资如何", SessionState())
        self.assertEqual(result.type, "media_analysis")
        self.assertEqual(result.brand, "谷雨")
        self.assertEqual(result.period, "5月")

    def test_media_route_does_not_include_question_suffix_in_chinese_brand(self):
        result = route("韩束4月的媒体投资是什么样的", SessionState())
        self.assertEqual(result.type, "media_analysis")
        self.assertEqual(result.brand, "韩束")
        self.assertEqual(result.period, "4月")

    def test_media_route_does_not_include_question_suffix_in_english_brand(self):
        result = route("KANS4月的媒体投资是什么样的", SessionState())
        self.assertEqual(result.type, "media_analysis")
        self.assertEqual(result.brand, "KANS")
        self.assertEqual(result.period, "4月")

    def test_media_route_inherits_context(self):
        state = SessionState()
        state.drilldown_ctx.brand = "谷雨"
        state.drilldown_ctx.period = "2026年3月"
        result = route("那它媒体投资如何？", state)
        self.assertEqual(result.type, "media_analysis")
        self.assertEqual(result.brand, "谷雨")
        self.assertEqual(result.period, "2026年3月")

    @patch("bot.router.classify_user_intent")
    def test_followup_prefix_overrides_full_media_report(self, mock_classify):
        mock_classify.return_value = IntentResult(
            intent="followup",
            followup_text="KOL performance如何",
            confidence="high",
        )
        state = SessionState()
        state.drilldown_ctx.brand = "谷雨"
        state.drilldown_ctx.period = "2026年3月"
        result = route("追问：KOL performance如何", state)
        self.assertEqual(result.type, "skill_dispatch")
        self.assertEqual(result.followup_text, "KOL performance如何")

    @patch("bot.router.classify_user_intent", return_value=None)
    def test_tmall_route_understands_month_without_year(self, _mock_intent):
        result = route("谷雨5月的生意如何", SessionState())
        self.assertEqual(result.type, "default_chain")
        self.assertEqual(result.brand, "谷雨")
        self.assertEqual(result.period, "5月")


class DefaultChainFailureTest(unittest.TestCase):
    @patch("bot.chains.default_chain.query_category")
    def test_failed_query_does_not_create_empty_document(self, mock_query):
        mock_query.return_value = {"error": "execution_error", "message": "查询失败"}
        result = run_default_chain("谷雨", "5月")
        self.assertFalse(result["ok"])
        self.assertFalse(result["meta"]["document_ready"])


class MediaToolTest(unittest.TestCase):
    @patch("bot.tools.query_social_search.fetch_df")
    def test_search_keeps_category_month_separate(self, mock_fetch):
        mock_fetch.return_value = pd.DataFrame([
            {
                "report_month": "2026-01-01",
                "grain_level": "brand",
                "brand": "Brand A",
                "category": None,
                "current_rank": 2,
                "current_search_index": 100,
                "previous_search_index": 80,
                "calculated_yoy_rate": 0.25,
            },
            {
                "report_month": "2026-02-01",
                "grain_level": "brand",
                "brand": "Brand A",
                "category": None,
                "current_rank": 1,
                "current_search_index": 120,
                "previous_search_index": 100,
                "calculated_yoy_rate": 0.20,
            },
            {
                "report_month": "2026-03-01",
                "grain_level": "brand_category",
                "brand": "Brand A",
                "category": "护肤",
                "current_rank": 3,
                "current_search_index": 70,
                "previous_search_index": 60,
                "calculated_yoy_rate": 0.1667,
            },
        ])
        result = query_social_search("Brand A", "2026-01-01", "2026-03-31")
        self.assertNotIn("error", result)
        self.assertNotIn("mom", result["monthly"][1])
        self.assertNotIn("rank", result["monthly"][1])
        self.assertEqual(result["monthly"][2]["grain"], "Category only")
        self.assertIsNone(result["monthly"][2]["actual"])
        self.assertEqual(result["coverage"]["category_only_months"], ["2026-03"])

    @patch("bot.tools.query_media_investment.fetch_df")
    def test_media_mix_only_returns_weight_and_change_contract(self, mock_fetch):
        mock_fetch.return_value = pd.DataFrame([
            {"year": 2026, "period_month": "2026-03-01", "media": "Tmall", "submedia": None, "ait_roe": "Transaction", "bkfs_overall": "B", "bkfs_xiaohongshu": "B", "bkfs_douyin": None, "spend_million": 60, "row_count": 1},
            {"year": 2026, "period_month": "2026-03-01", "media": "Live Streaming", "submedia": "Viya&Austin", "ait_roe": "Transaction", "bkfs_overall": "K", "bkfs_xiaohongshu": "K", "bkfs_douyin": None, "spend_million": 40, "row_count": 1},
            {"year": 2025, "period_month": "2025-03-01", "media": "Tmall", "submedia": None, "ait_roe": "Transaction", "bkfs_overall": "B", "bkfs_xiaohongshu": "B", "bkfs_douyin": None, "spend_million": 50, "row_count": 1},
            {"year": 2025, "period_month": "2025-03-01", "media": "Live Stream", "submedia": "Austin", "ait_roe": "Transaction", "bkfs_overall": "K", "bkfs_xiaohongshu": "K", "bkfs_douyin": None, "spend_million": 50, "row_count": 1},
        ])
        result = query_media_investment(
            "Brand A", "2026-03-01", "2026-03-31", "2025-03-01", "2025-03-31"
        )
        self.assertEqual(result["ttl"]["actual_yuan"], 100_000_000)
        self.assertEqual(result["mix"]["overall"][0]["weight"], 0.6)
        self.assertEqual(result["mix"]["overall"][0]["weight_change"], 0.1)
        self.assertEqual(result["channels"]["tmall"]["actual_yuan"], 100_000_000)
        self.assertEqual(result["ait"][2]["weight"], 1.0)
        self.assertEqual(result["transaction_platforms"][0]["weight"], 1.0)

    @patch("bot.tools.query_media_investment.fetch_df")
    def test_transaction_platform_excludes_non_transaction_rows(self, mock_fetch):
        mock_fetch.return_value = pd.DataFrame([
            {"year": 2026, "period_month": "2026-03-01", "media": "Tmall", "submedia": "", "ait_roe": "Awareness", "bkfs_overall": "B", "bkfs_xiaohongshu": None, "bkfs_douyin": None, "spend_million": 90, "row_count": 1},
            {"year": 2026, "period_month": "2026-03-01", "media": "Tmall", "submedia": "", "ait_roe": "Transaction", "bkfs_overall": "T", "bkfs_xiaohongshu": None, "bkfs_douyin": "T", "spend_million": 10, "row_count": 1},
            {"year": 2025, "period_month": "2025-03-01", "media": "Tmall", "submedia": "", "ait_roe": "Awareness", "bkfs_overall": "B", "bkfs_xiaohongshu": None, "bkfs_douyin": None, "spend_million": 80, "row_count": 1},
            {"year": 2025, "period_month": "2025-03-01", "media": "Tmall", "submedia": "", "ait_roe": "Transaction", "bkfs_overall": "T", "bkfs_xiaohongshu": None, "bkfs_douyin": "T", "spend_million": 20, "row_count": 1},
        ])
        result = query_media_investment(
            "Brand A", "2026-03-01", "2026-03-31", "2025-03-01", "2025-03-31"
        )
        self.assertEqual(result["ttl"]["actual_yuan"], 100_000_000)
        self.assertEqual(result["transaction_platforms"][0]["actual_yuan"], 10_000_000)
        self.assertEqual(result["transaction_platforms"][0]["weight"], 1.0)
        self.assertEqual(sum(row["actual_yuan"] for row in result["ait"]), 100_000_000)

    @patch("bot.tools.query_ec_nso.fetch_df")
    def test_ec_nso_uses_ttl_platform_and_prior_period(self, mock_fetch):
        mock_fetch.return_value = pd.DataFrame([
            {"period_type": "current", "year": 2026, "month": 3, "nso": 200_000_000, "row_count": 1},
            {"period_type": "prior", "year": 2025, "month": 3, "nso": 160_000_000, "row_count": 1},
        ])
        result = query_ec_nso(
            "PROYA", "2026-03-01", "2026-03-31", "2025-03-01", "2025-03-31"
        )
        self.assertEqual(result["nso_actual"], 200_000_000)
        self.assertEqual(result["evol"], 0.25)
        sql = mock_fetch.call_args.args[0]
        self.assertIn("FROM top_brands_total_ec", sql)
        self.assertIn("platform = 'TTL'", sql)

    @patch("bot.tools.query_douyin_gmv.fetch_df")
    def test_douyin_gmv_uses_sales_amount_field(self, mock_fetch):
        mock_fetch.return_value = pd.DataFrame([
            {
                "period_type": "current",
                "gmv": 120_000_000,
                "row_count": 20,
                "min_date": "2026-03-01",
                "max_date": "2026-03-31",
            },
            {
                "period_type": "prior",
                "gmv": 100_000_000,
                "row_count": 18,
                "min_date": "2025-03-01",
                "max_date": "2025-03-31",
            },
        ])
        result = query_douyin_gmv(
            "珀莱雅", "2026-03-01", "2026-03-31", "2025-03-01", "2025-03-31"
        )
        self.assertEqual(result["gmv_actual"], 120_000_000)
        self.assertEqual(result["evol"], 0.2)
        sql = mock_fetch.call_args.args[0]
        self.assertIn("SUM(`销售额`)", sql)
        self.assertIn("`商品品牌` = :brand", sql)

    @patch("bot.tools.query_kol_performance.fetch_df")
    def test_kol_cpe_is_ratio_of_aggregated_sums(self, mock_fetch):
        summary = pd.DataFrame([
            {"year": 2026, "period_month": "2026-03-01", "tier": "A", "kol_type": "美妆", "cost": 100, "engage": 10, "row_count": 1},
            {"year": 2026, "period_month": "2026-03-01", "tier": "A", "kol_type": "生活", "cost": 300, "engage": 90, "row_count": 1},
            {"year": 2025, "period_month": "2025-03-01", "tier": "A", "kol_type": "美妆", "cost": 200, "engage": 50, "row_count": 1},
        ])
        top = pd.DataFrame([
            {"nickname": "KOL 1", "tier": "A", "kol_type": "美妆", "cost": 500, "engage": 100},
        ])
        mock_fetch.side_effect = [summary, top]
        result = query_kol_performance(
            "Brand A", "red", "2026-03-01", "2026-03-31", "2025-03-01", "2025-03-31"
        )
        self.assertEqual(result["by_tier"][0]["cpe"], 4.0)
        self.assertEqual(result["top_kol"][0]["cpe"], 5.0)

    @patch("bot.tools.query_media_investment.fetch_df")
    def test_proya_uses_2025_actual_for_evol(self, mock_fetch):
        mock_fetch.return_value = pd.DataFrame([
            {
                "year": 2026, "period_month": "2026-03-01",
                "bkfs_overall": "S", "bkfs_xiaohongshu": None, "bkfs_douyin": "S",
                "spend_million": 242.371547, "row_count": 1032,
            },
            {
                "year": 2025, "period_month": "2025-03-01",
                "bkfs_overall": "S", "bkfs_xiaohongshu": None, "bkfs_douyin": "S",
                "spend_million": 232.837441, "row_count": 980,
            },
        ])
        result = query_media_investment(
            "PROYA", "2026-03-01", "2026-03-31", "2025-03-01", "2025-03-31"
        )
        self.assertEqual(result["matched_brand"], "PROYA")
        self.assertEqual(result["coverage"]["current_rows"], 1032)
        self.assertEqual(result["coverage"]["prior_rows"], 980)
        self.assertEqual(result["ttl"]["comparison_status"], "ok")
        self.assertAlmostEqual(result["ttl"]["actual_million"], 242.371547)
        self.assertEqual(result["ttl"]["evol"], 0.0409)

        rendered = _render_media(
            result,
            {
                "nso_actual": 100_000_000,
                "nso_prior": 90_000_000,
                "evol": 0.1111,
                "comparison_status": "ok",
            },
        )
        self.assertIn("242.4M", rendered)
        self.assertNotIn("新增", rendered)

    @patch("bot.tools.query_media_investment.fetch_df")
    def test_missing_prior_is_not_new_investment(self, mock_fetch):
        mock_fetch.return_value = pd.DataFrame([{
            "year": 2026, "period_month": "2026-03-01",
            "bkfs_overall": "S", "bkfs_xiaohongshu": None, "bkfs_douyin": "S",
            "spend_million": 10, "row_count": 12,
        }])
        result = query_media_investment(
            "PROYA", "2026-03-01", "2026-03-31", "2025-03-01", "2025-03-31"
        )
        self.assertEqual(result["ttl"]["comparison_status"], "missing_prior")
        self.assertIsNone(result["ttl"]["evol"])
        self.assertEqual(
            result["mix"]["overall"][3]["weight_comparison_status"],
            "missing_prior",
        )
        rendered = _render_media(
            result,
            {
                "gmv_actual": 100_000_000,
                "gmv_prior": 90_000_000,
                "evol": 0.1111,
                "comparison_status": "ok",
            },
        )
        self.assertIn("2025年无数据", rendered)
        self.assertNotIn("基期为0", rendered)
        self.assertNotIn("新增", rendered)

    @patch("bot.tools.query_kol_performance.fetch_df")
    def test_kol_distinguishes_no_platform_investment(self, mock_fetch):
        mock_fetch.side_effect = [
            pd.DataFrame(),
            pd.DataFrame([{"year": 2026, "row_count": 25}]),
        ]
        result = query_kol_performance(
            "PROYA", "red", "2026-03-01", "2026-03-31",
            "2025-03-01", "2025-03-31",
        )
        self.assertEqual(result["error"], "no_platform_investment")
        self.assertEqual(result["matched_brand"], "PROYA")

    @patch("bot.tools.query_kol_performance.fetch_df")
    def test_kol_distinguishes_missing_brand_period_data(self, mock_fetch):
        mock_fetch.side_effect = [pd.DataFrame(), pd.DataFrame()]
        result = query_kol_performance(
            "PROYA", "red", "2026-03-01", "2026-03-31",
            "2025-03-01", "2025-03-31",
        )
        self.assertEqual(result["error"], "period_data_missing")


class MediaBrandTest(unittest.TestCase):
    def setUp(self):
        # Brand-resolution unit tests must not inherit rows from a developer or
        # server-side Tmall brand index/alias file. Each test declares candidates.
        index_patcher = patch(
            "bot.media_brand._tmall_brand_index_matches",
            return_value=(),
        )
        alias_patcher = patch("bot.media_brand._load_aliases", return_value={})
        index_patcher.start()
        alias_patcher.start()
        self.addCleanup(index_patcher.stop)
        self.addCleanup(alias_patcher.stop)

    @patch("bot.media_brand._generate_brand_variants")
    @patch("bot.media_brand._dictionary_rows")
    @patch("bot.media_brand._read_resolution_cache", return_value=None)
    @patch("bot.media_brand._write_resolution_cache")
    def test_bilingual_router_parameters_resolve_all_sources_without_extra_llm(
        self, mock_write, _mock_cache, mock_dictionary, mock_generate
    ):
        mock_dictionary.return_value = [
            {"source_name": "search", "source_brand": "韩束", "normalized_brand": "韩束"},
            {"source_name": "topline", "source_brand": "KANS", "normalized_brand": "kans"},
            {"source_name": "ksi", "source_brand": "KANS", "normalized_brand": "kans"},
            {"source_name": "tmall", "source_brand": "韩束", "normalized_brand": "韩束"},
            {"source_name": "dy", "source_brand": "韩束", "normalized_brand": "韩束"},
        ]
        result = resolve_media_brand("韩束", brand_aliases=["韩束", "KANS"])
        self.assertEqual(
            result["resolved"],
            {
                "search": "韩束",
                "topline": "KANS",
                "ksi": "KANS",
                "tmall": "韩束",
                "dy": "韩束",
            },
        )
        mock_generate.assert_not_called()
        mock_write.assert_called_once()

    @patch("bot.media_brand._dictionary_rows")
    @patch("bot.media_brand._read_resolution_cache", return_value=None)
    @patch("bot.media_brand._write_resolution_cache")
    def test_proya_dictionary_mapping_is_complete(
        self, mock_write, _mock_cache, mock_dictionary
    ):
        mock_dictionary.return_value = [
            {"source_name": "search", "source_brand": "珀莱雅", "normalized_brand": "珀莱雅"},
            {"source_name": "topline", "source_brand": "PROYA", "normalized_brand": "珀莱雅"},
            {"source_name": "ksi", "source_brand": "PROYA", "normalized_brand": "珀莱雅"},
            {"source_name": "tmall", "source_brand": "珀莱雅", "normalized_brand": "珀莱雅"},
            {"source_name": "dy", "source_brand": "珀莱雅", "normalized_brand": "珀莱雅"},
        ]
        result = resolve_media_brand("珀莱雅")
        self.assertEqual(result["resolved"]["topline"], "PROYA")
        self.assertEqual(result["resolved"]["ksi"], "PROYA")
        self.assertEqual(result["resolved"]["search"], "珀莱雅")
        self.assertEqual(result["resolved"]["tmall"], "珀莱雅")
        self.assertEqual(result["resolved"]["dy"], "珀莱雅")
        mock_write.assert_called_once()

    @patch("bot.media_brand._dictionary_rows")
    @patch("bot.media_brand._generate_brand_variants", return_value=("珀莱雅", "PROYA"))
    @patch("bot.media_brand._read_resolution_cache", return_value=None)
    def test_one_missing_source_returns_partial_resolution(
        self, _mock_cache, _mock_variants, mock_dictionary
    ):
        rows = [
            {"source_name": "search", "source_brand": "珀莱雅", "normalized_brand": "珀莱雅"},
            {"source_name": "topline", "source_brand": "PROYA", "normalized_brand": "proya"},
            {"source_name": "ksi", "source_brand": "PROYA", "normalized_brand": "proya"},
            {"source_name": "dy", "source_brand": "珀莱雅", "normalized_brand": "珀莱雅"},
        ]
        mock_dictionary.side_effect = [rows, rows]
        result = resolve_media_brand("珀莱雅")
        self.assertNotIn("error", result)
        self.assertEqual(result["missing_sources"], ["tmall"])
        self.assertIsNone(result["resolved"]["tmall"])
        self.assertEqual(result["resolved"]["topline"], "PROYA")

    def test_llm_output_cannot_invent_brand(self):
        self.assertIsNone(_validate_selected_brand("模型编造的品牌", ("GUYU", "Guyue")))

    @patch("bot.media_brand.llm_client")
    def test_high_confidence_llm_candidate_is_accepted(self, mock_client):
        response = MagicMock()
        response.choices = [
            MagicMock(
                message=MagicMock(
                    content='{"mappings":{"topline":{"brand":"GUYU","confidence":0.97}}}'
                )
            )
        ]
        mock_client.return_value.chat.completions.create.return_value = response
        with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "test"}, clear=False):
            selected, suggestions, confidence = _select_source_mappings(
                "谷雨", {"topline": ("GUYU", "Guyue")}
            )
        self.assertEqual(selected, {"topline": "GUYU"})
        self.assertEqual(suggestions, {})
        self.assertEqual(confidence, 0.97)

    @patch("bot.media_brand._dictionary_rows")
    @patch("bot.media_brand._read_resolution_cache")
    def test_cache_hit_does_not_query_dictionary(self, mock_cache, mock_dictionary):
        mock_cache.return_value = ResolvedBrands("珀莱雅", "PROYA", "PROYA", "珀莱雅", "珀莱雅")
        result = resolve_media_brand("珀莱雅")
        self.assertEqual(result["resolved"]["topline"], "PROYA")
        self.assertEqual(set(result["match_methods"].values()), {"cache"})
        mock_dictionary.assert_not_called()

    @patch("bot.media_brand._generate_brand_variants", return_value=("未知品牌",))
    @patch("bot.media_brand.fetch_df")
    @patch("bot.media_brand._read_resolution_cache", return_value=None)
    def test_resolution_reads_only_lookup_tables_not_fact_tables(
        self, _mock_cache, mock_fetch, _mock_variants
    ):
        mock_fetch.return_value = pd.DataFrame()
        result = resolve_media_brand("未知品牌")
        self.assertEqual(result["error"], "brand_not_found")
        self.assertGreaterEqual(mock_fetch.call_count, 1)
        for call in mock_fetch.call_args_list:
            sql = call.args[0]
            self.assertTrue(
                "ai_bot_brand_dictionary" in sql or "ai_bot_tmall_brand_index" in sql
            )
            self.assertNotIn("ai_bot_media_topline_investment", sql)
            self.assertNotIn("ai_bot_media_ksi_performance", sql)
            self.assertNotIn("ai_bot_media_search_index", sql)
            self.assertNotIn("ai_bot_tmall_product_link", sql)
            self.assertNotIn("ai_bot_dy_product_link", sql)


class MediaChainIntegrityTest(unittest.TestCase):
    @patch("bot.chains.media_chain.query_ec_nso")
    @patch("bot.chains.media_chain.query_kol_performance")
    @patch("bot.chains.media_chain.query_media_investment")
    @patch("bot.chains.media_chain.query_social_search")
    @patch("bot.chains.media_chain.resolve_source_brand")
    @patch("bot.chains.media_chain.resolve_media_brand")
    def test_incomplete_brand_resolution_stops_all_queries(
        self, mock_resolve, mock_resolve_nso, mock_search, mock_investment,
        mock_kol, mock_nso
    ):
        mock_resolve.return_value = {
            "error": "brand_not_found",
            "message": "KSI没有可验证候选",
        }
        result = run_media_chain("珀莱雅", "2026年3月")
        self.assertFalse(result["ok"])
        self.assertFalse(result["meta"]["document_ready"])
        mock_search.assert_not_called()
        mock_investment.assert_not_called()
        mock_kol.assert_not_called()
        mock_nso.assert_not_called()
        mock_resolve_nso.assert_not_called()

    @patch("bot.chains.media_chain.format_media_report", return_value="# partial report")
    @patch("bot.chains.media_chain.query_ec_nso")
    @patch("bot.chains.media_chain.query_kol_performance")
    @patch("bot.chains.media_chain.query_media_investment")
    @patch("bot.chains.media_chain.query_social_search")
    @patch("bot.chains.media_chain.resolve_source_brand")
    @patch("bot.chains.media_chain.resolve_media_brand")
    def test_missing_one_source_still_queries_available_sources(
        self, mock_resolve, mock_resolve_nso, mock_search, mock_investment,
        mock_kol, mock_nso, mock_format
    ):
        mock_resolve.return_value = {
            "resolved": {
                "search": None,
                "topline": "FLOWER KNOWS",
                "ksi": "FLOWER KNOWS",
                "tmall": "Flower Knows",
                "dy": "花知晓",
            },
            "match_methods": {
                "search": "not_found",
                "topline": "dictionary_exact",
                "ksi": "dictionary_exact",
                "tmall": "dictionary_exact",
                "dy": "dictionary_exact",
            },
            "missing_sources": ["search"],
        }
        mock_resolve_nso.return_value = {
            "brand": "FLOWER KNOWS",
            "match_method": "dictionary_exact",
        }
        mock_search.return_value = {}
        mock_investment.return_value = {}
        mock_kol.return_value = {}
        mock_nso.return_value = {}
        result = run_media_chain(
            "花知晓",
            "2026年2月",
            brand_aliases=["花知晓", "Flower Knows"],
        )
        self.assertTrue(result["ok"])
        mock_search.assert_not_called()
        mock_investment.assert_called_once()
        self.assertEqual(mock_kol.call_count, 2)
        mock_nso.assert_called_once()
        search_result = mock_format.call_args.kwargs["search_result"]
        self.assertEqual(search_result["error"], "source_unavailable")

    @patch("bot.chains.media_chain.format_media_report", return_value="# report")
    @patch("bot.chains.media_chain.query_ec_nso")
    @patch("bot.chains.media_chain.query_kol_performance")
    @patch("bot.chains.media_chain.query_media_investment")
    @patch("bot.chains.media_chain.query_social_search")
    @patch("bot.chains.media_chain.resolve_source_brand")
    @patch("bot.chains.media_chain.resolve_media_brand")
    def test_five_tools_run_in_parallel_with_resolved_values(
        self, mock_resolve, mock_resolve_nso, mock_search, mock_investment,
        mock_kol, mock_nso, _mock_format
    ):
        mock_resolve.return_value = {
            "resolved": {
                "search": "珀莱雅",
                "topline": "PROYA",
                "ksi": "PROYA",
                "tmall": "珀莱雅",
                "dy": "珀莱雅",
            },
            "match_methods": {
                source: "cache"
                for source in ("search", "topline", "ksi", "tmall", "dy")
            },
        }
        mock_resolve_nso.return_value = {
            "brand": "PROYA",
            "match_method": "source_cache",
        }

        def delayed(**_kwargs):
            time.sleep(0.06)
            return {}

        mock_search.side_effect = delayed
        mock_investment.side_effect = delayed
        mock_kol.side_effect = delayed
        mock_nso.side_effect = delayed
        started = time.perf_counter()
        result = run_media_chain("珀莱雅", "2026年3月")
        elapsed = time.perf_counter() - started
        self.assertTrue(result["ok"])
        self.assertLess(elapsed, 0.20)
        self.assertEqual(mock_investment.call_args.kwargs["brand"], "PROYA")
        self.assertEqual(mock_kol.call_args_list[0].kwargs["brand"], "PROYA")
        self.assertEqual(mock_search.call_args.kwargs["brand"], "珀莱雅")
        self.assertEqual(mock_nso.call_args.kwargs["brand"], "PROYA")


class MediaFormatterTest(unittest.TestCase):
    def test_media_table_keeps_all_nine_columns(self):
        headers = [
            "类型", "媒体花费Actual", "花费Evol%", "媒体花费Wgt%",
            "Wgt Change", "NSO Actual", "NSO Evol%", "媒体费比", "费比变化",
        ]
        compact_headers, compact_rows = _compact_wide_table(
            headers,
            [["TTL", "262.3M", "+26.2%", "100.0%", "+0.0pp",
              "705.9M", "+22.6%", "37.1%", "+1.1pp"]],
        )
        self.assertEqual(compact_headers, headers)
        self.assertEqual(len(compact_rows[0]), 9)
        self.assertNotIn("补充信息", compact_headers)

    def test_media_table_uses_visible_tree_hierarchy(self):
        rendered = _render_media(
            {
                "ttl": {"actual_yuan": 100},
                "ait": [
                    {"label": "Awareness", "actual_yuan": 20, "weight": .2},
                    {"label": "Influencer", "actual_yuan": 10, "weight": .1},
                    {"label": "Transaction", "actual_yuan": 70, "weight": .7},
                ],
                "transaction_platforms": [
                    {"label": "TMALL", "actual_yuan": 30, "weight": 3 / 7},
                    {"label": "Douyin", "actual_yuan": 40, "weight": 4 / 7},
                ],
                "mix": {"overall": [], "red": [], "douyin": []},
            },
            {"error": "no_data"},
        )
        table = next(
            item for item in markdown_to_items(rendered)
            if getattr(item, "headers", [None])[0] == "类型"
        )
        self.assertEqual(table.rows[1][0], "├─ Awareness")
        self.assertEqual(table.rows[2][0], "├─ Influencer")
        self.assertEqual(table.rows[3][0], "├─ Transaction")
        self.assertEqual(table.rows[4][0], "│　├─ TMALL")
        self.assertEqual(table.rows[5][0], "│　└─ Douyin")

    def test_social_search_source_uses_requested_name(self):
        rendered = format_media_report(
            "品牌", parse_media_period("2026年3月").to_dict(),
            {"monthly": [], "category_monthly": []},
            {"error": "no_data", "message": "无数据"},
            {"error": "no_data", "message": "无数据"},
            {"error": "no_data"},
            {"error": "no_data"},
            {},
        )
        self.assertIn("数据来源：小红书", rendered)
        self.assertNotIn("小红书灵犀", rendered)

    def test_media_weight_formulas_use_explicit_denominators(self):
        rendered = _render_media(
            {"error": "no_data", "message": "无数据"},
            {"error": "no_data"},
        )
        self.assertIn("AIT Wgt%=对应AIT类型花费/TTL媒体花费", rendered)
        self.assertIn("交易平台Wgt%=对应平台交易花费/Transaction花费", rendered)
        self.assertNotIn("Wgt%=本层花费/父层花费", rendered)

    def test_all_section_source_notes_render_as_gray_text(self):
        rendered = format_media_report(
            "品牌", parse_media_period("2026年3月").to_dict(),
            {"monthly": [], "category_monthly": []},
            {"error": "no_data", "message": "无数据"},
            {"error": "no_data", "message": "无数据"},
            {"error": "no_data"},
            {"error": "no_data"},
            {},
        )
        source_runs = []
        for item in markdown_to_items(rendered):
            if not getattr(item, "text", None):
                continue
            for element in item.text.elements or []:
                run = getattr(element, "text_run", None)
                if run and run.content.startswith("数据来源："):
                    source_runs.append(run)
        self.assertEqual(len(source_runs), 3)
        self.assertTrue(all(run.text_element_style.italic for run in source_runs))
        self.assertTrue(all(run.text_element_style.text_color == 7 for run in source_runs))

    def test_mix_insight_compares_same_level_types(self):
        rows = [
            {
                "label": "T", "weight": 0.60, "weight_change": 0.20,
                "weight_comparison_status": "ok",
                "current_spend_million": 60, "prior_spend_million": 40,
            },
            {
                "label": "F", "weight": 0.25, "weight_change": -0.15,
                "weight_comparison_status": "ok",
                "current_spend_million": 25, "prior_spend_million": 40,
            },
        ]
        insight = _mix_bullets(rows, "整体媒体")
        self.assertIn("交易类投资", insight)
        self.assertIn("信息流投放", insight)
        self.assertIn("相比之下", insight)

    def test_kol_insight_compares_cpe_and_tier_direction(self):
        rows = [
            {
                "name": "T1", "cost": 20, "weight": 0.20,
                "weight_change": -0.10, "weight_comparison_status": "ok",
                "cpe": 5.0,
            },
            {
                "name": "T4", "cost": 45, "weight": 0.45,
                "weight_change": 0.06, "weight_comparison_status": "ok",
                "cpe": 3.0,
            },
            {
                "name": "KOC", "cost": 35, "weight": 0.35,
                "weight_change": 0.04, "weight_comparison_status": "ok",
                "cpe": 2.0,
            },
        ]
        insight = _dimension_bullets(rows, "Tier")
        self.assertIn("相比之下", insight)
        self.assertIn("互动成本更低", insight)
        self.assertIn("向长尾和素人达人转移", insight)

    def test_media_insight_uses_nso_for_ttl_fee_ratio(self):
        investment = {
            "ttl": {
                "actual_yuan": 100_000_000, "prior_yuan": 80_000_000,
                "evol": 0.25, "comparison_status": "ok",
            },
            "ait": [],
            "transaction_platforms": [],
            "mix": {"overall": [], "red": [], "douyin": []},
        }
        rendered = _render_media(
            investment,
            {
                "nso_actual": 400_000_000, "nso_prior": 400_000_000,
                "evol": 0.0, "comparison_status": "ok",
            },
        )
        self.assertIn("媒体费比25.0%", rendered)
        self.assertNotIn("GMV", rendered)

    def test_missing_nso_keeps_spend_report_and_leaves_ratio_blank(self):
        investment = {
            "ttl": {
                "actual_yuan": 10_000_000,
                "prior_yuan": 8_000_000,
                "evol": 0.25,
                "comparison_status": "ok",
            },
            "ait": [],
            "transaction_platforms": [],
            "mix": {"overall": [], "red": [], "douyin": []},
        }
        rendered = _render_media(
            investment,
            {"error": "no_data", "message": "EC Consolidation没有对应品牌"},
        )
        self.assertIn("10.0M", rendered)
        self.assertIn("EC Consolidation没有对应品牌", rendered)
        self.assertIn("NSO与媒体费比留空", rendered)

    def test_top_kol_compares_with_tier_average_cpe(self):
        result = {
            "by_tier": [{
                "name": "T4", "cost": 100, "cost_prior": 90,
                "cost_evol": 0.1111, "comparison_status": "ok",
                "weight": 1, "weight_change": 0,
                "weight_comparison_status": "ok",
                "engage": 10, "engage_prior": 9, "engage_evol": 0.1111,
                "engage_comparison_status": "ok", "cpe": 10,
            }],
            "by_kol_type": [],
            "top_kol": [{
                "rank": 1, "nickname": "达人A", "tier": "T4",
                "kol_type": "Beauty", "cost": 50, "engage": 10, "cpe": 5,
            }],
        }
        rendered = _render_kol_platform(result, "RED", "3.1")
        self.assertIn("比T4层级平均CPE", rendered)
        self.assertIn("低50.0%", rendered)

    def test_report_uses_requested_columns_and_evidence(self):
        period = parse_media_period("2026年3月").to_dict()
        search = {
            "monthly": [
                {"month": "2026-01", "actual": 100, "previous_actual": 80, "evol": 0.25, "mom": None, "rank": 2, "grain": "Brand"},
                {"month": "2026-02", "actual": 120, "previous_actual": 100, "evol": 0.2, "mom": 0.2, "rank": 1, "grain": "Brand"},
                {"month": "2026-03", "actual": None, "previous_actual": None, "evol": None, "mom": None, "rank": None, "grain": "Category only"},
            ],
            "categories": [
                {"month": "2026-03", "category": "护肤", "actual": 70, "previous_actual": 60, "evol": 0.1667, "rank": 3},
            ],
            "coverage": {
                "brand_months": ["2026-01", "2026-02"],
                "category_only_months": ["2026-03"],
                "missing_months": [],
            },
        }
        mix_rows = [
            {"label": label, "weight": 0.2, "weight_prior": 0.2, "weight_change": 0, "current_spend_million": 1, "prior_spend_million": 1}
            for label in ["B", "K", "F", "S", "T"]
        ]
        investment = {
            "ttl": {"actual_yuan": 10_000_000, "prior_yuan": 8_000_000, "evol": 0.25, "comparison_status": "ok"},
            "ait": [
                {"label": "Awareness", "actual_yuan": 2_000_000, "prior_yuan": 1_000_000, "evol": 1.0, "comparison_status": "ok", "weight": 0.2, "weight_change": 0.075, "weight_comparison_status": "ok"},
                {"label": "Influencer", "actual_yuan": 3_000_000, "prior_yuan": 2_000_000, "evol": 0.5, "comparison_status": "ok", "weight": 0.3, "weight_change": 0.05, "weight_comparison_status": "ok"},
                {"label": "Transaction", "actual_yuan": 5_000_000, "prior_yuan": 5_000_000, "evol": 0.0, "comparison_status": "ok", "weight": 0.5, "weight_change": -0.125, "weight_comparison_status": "ok"},
            ],
            "transaction_platforms": [
                {"label": "TMALL", "actual_yuan": 3_000_000, "prior_yuan": 2_000_000, "evol": 0.5, "comparison_status": "ok", "weight": 0.6, "weight_change": 0.2, "weight_comparison_status": "ok"},
                {"label": "Douyin", "actual_yuan": 2_000_000, "prior_yuan": 3_000_000, "evol": -0.3333, "comparison_status": "ok", "weight": 0.4, "weight_change": -0.2, "weight_comparison_status": "ok"},
                {"label": "JD", "actual_yuan": 0, "prior_yuan": 0, "evol": None, "comparison_status": "base_zero", "weight": 0.0, "weight_change": 0.0, "weight_comparison_status": "ok"},
            ],
            "mix": {"overall": mix_rows, "red": mix_rows[:4], "douyin": mix_rows},
        }
        nso = {
            "nso_actual": 100_000_000,
            "nso_prior": 160_000_000,
            "evol": -0.375,
            "comparison_status": "ok",
        }
        dimension = [{
            "name": "A",
            "cost": 1000,
            "cost_prior": 800,
            "cost_evol": 0.25,
            "weight": 1,
            "weight_prior": 1,
            "weight_change": 0,
            "engage": 100,
            "engage_prior": 80,
            "engage_evol": 0.25,
            "cpe": 10,
        }]
        kol = {
            "by_tier": dimension,
            "by_kol_type": dimension,
            "top_kol": [{"rank": 1, "nickname": "KOL 1", "tier": "A", "kol_type": "美妆", "cost": 1000, "engage": 100, "cpe": 10}],
        }
        report = format_media_report(
            "Brand A", period, search, investment, nso, kol, kol,
            {"search": "Brand A", "topline": "Brand A", "ksi": "Brand A", "nso": "Brand A"},
        )
        self.assertIn("Category only", report)
        self.assertIn("| 类型", report)
        self.assertIn("费比变化", report)
        self.assertIn("+5.0pp", report)
        self.assertNotIn("费比Evol%", report)
        self.assertIn("10.0M", report)
        self.assertIn("1.0K", report)
        self.assertNotIn("不能单独证明", report)
        self.assertNotIn("不代表生意结果", report)
        self.assertNotIn("不等同于销售转化效率", report)
        self.assertIn("| Tier", report)
        self.assertIn("CPE", report)
        self.assertNotIn("2025 Actual", report)
        self.assertTrue(report.startswith("# 1. Media Investment"))
        self.assertNotIn("# Brand A 2026年3月 BET媒体投资分析报告", report)
        self.assertIn("## 1.1 Overall", report)
        self.assertIn("### 1.1.1 TTL、AIT与交易平台", report)
        self.assertIn("# 2. KOL Performance", report)
        self.assertIn("## 2.1 RED", report)
        self.assertIn("### 2.1.1 By Tier", report)
        self.assertIn("# 3. Social Search", report)
        self.assertIn("## 3.1 Category粒度明细（不加总为Brand）", report)
        self.assertIn("EC Consolidation", report)
        self.assertGreaterEqual(report.count("> _数据来源："), 3)
        self.assertLess(
            report.index("# 1. Media Investment"),
            report.index("# 2. KOL Performance"),
        )
        self.assertLess(
            report.index("# 2. KOL Performance"),
            report.index("# 3. Social Search"),
        )


if __name__ == "__main__":
    unittest.main()
