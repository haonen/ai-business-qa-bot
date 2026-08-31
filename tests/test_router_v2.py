from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from bot.router import route
from bot.session import SessionState
from bot.entity_resolution import ResolvedPeriod
from bot.chains.brand_business_investment_chain import run_brand_business_investment_chain


class RouterV2GoldenSetTests(unittest.TestCase):
    def route(self, text: str, state: SessionState | None = None):
        with patch.dict(os.environ, {
            "ROUTER_V2_ENABLED": "1", "ENTITY_RESOLVER_V2_ENABLED": "0",
        }, clear=False):
            return route(text, state or SessionState())

    def assert_intents(self, result, expected):
        self.assertEqual((result.route_decision or {}).get("intents"), expected)

    def test_douyin_business_is_commerce(self):
        result = self.route("美宝莲2026年Q1抖音生意怎么样")
        self.assertEqual(result.type, "douyin_business_analysis")
        self.assertEqual(result.platform, "DY")
        self.assert_intents(result, ["EC_BUSINESS"])

    def test_plain_bet_is_overall(self):
        result = self.route("美宝莲2026年Q1 BET怎么样")
        self.assertEqual(result.type, "media_analysis")
        self.assertEqual(result.media_mode, "OVERALL_BET")
        self.assert_intents(result, ["BET"])

    def test_douyin_bet_requires_media_scope(self):
        result = self.route("美宝莲2026年Q1抖音BET怎么样")
        self.assertEqual(result.type, "clarify_media_scope")
        self.assertIn("media_scope.mode", result.route_decision["missing_slots"])

    def test_douyin_investment_requires_media_scope(self):
        result = self.route("美宝莲2026年Q1抖音投放怎么样")
        self.assertEqual(result.type, "clarify_media_scope")

    def test_douyin_business_and_bet_is_composite_ambiguity(self):
        result = self.route("美宝莲2026年Q1抖音生意和BET怎么样")
        self.assertEqual(result.type, "clarify_media_scope")
        self.assertEqual(result.platform, "DY")
        self.assert_intents(result, ["EC_BUSINESS", "BET"])

    def test_explicit_douyin_business_and_douyin_investment_has_channel_plan(self):
        result = self.route("美宝莲2026年Q1抖音生意和抖音投放")
        self.assertEqual(result.type, "clarify_analysis_scope")
        self.assertEqual(result.media_mode, "CHANNEL_ONLY")
        self.assertEqual(result.media_channels, ["DOUYIN"])

    def test_tmall_business_and_bet_is_composite_confirmation(self):
        result = self.route("PROYA 2026年Q1天猫生意和BET")
        self.assertEqual(result.type, "clarify_analysis_scope")
        self.assertEqual(result.brand, "PROYA")
        self.assertEqual(result.platform, "TM")
        self.assertEqual(result.media_mode, "OVERALL_BET")

    def test_non_kol_is_tmall_commerce_not_bet(self):
        result = self.route("谷雨2026年5月Non-KOL表现")
        self.assertEqual(result.type, "default_chain")
        self.assertEqual(result.platform, "TM")
        self.assert_intents(result, ["EC_BUSINESS"])

    def test_tmall_key_drivers_infer_tmall_platform(self):
        result = self.route("谷雨2026年5月李佳琦、T2和Non-KOL的生意分别有多少")
        self.assertEqual(result.type, "default_chain")
        self.assertEqual(result.platform, "TM")
        self.assert_intents(result, ["EC_BUSINESS"])

    def test_douyin_key_drivers_infer_douyin_platform(self):
        result = self.route("谷雨2026年5月KOL直播、品牌自营直播和短视频的生意分别有多少")
        self.assertEqual(result.type, "douyin_business_analysis")
        self.assertEqual(result.platform, "DY")
        self.assert_intents(result, ["EC_BUSINESS"])

    def test_non_kol_with_explicit_bet_keeps_both_intents(self):
        result = self.route("谷雨2026年5月Non-KOL生意和BET")
        self.assertEqual(result.type, "clarify_analysis_scope")
        self.assertEqual(result.platform, "TM")
        self.assert_intents(result, ["EC_BUSINESS", "BET"])

    def test_brand_pollution_regression(self):
        result = self.route("分析PROYA 2026年Q1天猫生意和BET投资")
        self.assertEqual(result.brand, "PROYA")
        self.assertNotIn("年Q1天猫", result.brand)

    def test_quantity_phrasing_and_single_date_do_not_pollute_brand(self):
        # router_v2.extract_brand_surface's date-strip alternatives used to be
        # ordered bare-month-first, so "8月20日" only lost "8月" and left
        # "20日" glued to the brand; its cut-marker list also had no entry for
        # "是多少", so brands with no earlier marker kept the whole tail.
        result = self.route("分析PROYA 2026年8月20日天猫生意是多少")
        self.assertEqual(result.brand, "PROYA")
        self.assertEqual(result.period, "2026年8月20日")

    def test_full_day_range_with_month_on_both_sides_does_not_pollute_brand(self):
        result = self.route("分析珀莱雅5月1日至5月20日天猫的生意是多少")
        self.assertEqual(result.brand, "珀莱雅")
        self.assertEqual(result.period, "5月1日至5月20日")

    def test_stacked_polite_prefixes_do_not_pollute_brand(self):
        # Real historical traffic (via bot_questions.csv replay) showed multi-
        # token prefixes ("你现在能帮我算一下", "你好 帮我看一下") surviving into
        # the brand because the old prefix regex only allowed a single,
        # contiguous match instead of repeating over whitespace-separated
        # filler tokens.
        for raw, expected_brand in (
            ("你帮我算一下珀莱雅在2026年5月13日-5月31日期间的生意", "珀莱雅"),
            ("你好 帮我看一下谷雨1-6月的BET", "谷雨"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self.route(raw).brand, expected_brand)

    def test_demonstrative_back_reference_does_not_pollute_brand(self):
        result = self.route("再算一下珀莱雅在这个期间的gmv")
        self.assertEqual(result.brand, "珀莱雅")

    def test_generic_brand_business_does_not_default_platform(self):
        result = self.route("谷雨618怎么样")
        self.assertEqual(result.type, "clarify_ec_platform")
        self.assertIsNone(result.platform)

    def test_naked_market_asks_all_missing_scopes(self):
        result = self.route("大盘怎么样")
        self.assertEqual(result.type, "clarify_market_scope")
        self.assertEqual(
            set(result.route_decision["missing_slots"]),
            {"period", "commerce_scope.platforms", "market_scope.segment", "market_scope.category"},
        )

    def test_market_complete_total_beauty_routes(self):
        result = self.route("2026年Q1天猫Pure Mass TTL Beauty大盘Top 3品牌")
        self.assertEqual(result.type, "market_brand_ranking")
        self.assertEqual(result.ranking_metric, "gmv_actual")
        self.assertEqual(result.ranking_limit, 3)

    def test_ranking_vocabulary(self):
        self.assertEqual(
            self.route("2026年Q1天猫Pure Mass TTL Beauty哪些品牌涨得最好").ranking_metric,
            "gmv_growth",
        )
        self.assertEqual(
            self.route("2026年Q1天猫Pure Mass TTL Beauty哪些品牌增速最高").ranking_metric,
            "evol",
        )

    def test_strategy_is_observational_only(self):
        result = self.route("花西子2026年3月BET增长为什么，公司做了什么策略？")
        self.assertEqual(result.question_mode, "strategy")
        self.assertIn("causal_attribution", result.route_decision["unsupported_requests"])

    def test_complete_new_request_overrides_stale_pending(self):
        state = SessionState()
        state.pending_request = {"intent": "business_platform_selection", "brand": "旧品牌", "period": "2026年1月"}
        result = self.route("美宝莲2026年Q1抖音生意怎么样", state)
        self.assertEqual((result.brand, result.platform), ("美宝莲", "DY"))

    def test_rich_platform_reply_preserves_pending_entities_and_goal(self):
        state = SessionState(pending_request={
            "intent": "business_platform_selection", "brand": "dirovo",
            "period": "2026年4-6月", "brand_aliases": ["蒂洛薇", "dirovo"],
            "original_text": "分析dirovo2026年4-6月的表现，重点看主推商品的生意表现和投资情况",
            "include_bet": True,
        })
        result = self.route("三平台，然后看下最好的平台是哪个 再继续下钻分析这个平台的生意", state)
        self.assertEqual(result.type, "brand_platform_deep_dive")
        self.assertEqual((result.brand, result.period, result.platform), ("dirovo", "2026年4-6月", "TTL"))
        self.assertTrue(result.include_bet)
        self.assertIn("主推商品", result.original_text)
        self.assertIn("继续下钻", result.original_text)

    def test_prefixed_platform_reply_does_not_erase_brand_and_period(self):
        state = SessionState(pending_request={
            "intent": "business_platform_selection", "brand": "花知晓",
            "period": "2026年4-6月", "brand_aliases": ["花知晓", "Flower Knows"],
            "original_text": "分析一下花知晓2026年4-6月的表现，重点看主推商品的生意表现和投资情况",
            "include_bet": True,
        })
        result = self.route("先看三平台 再看哪个平台生意最好 下钻分析一下主推商品和媒体", state)
        self.assertEqual(result.type, "brand_platform_deep_dive")
        self.assertEqual((result.brand, result.period), ("花知晓", "2026年4-6月"))
        self.assertTrue(result.include_bet)

    def test_platform_slot_merge_does_not_depend_on_sentence_prefix(self):
        state = SessionState(pending_request={
            "intent": "business_platform_selection", "brand": "花知晓",
            "period": "2026年4-6月", "brand_aliases": ["花知晓", "Flower Knows"],
            "original_text": "花知晓2026年4-6月生意和投资情况", "include_bet": True,
        })
        result = self.route("我的选择是全平台。接下来请比较表现，并深入分析胜出的那一个", state)
        self.assertEqual(result.type, "brand_platform_deep_dive")
        self.assertEqual((result.brand, result.period, result.platform), ("花知晓", "2026年4-6月", "TTL"))

    def test_brand_slot_merge_inherits_time_and_platform(self):
        state = SessionState(pending_request={
            "intent": "v2_brand", "intents": ["EC_BUSINESS"],
            "period": "2026年Q2", "platform": "TTL",
            "original_text": "2026年Q2三平台品牌生意分析",
        })
        result = self.route("花知晓", state)
        self.assertEqual(result.type, "three_platform_competitor_analysis")
        self.assertEqual((result.brand, result.period, result.platform), ("花知晓", "2026年Q2", "TTL"))

    def test_route_trace_is_present(self):
        result = self.route("PROYA 2026年Q1天猫生意和BET")
        trace = result.route_decision["trace"]
        self.assertEqual(trace["router_version"], "v2")
        self.assertEqual(trace["final_action"], "clarify_analysis_scope")

    def test_media_scope_answer_resumes_only_matching_pending(self):
        state = SessionState(pending_request={
            "intent": "v2_media_scope", "intents": ["BET"],
            "brand": "美宝莲", "period": "2026年Q1", "question_mode": "lookup",
        })
        result = self.route("整体BET", state)
        self.assertEqual((result.type, result.brand, result.media_mode), (
            "media_analysis", "美宝莲", "OVERALL_BET",
        ))
        self.assertEqual(result.route_decision["decision_source"], "context_merge")

    def test_market_scope_answer_merges_all_pending_slots(self):
        state = SessionState(pending_request={
            "intent": "v2_market_scope", "question_mode": "ranking",
            "ranking_metric": "gmv_actual", "ranking_limit": 3,
        })
        result = self.route("2026年Q1 天猫 Pure Mass TTL Beauty", state)
        self.assertEqual(result.type, "market_brand_ranking")
        self.assertEqual((result.platform, result.segment, result.category), (
            "TM", "PURE MASS", "TOTAL BEAUTY",
        ))

    def test_long_market_bet_request_keeps_the_complete_plan(self):
        text = (
            "我想知道2026年6月pure mass top3的品牌有谁，先看他们三平台的生意分析，"
            "然后选择他们增长最多的平台再往下分析。最后看他们今年到目前为止最新的BET。"
        )
        result = self.route(text)
        self.assertEqual(result.type, "clarify_market_scope")
        self.assert_intents(result, ["MARKET", "BET"])
        self.assertTrue(result.include_bet)
        self.assertTrue(result.bet_latest_ytd)
        self.assertEqual(result.ranking_limit, 3)
        self.assertIn("已保留完整任务", result.message)
        self.assertIn("最新可用月", result.message)
        self.assertEqual(result.route_decision["missing_slots"], ["market_scope.category"])

    def test_market_scope_reply_does_not_collapse_composite_plan(self):
        original = "2026年6月Pure Mass Top3三平台下钻，最后看今年最新BET"
        state = SessionState(pending_request={
            "intent": "v2_market_scope", "original_text": original,
            "intents": ["MARKET", "BET"], "preflight_target": "market_brand_deep_dive",
            "include_bet": True, "bet_latest_ytd": True,
            "period": "2026年6月", "platform": "TTL", "segment": "PURE MASS",
            "question_mode": "ranking", "ranking_metric": "gmv_actual", "ranking_limit": 3,
        })
        result = self.route("TTL Beauty", state)
        self.assertEqual(result.type, "market_brand_deep_dive")
        self.assertEqual(result.original_text, original)
        self.assertTrue(result.include_bet)
        self.assertTrue(result.bet_latest_ytd)

    def test_collective_followup_uses_latest_top_brand_context(self):
        state = SessionState()
        state.market_context.period = "2026年6月"
        state.market_context.top_brands = ["L'OREAL PARIS", "PROYA", "KANS"]
        result = self.route("我想知道这三位分别在天猫、抖音、京东的生意如何", state)
        self.assertEqual(result.type, "market_brand_deep_dive")
        self.assertIsNone(result.brand)
        self.assertEqual((result.period, result.ranking_limit), ("2026年6月", 3))

    def test_listing_ranked_brands_does_not_fall_back_to_single_brand_confirmation(self):
        state = SessionState(pending_request={
            "intent": "v2_brand",
            "original_text": "我想知道这三位分别在天猫、抖音、京东的生意如何",
        })
        state.market_context.period = "2026年6月"
        state.market_context.top_brands = ["L'OREAL PARIS", "PROYA", "KANS"]
        result = self.route("欧莱雅 珀莱雅 和韩束", state)
        self.assertEqual(result.type, "market_brand_deep_dive")
        self.assertIsNone(result.brand)
        self.assertEqual(result.period, "2026年6月")

    def test_supported_market_category_is_preserved(self):
        result = self.route("2026年Q1天猫Pure Mass彩妆大盘Top 3品牌")
        self.assertEqual(result.type, "market_brand_ranking")
        self.assertEqual(result.category, "MAKEUP")
        self.assertNotIn("market_scope.category", result.route_decision["unsupported_requests"])

    def test_tmall_pure_mass_female_skincare_top5_is_executable(self):
        result = self.route("2026年7月天猫Pure Mass女士护肤Top5品牌")
        self.assertEqual(result.type, "market_brand_ranking")
        self.assertEqual(
            (result.platform, result.segment, result.category, result.period, result.ranking_limit),
            ("TM", "PURE MASS", "FEMALE SKINCARE", "2026年7月", 5),
        )
        self.assertFalse((result.route_decision or {}).get("unsupported_requests"))

    def test_multiple_commerce_platforms_become_ttl(self):
        result = self.route("珀莱雅2026年Q2在天猫、抖音、京东的GMV和类目表现")
        self.assertEqual(result.type, "three_platform_competitor_analysis")
        self.assertEqual(result.platform, "TTL")

    def test_clear_pronoun_inherits_validated_context(self):
        state = SessionState()
        state.drilldown_ctx.brand = "谷雨"
        state.drilldown_ctx.period = "2026年3月"
        result = self.route("那它媒体投资如何？", state)
        self.assertEqual((result.type, result.brand, result.period), (
            "media_analysis", "谷雨", "2026年3月",
        ))
        self.assertEqual(
            set(result.route_decision["trace"]["inherited_parameters"]),
            {"brand_surface", "period"},
        )

    def test_period_only_reply_keeps_bet_intent_and_template(self):
        state = SessionState()
        state.drilldown_ctx.last_analysis_view = "media_analysis"
        state.bet_context.brand = "韩束"
        state.bet_context.brand_aliases = ["韩束", "KANS"]
        state.bet_context.period = "2026年1-6月"
        state.bet_context.filters = {
            "media_mode": "OVERALL_BET",
            "media_channels": [],
        }
        result = self.route("到5月也可以的", state)
        self.assertEqual((result.type, result.brand), ("media_analysis", "韩束"))
        self.assertEqual(str(result.period), "5月")
        self.assertEqual(result.media_mode, "OVERALL_BET")

    @patch("bot.chains.brand_business_investment_chain.run_media_chain")
    @patch("bot.chains.brand_business_investment_chain.run_default_chain")
    def test_composite_chain_executes_each_bound_period(self, business, media):
        business.return_value = {"ok": True, "markdown": "business", "meta": {}}
        media.return_value = {"ok": True, "markdown": "bet", "meta": {}}
        business_period = ResolvedPeriod(
            "2026年6月", start_date="2026-06-01", end_date="2026-06-30",
        )
        bet_period = ResolvedPeriod(
            "2026年1-6月", start_date="2026-01-01", end_date="2026-06-30",
        )
        result = run_brand_business_investment_chain(
            "珀莱雅", "2026年6月", "TM",
            business_period=business_period, bet_period=bet_period,
        )
        self.assertEqual(str(business.call_args.args[1]), "2026年6月")
        self.assertEqual(str(media.call_args.args[1]), "2026年1-6月")
        self.assertEqual(result["meta"]["bet_period"], "2026年1-6月")


if __name__ == "__main__":
    unittest.main()
