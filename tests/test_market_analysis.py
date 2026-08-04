from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from bot.market_formatter import format_market_result
from bot.market_plan import build_market_plan
from bot.router import route
from bot.session import SessionState
from bot.tools.query_market_top_brands import query_market_top_brands
from bot.tools.query_market_trend import query_market_trend
from bot.utils import parse_ec_period


class MarketPlanTest(unittest.TestCase):
    def test_cross_month_date_range_is_not_truncated_to_first_day(self):
        plan = build_market_plan("2026年6月1日到7月10日生意最好的牌子")
        self.assertEqual(plan.period, "2026年6月1日到7月10日")
        self.assertEqual(plan.intent, "market_brand_ranking")

    def test_single_day_period(self):
        parsed = parse_ec_period("2026年7月10日", 2026)
        self.assertEqual(parsed["current_start"], "2026-07-10")
        self.assertEqual(parsed["current_end"], "2026-07-10")
        self.assertEqual(parsed["prior_start"], "2025-07-10")

    def test_defaults_and_ranking_semantics(self):
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

    def test_tmall_top5_is_deterministic_market_ranking(self):
        plan = build_market_plan("天猫top5")
        self.assertEqual(plan.intent, "market_brand_ranking")
        self.assertEqual(plan.platform, "TM")


class MarketRouterTest(unittest.TestCase):
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
        self.assertIn("LPAD(DAY(bus_date), 2, '0')", mock_fetch.call_args_list[0].args[0])

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

    @patch("bot.tools.query_market_top_brands.fetch_df")
    def test_pure_mass_brand_ranking_filters_null_selectivity(self, mock_fetch):
        mock_fetch.side_effect = [pd.DataFrame(), pd.DataFrame()]
        query_market_top_brands("2026年7月1日到7月10日", platform="TM")
        monthly_sql = mock_fetch.call_args_list[0].args[0]
        daily_sql = mock_fetch.call_args_list[1].args[0]
        self.assertIn("SELECTIVITY IS NULL", monthly_sql)
        self.assertIn("LPAD(DAY(bus_date), 2, '0')", monthly_sql)
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


if __name__ == "__main__":
    unittest.main()
