from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from bot.market_formatter import format_market_result
from bot.market_plan import build_market_plan
from bot.router import route
from bot.session import SessionState
from bot.tools.query_market_top_brands import query_market_top_brands
from bot.tools.query_market_brand_deep_dive import (
    _platform_and_month_analysis,
    _select_representative_brands,
)
from bot.tools.query_market_trend import query_market_trend
from bot.utils import parse_ec_period


class MarketPlanTest(unittest.TestCase):
    def test_q2_mass_top3_means_scale_top3_and_deep_dive(self):
        plan = build_market_plan("分析2026年q2 mass beauty的top3品牌是哪些，是如何成为top3的")
        self.assertEqual(plan.intent, "market_brand_deep_dive")
        self.assertEqual(plan.period.lower(), "2026年q2")
        self.assertEqual(plan.segment, "PURE MASS")
        self.assertEqual(plan.platform, "TTL")
        self.assertEqual(plan.ranking_metric, "gmv_actual")
        self.assertEqual(plan.ranking_limit, 3)

    def test_multidimensional_top_brand_request_routes_to_deep_dive(self):
        plan = build_market_plan(
            "分析2026年1-6月mass top品牌的表现，跨三平台分析，"
            "看平台、选品、生意节奏、价格和促销及值得学习的点"
        )
        self.assertEqual(plan.intent, "market_brand_deep_dive")
        self.assertEqual(plan.platform, "TTL")
        self.assertEqual(plan.period, "2026年1-6月")

    @patch("bot.router.classify_user_intent", return_value=None)
    def test_exact_multidimensional_request_routes_end_to_end(self, _mock):
        result = route(
            "分析一下今年2026年1-6月，mass top品牌的表现，需要跨三平台分析，"
            "最终告诉我这些top品牌在平台、选品、生意节奏、价格和促销上分别有哪些值得我学习的点",
            SessionState(),
        )
        self.assertEqual(result.type, "market_brand_deep_dive")
        self.assertEqual(result.platform, "TTL")

    def test_cross_month_date_range_is_not_truncated_to_first_day(self):
        plan = build_market_plan("2026年6月1日到7月10日生意最好的牌子")
        self.assertEqual(plan.period, "2026年6月1日到7月10日")
        self.assertEqual(plan.intent, "market_brand_ranking")

    def test_single_day_period(self):
        parsed = parse_ec_period("2026年7月10日", 2026)
        self.assertEqual(parsed["current_start"], "2026-07-10")
        self.assertEqual(parsed["current_end"], "2026-07-10")
        self.assertEqual(parsed["prior_start"], "2025-07-10")

    def test_growth_words_override_scale_ranking_semantics(self):
        plan = build_market_plan("2026年1-6月大盘里涨得最好的品牌")
        self.assertEqual(plan.segment, "PURE MASS")
        self.assertEqual(plan.platform, "TM")
        self.assertEqual(plan.ranking_metric, "gmv_growth")
        self.assertEqual(plan.intent, "market_brand_ranking")

    def test_market_trend_still_defaults_to_three_platforms(self):
        plan = build_market_plan("2026年1-6月大盘怎么样")
        self.assertEqual(plan.platform, "TTL")

    def test_explicit_three_platform_top5_overrides_tmall_default(self):
        plan = build_market_plan("2026年6月三平台Top 5品牌")
        self.assertEqual(plan.intent, "market_brand_ranking")
        self.assertEqual(plan.platform, "TTL")

    def test_explicit_segment_platform(self):
        plan = build_market_plan("2026年6月天猫Selective大盘")
        self.assertEqual(plan.segment, "SELECTIVE")
        self.assertEqual(plan.platform, "TM")

    def test_total_beauty_market_maps_to_beauty_market_segment(self):
        plan = build_market_plan("2026年1-6月 Total Beauty Market大盘")
        self.assertEqual(plan.segment, "BEAUTY MARKET")
        self.assertEqual(plan.platform, "TTL")

    def test_lowercase_total_beauty_market_is_recognized(self):
        plan = build_market_plan("2026年1-6月 total beauty market趋势")
        self.assertEqual(plan.segment, "BEAUTY MARKET")

    def test_tmall_top5_is_deterministic_market_ranking(self):
        plan = build_market_plan("天猫top5")
        self.assertEqual(plan.intent, "market_brand_ranking")
        self.assertEqual(plan.platform, "TM")

    def test_plain_top3_defaults_to_three_platform_scale(self):
        plan = build_market_plan("2026年Q2 Mass Beauty Top3品牌")
        self.assertEqual(plan.intent, "market_brand_ranking")
        self.assertEqual(plan.platform, "TTL")
        self.assertEqual(plan.ranking_metric, "gmv_actual")
        self.assertEqual(plan.ranking_limit, 3)


class MarketRouterTest(unittest.TestCase):
    @patch("bot.router.classify_user_intent", return_value=None)
    def test_q2_mass_top3_routes_to_market_not_followup(self, _mock):
        result = route("分析2026年q2 mass beauty的top3品牌是哪些，是如何成为top3的", SessionState())
        self.assertEqual(result.type, "clarify_analysis_scope")
        self.assertEqual(result.preflight_target, "market_brand_deep_dive")
        self.assertEqual(result.platform, "TTL")
        self.assertEqual(result.ranking_metric, "gmv_actual")
        self.assertEqual(result.ranking_limit, 3)
        self.assertIn("不能直接回答", result.message)
        self.assertIn("不能把相关性写成因果", result.message)

    def test_market_preflight_confirmation_can_change_ranking_metric(self):
        state = SessionState(pending_request={
            "intent": "analysis_preflight",
            "target": "market_brand_deep_dive",
            "period": "2026年Q2",
            "segment": "PURE MASS",
            "platform": "TTL",
            "market_view": "top_brands",
            "ranking_metric": "gmv_actual",
            "ranking_limit": 3,
        })
        result = route("改成按增长额排名，确认", state)
        self.assertEqual(result.type, "market_brand_deep_dive")
        self.assertEqual(result.ranking_metric, "gmv_growth")
        self.assertEqual(result.ranking_limit, 3)

    @patch("bot.router.classify_user_intent", return_value=None)
    def test_single_day_market_keeps_exact_date(self, _mock):
        result = route("2026年7月10日的大盘如何", SessionState())
        self.assertEqual(result.type, "market_analysis")
        self.assertEqual(result.period, "2026年7月10日")

    @patch("bot.router.classify_user_intent", return_value=None)
    def test_market_beats_ec_brand_route(self, _mock):
        result = route("2026年1-6月大盘怎么样", SessionState())
        self.assertEqual(result.type, "market_analysis")
        self.assertEqual(result.period, "2026年1-6月")

    @patch("bot.router.classify_user_intent", return_value=None)
    def test_missing_period_clarifies(self, _mock):
        result = route("大盘怎么样", SessionState())
        self.assertEqual(result.type, "clarify_market_period")

    @patch("bot.router.classify_user_intent", return_value=None)
    def test_ranking_inherits_market_context(self, _mock):
        state = SessionState()
        state.market_context.period = "2026年1-6月"
        state.market_context.segment = "SELECTIVE"
        result = route("那大盘里面涨得最好的品牌是什么", state)
        self.assertEqual(result.type, "market_brand_ranking")
        self.assertEqual(result.period, "2026年1-6月")
        self.assertEqual(result.segment, "SELECTIVE")

    @patch("bot.router.classify_user_intent", return_value=None)
    def test_tmall_top5_inherits_failed_market_request_period(self, _mock):
        state = SessionState()
        state.market_context.period = "2026年7月1日到7月10日"
        result = route("天猫top5", state)
        self.assertEqual(result.type, "market_brand_ranking")
        self.assertEqual(result.period, "2026年7月1日到7月10日")
        self.assertEqual(result.platform, "TM")

    @patch("bot.router.classify_user_intent", return_value=None)
    def test_best_brands_default_to_tmall_pure_mass(self, _mock):
        result = route("2026年7月1日到7月10日生意最好的牌子", SessionState())
        self.assertEqual(result.type, "market_brand_ranking")
        self.assertEqual(result.segment, "PURE MASS")
        self.assertEqual(result.platform, "TM")

    @patch("bot.router.classify_user_intent", return_value=None)
    def test_ordinal_jumps_to_ec(self, _mock):
        state = SessionState()
        state.market_context.period = "2026年6月"
        state.market_context.top_brands = ["KANS", "PROYA"]
        result = route("第2名的生意怎么样", state)
        self.assertEqual((result.type, result.brand, result.period), ("default_chain", "PROYA", "2026年6月"))

    @patch("bot.router.classify_user_intent", return_value=None)
    def test_ordinal_jumps_to_bet(self, _mock):
        state = SessionState()
        state.market_context.period = "2026年6月"
        state.market_context.top_brands = ["KANS"]
        result = route("第1名BET如何", state)
        self.assertEqual((result.type, result.brand), ("media_analysis", "KANS"))


class MarketTrendToolTest(unittest.TestCase):
    def test_tool_rejects_non_whitelisted_scope(self):
        result = query_market_trend("2026年6月", segment="UNKNOWN", platform="TTL")
        self.assertEqual(result["error"], "execution_error")
        self.assertIn("Segment", result["message"])

    @patch("bot.tools.query_market_trend.fetch_df")
    def test_beauty_market_uses_global_segment_not_total_beauty_category(self, mock_fetch):
        rows = pd.DataFrame([
            {"period_key": period, "source_month": month, "platform": platform,
             "row_count": 1, "gmv": value}
            for period, month, value in (
                ("current", "2026-06", 100), ("prior", "2025-06", 80)
            )
            for platform in ("TM", "DY", "JD")
        ])
        mock_fetch.side_effect = [rows, rows]
        result = query_market_trend("2026年6月", segment="BEAUTY MARKET")
        monthly_sql = mock_fetch.call_args_list[0].args[0]
        daily_sql = mock_fetch.call_args_list[1].args[0]
        self.assertNotIn("category_EN = 'Total Beauty'", monthly_sql)
        self.assertNotIn("category_EN = 'Total Beauty'", daily_sql)
        self.assertEqual(mock_fetch.call_args_list[0].args[1]["segment"], "BEAUTY MARKET")
        self.assertEqual(result["query_meta"]["category"], None)
        self.assertEqual(result["query_meta"]["monthly_scope_rule"], "global_segment=Beauty Market")

    @patch("bot.tools.query_market_trend.fetch_df")
    def test_full_month_uses_monthly_without_daily_double_count(self, mock_fetch):
        monthly = pd.DataFrame([
            {"period_key": period, "source_month": month, "platform": p, "row_count": 1, "gmv": value}
            for period, month, base in (("current", "2026-06", 100), ("prior", "2025-06", 80))
            for p, value in (("TM", base), ("DY", base * 2), ("JD", base / 2))
        ])
        daily = pd.DataFrame([
            {"period_key": "current", "source_month": "2026-06", "platform": "TM", "row_count": 30, "gmv": 9999}
        ])
        mock_fetch.side_effect = [monthly, daily]
        result = query_market_trend("2026年6月")
        ttl = result["rows"][0]
        self.assertEqual(ttl["gmv_actual"], 350)
        self.assertTrue(all(row["source"] == "monthly" for row in result["coverage"]))
        self.assertIn("LPAD(DAY(bus_date)", mock_fetch.call_args_list[0].args[0])

    @patch("bot.tools.query_market_trend.fetch_df")
    def test_partial_month_uses_daily(self, mock_fetch):
        mock_fetch.return_value = pd.DataFrame([
            {"period_key": period, "source_month": month, "platform": p, "row_count": 10, "gmv": value}
            for period, month, base in (("current", "2026-07", 10), ("prior", "2025-07", 8))
            for p, value in (("TM", base), ("DY", base * 2), ("JD", base / 2))
        ])
        result = query_market_trend("2026年7月1日到7月10日")
        self.assertFalse(result.get("error"))
        self.assertTrue(all(row["source"] == "daily" for row in result["coverage"]))
        self.assertEqual(mock_fetch.call_count, 1)
        daily_sql = mock_fetch.call_args.args[0]
        self.assertIn("three_platforms_segmented_markets_daily", daily_sql)
        self.assertNotIn("category_EN =", daily_sql)

    @patch("bot.tools.query_market_trend.fetch_df")
    def test_full_month_and_partial_month_are_blended_once(self, mock_fetch):
        monthly = pd.DataFrame([
            {"period_key": period, "source_month": month, "platform": p, "row_count": 1, "gmv": 100}
            for period, month in (("current", "2026-06"), ("prior", "2025-06")) for p in ("TM", "DY", "JD")
        ])
        daily = pd.DataFrame([
            {"period_key": period, "source_month": month, "platform": p, "row_count": 10, "gmv": 10}
            for period, month in (("current", "2026-07"), ("prior", "2025-07")) for p in ("TM", "DY", "JD")
        ])
        mock_fetch.side_effect = [monthly, daily]
        result = query_market_trend("2026年6月1日到7月10日")
        self.assertEqual(result["rows"][0]["gmv_actual"], 330)
        sources = {(r["month"], r["source"]) for r in result["coverage"] if r["period_key"] == "current"}
        self.assertEqual(sources, {("2026-06", "monthly"), ("2026-07", "daily")})


class MarketRankingToolTest(unittest.TestCase):
    def test_representative_selection_covers_scale_growth_and_speed(self):
        selected = _select_representative_brands([
            {"brand": "SCALE", "gmv_actual": 1000, "gmv_prior": 950, "gmv_growth": 50, "evol": .0526},
            {"brand": "GROWTH", "gmv_actual": 800, "gmv_prior": 500, "gmv_growth": 300, "evol": .6},
            {"brand": "SPEED", "gmv_actual": 120, "gmv_prior": 40, "gmv_growth": 80, "evol": 2.0},
        ])
        self.assertEqual([row["brand"] for row in selected], ["SCALE", "GROWTH", "SPEED"])

    def test_platform_breakdown_returns_mix_growth_and_monthly_rhythm(self):
        coverage = []
        for period, month, values in (
            ("current", "2026-05", {"TM": 50, "DY": 40, "JD": 10}),
            ("current", "2026-06", {"TM": 80, "DY": 100, "JD": 20}),
            ("prior", "2025-05", {"TM": 40, "DY": 20, "JD": 10}),
            ("prior", "2025-06", {"TM": 60, "DY": 30, "JD": 10}),
        ):
            for platform, gmv in values.items():
                coverage.append({"brand": "A", "period_key": period, "month": month, "platform": platform, "gmv": gmv})
        result = _platform_and_month_analysis(coverage, ["A"])["A"]
        by_platform = {row["platform"]: row for row in result["platforms"]}
        self.assertAlmostEqual(by_platform["DY"]["weight"], 140 / 300, places=4)
        self.assertEqual(by_platform["DY"]["gmv_growth"], 90)
        self.assertEqual(result["peak_month"]["month"], "2026-06")
        self.assertEqual(result["growth_month"]["month"], "2026-06")
        self.assertEqual(result["months"][1]["mom"], 1.0)
        self.assertIn("618观察窗口", result["months"][1]["event_context"])

    def test_ttl_partial_period_is_rejected_without_query(self):
        with patch("bot.tools.query_market_top_brands.fetch_df") as mock_fetch:
            result = query_market_top_brands("2026年7月1日到7月10日", platform="TTL")
        self.assertEqual(result["error"], "unsupported_partial_platform_coverage")
        mock_fetch.assert_not_called()

    @patch("bot.tools.query_market_top_brands.fetch_df")
    def test_growth_ranking_is_comparable_top_five(self, mock_fetch):
        rows = []
        for period, month, multiplier in (("current", "2026-06", 1.0), ("prior", "2025-06", 0.5)):
            for brand, base in (("A", 100), ("B", 80), ("C", 50)):
                for platform in ("TM", "DY", "JD"):
                    rows.append({"period_key": period, "source_month": month, "platform": platform,
                                 "brand_name": brand, "row_count": 1, "gmv": base * multiplier})
        mock_fetch.return_value = pd.DataFrame(rows)
        result = query_market_top_brands("2026年6月", platform="TTL")
        self.assertEqual(result["rows"][0]["brand"], "A")
        self.assertEqual(result["rows"][0]["gmv_growth"], 150)
        self.assertEqual(mock_fetch.call_count, 1)
        self.assertNotIn("tmall_store_ranking_day_jiashicang", mock_fetch.call_args.args[0])

    @patch("bot.tools.query_market_top_brands.fetch_df")
    def test_complete_month_range_never_scans_tmall_daily_table(self, mock_fetch):
        rows = pd.DataFrame([
            {"period_key": period, "source_month": month, "platform": "TM",
             "brand_name": "A", "row_count": 1, "gmv": value}
            for period, month, value in (
                ("current", "2026-01", 100), ("current", "2026-02", 100),
                ("current", "2026-03", 100), ("current", "2026-04", 100),
                ("current", "2026-05", 100), ("current", "2026-06", 100),
                ("prior", "2025-01", 80), ("prior", "2025-02", 80),
                ("prior", "2025-03", 80), ("prior", "2025-04", 80),
                ("prior", "2025-05", 80), ("prior", "2025-06", 80),
            )
        ])
        mock_fetch.return_value = rows
        result = query_market_top_brands(
            "2026年1-6月", platform="TM", ranking_metric="gmv_growth",
        )
        self.assertFalse(result.get("error"))
        self.assertEqual(mock_fetch.call_count, 1)
        sql, params = mock_fetch.call_args.args
        self.assertNotIn("tmall_store_ranking_day_jiashicang", sql)
        self.assertIn("bus_date BETWEEN :current_month_start", sql)
        self.assertEqual(params["current_month_start"], "2026-01-01")
        self.assertEqual(params["current_month_end"], "2026-01-06")

    @patch("bot.tools.query_market_top_brands.fetch_df")
    def test_scale_ranking_deduplicates_monthly_business_rows(self, mock_fetch):
        mock_fetch.return_value = pd.DataFrame([
            {"period_key": "current", "source_month": "2026-04", "platform": platform,
             "brand_name": brand, "row_count": 1, "gmv": gmv}
            for brand, gmv in (("GUYU", 351), ("FAN BEAUTY", 320), ("PECHOIN", 303), ("OFFRELAX", 273))
            for platform in ("TM", "DY", "JD")
        ] + [
            {"period_key": "prior", "source_month": "2025-04", "platform": platform,
             "brand_name": brand, "row_count": 1, "gmv": gmv * .8}
            for brand, gmv in (("GUYU", 351), ("FAN BEAUTY", 320), ("PECHOIN", 303), ("OFFRELAX", 273))
            for platform in ("TM", "DY", "JD")
        ])
        result = query_market_top_brands("2026年4月", platform="TTL", ranking_metric="gmv_actual", limit=3)
        self.assertEqual([row["brand"] for row in result["rows"]], ["GUYU", "FAN BEAUTY", "PECHOIN"])
        sql = mock_fetch.call_args.args[0]
        self.assertIn("MAX(CAST(REPLACE(NULLIF(TRIM(gmv)", sql)
        self.assertIn("GROUP BY bus_date, category_CN", sql)

    @patch("bot.tools.query_market_top_brands.fetch_df")
    def test_pure_mass_brand_ranking_filters_null_selectivity(self, mock_fetch):
        mock_fetch.side_effect = [pd.DataFrame(), pd.DataFrame()]
        query_market_top_brands("2026年7月1日到7月10日", platform="TM")
        monthly_sql = mock_fetch.call_args_list[0].args[0]
        daily_sql = mock_fetch.call_args_list[1].args[0]
        self.assertIn("SELECTIVITY IS NULL", monthly_sql)
        self.assertIn("LPAD(DAY(bus_date)", monthly_sql)
        self.assertIn("d.bus_date BETWEEN :daily_0_start", daily_sql)
        self.assertNotIn("CAST(d.bus_date AS DATE)", daily_sql)
        self.assertIn("d.SELECTIVITY IS NULL", daily_sql)
        self.assertNotIn("segment_brand", daily_sql)

    @patch("bot.tools.query_market_top_brands.fetch_df")
    def test_explicit_selective_daily_can_use_monthly_segment_membership(self, mock_fetch):
        mock_fetch.side_effect = [pd.DataFrame(), pd.DataFrame()]
        query_market_top_brands(
            "2026年7月1日到7月10日",
            segment="SELECTIVE",
            platform="TM",
        )
        daily_sql = mock_fetch.call_args_list[1].args[0]
        self.assertIn("LEFT JOIN", daily_sql)
        self.assertIn("segment_brand", daily_sql)
        self.assertIn("d.SELECTIVITY IS NULL", daily_sql)


class MarketFormatterTest(unittest.TestCase):
    def test_deep_dive_report_contains_required_evidence_and_price_boundary(self):
        result = format_market_result({
            "query_meta": {"tool": "query_market_brand_deep_dive", "segment": "PURE MASS",
                "platform": "TTL", "category": "Total Beauty",
                "current_period": ["2026-01-01", "2026-06-30"], "prior_period": ["2025-01-01", "2025-06-30"]},
            "brands": [{"brand": "A", "selection_reasons": ["规模领先"],
                "gmv_actual": 1000, "gmv_prior": 600, "gmv_growth": 400, "evol": 2 / 3,
                "platforms": [{"platform": "DY", "gmv_actual": 700, "gmv_prior": 300,
                    "gmv_growth": 400, "evol": 4 / 3, "weight": .7, "growth_contribution": 1.0}],
                "months": [{"month": "2026-06", "gmv_actual": 500, "gmv_prior": 200,
                    "gmv_growth": 300, "evol": 1.5, "leading_platform": "DY"}],
                "product_evidence": [{"platform": "DY", "status": "ok", "products": [{
                    "product_name": "爆品A", "gmv_actual": 300, "gmv_prior": 100, "gmv_growth": 200,
                    "evol": 2.0, "paid_unit_value_proxy": None, "proxy_month_min": None, "proxy_month_max": None,
                }]}, {"platform": "JD", "status": "no_product_table", "products": []}] }],
            "event_context": ["618"], "coverage": [],
            "limitations": ["GMV/销量只是成交均价代理，不是商品标价。"],
        })
        markdown = result["markdown"]
        for phrase in ("选牌逻辑", "平台结构与增长来源", "生意节奏", "选品与价格证据", "值得学习的点"):
            self.assertIn(phrase, markdown)
        self.assertIn("当前无商品级数据表", markdown)
        self.assertIn("不是商品标价", markdown)
        self.assertTrue(result["meta"]["document_ready"])

    def test_default_scope_is_explicit(self):
        result = format_market_result({
            "query_meta": {"tool": "query_market_trend", "segment": "PURE MASS", "category": "Total Beauty", "platform": "TTL",
                           "current_period": ["2026-06-01", "2026-06-30"], "prior_period": ["2025-06-01", "2025-06-30"]},
            "rows": [{"platform": "TTL", "gmv_actual": 100, "gmv_prior": 80, "evol": .25, "gmv_growth": 20,
                      "wgt": 1.0, "wgt_change": 0.0, "comparison_status": "ok"}],
            "coverage": [], "missing": [],
        })
        self.assertIn("Pure Mass三平台TTL Total Beauty大盘", result["markdown"])
        self.assertFalse(result["meta"]["document_ready"])

    def test_beauty_market_formatter_does_not_call_it_a_category(self):
        result = format_market_result({
            "query_meta": {"tool": "query_market_trend", "segment": "BEAUTY MARKET", "category": None, "platform": "TTL",
                           "current_period": ["2026-06-01", "2026-06-30"], "prior_period": ["2025-06-01", "2025-06-30"]},
            "rows": [{"platform": "TTL", "gmv_actual": 100, "gmv_prior": 80, "evol": .25, "gmv_growth": 20,
                      "wgt": 1.0, "wgt_change": 0.0, "comparison_status": "ok"}],
            "coverage": [], "missing": [],
        })
        self.assertIn("Total Beauty Market三平台TTL大盘", result["markdown"])
        self.assertNotIn("Total Beauty Market三平台TTL Total Beauty", result["markdown"])


if __name__ == "__main__":
    unittest.main()
