from __future__ import annotations

import os
import unittest
from datetime import date
from unittest.mock import patch

from bot.entity_resolution import (
    ResolvedPeriod,
    build_comparison_spec,
    resolve_entities,
    resolve_time_scope,
)
from bot.media_period import parse_media_period
from bot.router import route
from bot.session import BusinessTaskContext, SessionState
from bot.utils import parse_ec_period


class EntityResolutionGoldenSetTest(unittest.TestCase):
    def test_ytd_is_resolved_as_year_start_through_as_of_date(self):
        result = resolve_time_scope(
            "2026年YTD 天猫女士护肤top5",
            current_year=2026,
            as_of_date=date(2026, 8, 25),
        )
        self.assertEqual(result.focus_period.start_date, "2026-01-01")
        self.assertEqual(result.focus_period.end_date, "2026-08-25")
        self.assertEqual(result.mentions[0].granularity, "YEAR_TO_DATE")
        self.assertEqual(str(result.focus_period), "2026年YTD")

    def test_mtd_is_resolved_as_month_start_through_as_of_date(self):
        result = resolve_time_scope(
            "MTD珀莱雅抖音生意",
            current_year=2026,
            as_of_date=date(2026, 8, 25),
        )
        self.assertEqual(result.focus_period.start_date, "2026-08-01")
        self.assertEqual(result.focus_period.end_date, "2026-08-25")
        self.assertEqual(result.mentions[0].granularity, "MONTH_TO_DATE")

    def test_chinese_to_date_aliases_are_supported(self):
        ytd = resolve_time_scope(
            "今年以来", current_year=2026,
            as_of_date=date(2026, 8, 25),
        )
        mtd = resolve_time_scope(
            "本月至今", current_year=2026,
            as_of_date=date(2026, 8, 25),
        )
        self.assertEqual(ytd.focus_period.start_date, "2026-01-01")
        self.assertEqual(mtd.focus_period.start_date, "2026-08-01")

    def test_year_only_is_full_year_and_does_not_pollute_brand(self):
        result = resolve_entities("林清玄2024年生意咋样", current_year=2026)
        self.assertEqual(result.brand.surface, "林清玄")
        self.assertEqual(result.brand.canonical_display_name, "Forest Cabin")
        self.assertEqual(result.time_scope.focus_period.start_date, "2024-01-01")
        self.assertEqual(result.time_scope.focus_period.end_date, "2024-12-31")

    def test_two_digit_year_month_range_is_explicit(self):
        result = resolve_entities("花西子26年1-3月BET", current_year=2026)
        self.assertEqual(str(result.time_scope.focus_period), "2026年1-3月")
        self.assertEqual(result.time_scope.mentions[0].year_source, "explicit")

    def test_english_date_range_defaults_to_current_year(self):
        result = resolve_entities(
            "Please analyze Proya business During May 13 to June 3rd",
            current_year=2026,
        )
        self.assertEqual(result.brand.canonical_display_name, "PROYA")
        self.assertEqual(result.time_scope.focus_period.start_date, "2026-05-13")
        self.assertEqual(result.time_scope.focus_period.end_date, "2026-06-03")

    def test_market_ranking_does_not_create_fake_brand(self):
        result = resolve_entities(
            "分析2026年q2 mass beauty top3品牌表现", current_year=2026,
        )
        self.assertEqual(result.brand.status, "missing")
        self.assertEqual(result.brand.match_method, "market_brand_not_required")

    def test_long_mass_top_question_does_not_fallback_to_task_words_as_brand(self):
        result = resolve_entities(
            "分析一下今年2026年1-6月，mass top品牌的表现，需要跨三平台分析",
            current_year=2026,
        )
        self.assertEqual(result.brand.status, "missing")

    def test_brand_before_time(self):
        result = resolve_entities("分析PROYA 2026年Q1天猫生意和BET投资", current_year=2026)
        self.assertEqual(result.brand.surface, "PROYA")
        self.assertEqual(result.brand.canonical_display_name, "PROYA")
        self.assertEqual(str(result.time_scope.focus_period), "2026年Q1")

    def test_time_before_brand(self):
        result = resolve_entities("分析2026年3月谷雨的媒体投资", current_year=2026)
        self.assertEqual(result.brand.surface, "谷雨")
        self.assertEqual(result.time_scope.mentions[0].raw_text, "2026年3月")
        self.assertEqual(result.brand.source_mappings["topline"], "GRAIN RAIN")
        self.assertEqual(result.brand.source_mappings["ksi"], "GRAIN RAIN")

    def test_long_date_range_beats_single_date(self):
        result = resolve_entities("看珀莱雅7月1日至15日京东表现", current_year=2026)
        mention = result.time_scope.mentions[0]
        self.assertEqual(mention.raw_text, "7月1日至15日")
        self.assertEqual(mention.canonical, "2026年7月1日至7月15日")
        self.assertEqual(mention.resolution_status, "exact")
        self.assertEqual(mention.year_source, "default_current_year")

    def test_yearless_month_defaults_to_current_year(self):
        result = resolve_entities("谷雨6月怎么样", current_year=2026)
        self.assertEqual(result.time_scope.missing_slots, [])
        self.assertEqual(str(result.time_scope.focus_period), "2026年6月")
        self.assertEqual(result.time_scope.mentions[0].year_source, "default_current_year")

    def test_explicit_relative_year_is_exact(self):
        result = resolve_entities("谷雨今年6月怎么样", current_year=2026)
        self.assertEqual(str(result.time_scope.focus_period), "2026年6月")
        self.assertEqual(result.time_scope.mentions[0].year_source, "relative")

    def test_campaign_defaults_year_but_requires_window(self):
        result = resolve_entities("谷雨618怎么样", current_year=2026)
        self.assertEqual(result.time_scope.campaign_name, "618")
        self.assertEqual(result.time_scope.missing_slots, ["time_scope.campaign_window"])

    def test_campaign_with_explicit_window_does_not_reprompt(self):
        result = resolve_entities(
            "谷雨618，窗口是2026年5月13日至6月7日",
            current_year=2026,
        )
        self.assertEqual(result.time_scope.missing_slots, [])
        self.assertEqual(result.time_scope.campaign_name, "618")
        self.assertEqual(result.time_scope.focus_period.start_date, "2026-05-13")
        self.assertEqual(result.time_scope.focus_period.end_date, "2026-06-07")

    def test_period_roles_follow_relation_words(self):
        result = resolve_entities("2026Q2同比2025Q2", current_year=2026)
        self.assertEqual([item.role for item in result.time_scope.mentions], ["FOCUS", "COMPARISON"])
        self.assertEqual(result.time_scope.focus_period.comparison_start, "2025-04-01")
        self.assertEqual(result.time_scope.focus_period.comparison_end, "2025-06-30")

    def test_multiple_periods_without_relation_are_ambiguous(self):
        result = resolve_time_scope("2026Q1 2026Q2", current_year=2026)
        self.assertIn("time_scope.period_roles", result.missing_slots)

    def test_two_observation_months_default_to_yoy_vs_yoy(self):
        scope = resolve_time_scope("比较2026年3月和2026年4月", current_year=2026)
        spec = build_comparison_spec(scope, "比较2026年3月和2026年4月")
        self.assertEqual(spec["mode"], "YOY_ALIGNED")
        self.assertEqual(
            [item["start_date"] for item in spec["analysis_periods"]],
            ["2026-03-01", "2026-04-01"],
        )
        self.assertEqual(
            [item["start_date"] for item in spec["baseline_periods"]],
            ["2025-03-01", "2025-04-01"],
        )
        self.assertEqual(spec["comparison_target"], "EVOLUTION")

    def test_explicit_month_on_month_uses_direct_sequential_baseline(self):
        scope = resolve_time_scope("2026年4月环比2026年3月", current_year=2026)
        spec = build_comparison_spec(scope, "2026年4月环比2026年3月")
        self.assertNotIn("time_scope.period_roles", scope.missing_slots)
        self.assertEqual(spec["mode"], "MOM")
        self.assertEqual(spec["analysis_periods"][0]["start_date"], "2026-04-01")
        self.assertEqual(spec["baseline_periods"][0]["start_date"], "2026-03-01")
        self.assertEqual(spec["comparison_target"], "ACTUAL")

    def test_explicit_actual_difference_is_not_treated_as_yoy(self):
        text = "2026年4月比2026年3月GMV多多少"
        scope = resolve_time_scope(text, current_year=2026)
        spec = build_comparison_spec(scope, text)
        self.assertNotIn("time_scope.period_roles", scope.missing_slots)
        self.assertEqual(spec["mode"], "DIRECT_ACTUAL")
        self.assertEqual(spec["comparison_target"], "ACTUAL")

    def test_same_brand_chinese_and_english_collapses(self):
        result = resolve_entities("珀莱雅PROYA 2026年Q1生意", current_year=2026)
        self.assertEqual(result.brand.status, "resolved")
        self.assertEqual(result.brand.canonical_brand_key, "proya")

    def test_two_real_brands_do_not_concatenate(self):
        result = resolve_entities("珀莱雅和韩束2026年Q1生意", current_year=2026)
        self.assertEqual(result.brand.status, "ambiguous")
        self.assertEqual(
            {item.canonical_display_name for item in result.brand.candidates},
            {"PROYA", "KANS"},
        )

    def test_unknown_brand_never_becomes_resolved(self):
        result = resolve_entities("完全不存在牌2026年Q1生意", current_year=2026)
        self.assertNotEqual(result.brand.status, "resolved")

    def test_entity_spans_do_not_overlap(self):
        result = resolve_entities("分析2026年3月谷雨的媒体投资", current_year=2026)
        time = result.time_scope.mentions[0]
        self.assertFalse(
            result.brand.start_offset < time.end_offset
            and result.brand.end_offset > time.start_offset
        )

    def test_invalid_calendar_date_is_rejected(self):
        result = resolve_time_scope("2026年2月30日", current_year=2026)
        self.assertIn("time_scope.valid_period", result.missing_slots)

    def test_existing_explicit_date_spellings_remain_supported(self):
        cases = {
            "20260101-20260328": ("2026-01-01", "2026-03-28"),
            "2026/1/1 - 2026/3/28": ("2026-01-01", "2026-03-28"),
            "2026-1-1 - 2026-03-28": ("2026-01-01", "2026-03-28"),
            "2026-01~2026-03": ("2026-01-01", "2026-03-31"),
            "2026 1-6": ("2026-01-01", "2026-06-30"),
            "2026-07-10": ("2026-07-10", "2026-07-10"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                result = resolve_time_scope(text, current_year=2026)
                self.assertEqual(
                    (result.focus_period.start_date, result.focus_period.end_date),
                    expected,
                )

    def test_resolved_period_bypasses_ec_reparse(self):
        period = ResolvedPeriod(
            "自定义窗口", start_date="2026-05-13", end_date="2026-06-07",
            comparison_start="2025-05-13", comparison_end="2025-06-07",
        )
        parsed = parse_ec_period(period, 1999)
        self.assertEqual(parsed["current_start"], "2026-05-13")
        self.assertEqual(parsed["resolution_source"], "entity_resolver_v2")

    def test_resolved_period_bypasses_media_reparse(self):
        period = ResolvedPeriod("自定义窗口", start_date="2026-05-13", end_date="2026-06-07")
        parsed = parse_media_period(period)
        self.assertEqual(parsed.focus_start, "2026-05-01")
        self.assertEqual(parsed.focus_end, "2026-06-30")


class EntityRouterIntegrationTest(unittest.TestCase):
    def routed(self, text: str, state: SessionState | None = None):
        with patch.dict(os.environ, {
            "ROUTER_V2_ENABLED": "1", "ENTITY_RESOLVER_V2_ENABLED": "1",
        }, clear=False):
            return route(text, state or SessionState())

    def test_yearless_month_routes_with_current_year(self):
        result = self.routed("谷雨6月怎么样")
        self.assertEqual(result.type, "clarify_ec_platform")
        self.assertEqual(result.brand, "谷雨")
        self.assertEqual(str(result.period), "2026年6月")

    def test_current_year_phrase_routes_without_year_confirmation(self):
        result = self.routed("谷雨今年6月怎么样")
        self.assertEqual(result.type, "clarify_ec_platform")
        self.assertEqual(getattr(result.period, "start_date", None), "2026-06-01")

    def test_ytd_market_ranking_keeps_accumulated_period(self):
        result = self.routed("2026年YTD 天猫女士护肤top5")
        self.assertEqual(result.type, "market_brand_ranking")
        self.assertEqual(result.platform, "TM")
        self.assertEqual(result.category, "FEMALE SKINCARE")
        self.assertEqual(str(result.period), "2026年YTD")
        self.assertEqual(parse_ec_period(result.period, 2026)["current_start"], "2026-01-01")

    def test_campaign_routes_to_window_request(self):
        result = self.routed("谷雨618怎么样")
        self.assertEqual(result.type, "provide_campaign_window")

    def test_aligned_year_pair_does_not_require_time_role_clarification(self):
        result = self.routed("欧莱雅旗舰店天猫2026年7月和2025年7月的生意")
        self.assertEqual(result.type, "default_chain")
        self.assertEqual(result.comparison_spec["mode"], "YOY_ALIGNED")
        self.assertEqual(result.comparison_spec["source"], "inferred_aligned_pair")

    def test_ascii_vs_adjacent_to_chinese_assigns_explicit_time_roles(self):
        result = self.routed(
            "欧莱雅旗舰店 天猫 2026年7月的生意vs 2025年7月的生意 你有什么发现 请分析"
        )
        self.assertEqual(result.type, "default_chain")
        self.assertEqual(str(result.period), "2026年7月")
        self.assertEqual(result.period.comparison_start, "2025-07-01")
        self.assertEqual(result.comparison_spec["mode"], "YOY_ALIGNED")

    def test_two_douyin_observation_months_route_to_yoy_comparison_execution(self):
        result = self.routed("你能分析一下珀莱雅5月和6月在抖音的生意吗")
        self.assertEqual(result.type, "multi_period_business_analysis")
        self.assertEqual((result.brand, result.platform), ("珀莱雅", "DY"))
        self.assertEqual(result.comparison_spec["mode"], "YOY_ALIGNED")
        self.assertEqual(
            [item["start_date"] for item in result.comparison_spec["analysis_periods"]],
            ["2026-05-01", "2026-06-01"],
        )
        self.assertIn("MULTI_OBSERVATION_YOY_PLAN", result.route_decision["reason_codes"])

    def test_multi_observation_time_plan_is_independent_of_business_dimensions(self):
        cases = [
            ("分析珀莱雅2026年5月和6月在天猫的生意", "TM", "珀莱雅", None),
            ("分析珀莱雅2026年5月和6月在抖音的生意", "DY", "珀莱雅", None),
            ("分析珀莱雅2026年5月和6月在京东的生意", "JD", "珀莱雅", None),
            ("分析珀莱雅2026年5月和6月三平台的生意", "TTL", "珀莱雅", None),
            ("分析2026年5月和6月天猫Pure Mass TTL Beauty大盘", "TM", None, "TOTAL BEAUTY"),
            ("分析2026年5月和6月三平台Pure Mass女士护肤大盘", "TTL", None, "FEMALE SKINCARE"),
        ]
        for text, platform, brand, category in cases:
            with self.subTest(text=text):
                result = self.routed(text)
                self.assertEqual(result.type, "multi_period_business_analysis")
                self.assertEqual(result.platform, platform)
                self.assertEqual(result.brand, brand)
                self.assertEqual(result.category, category)
                self.assertEqual(result.comparison_spec["mode"], "YOY_ALIGNED")
                self.assertEqual(len(result.comparison_spec["analysis_periods"]), 2)
                spec = result.business_spec
                self.assertEqual(spec["subject_scope"], "market" if brand is None else "brand")
                self.assertEqual(spec["category"], category or "TOTAL BEAUTY")
                self.assertEqual(
                    spec["platforms"],
                    ["TM", "DY", "JD"] if platform == "TTL" else [platform],
                )

    def test_brand_business_preserves_macro_category_scope(self):
        result = self.routed("分析珀莱雅2026年5月和6月天猫女士护肤生意")
        self.assertEqual(result.type, "multi_period_business_analysis")
        self.assertEqual(result.category, "FEMALE SKINCARE")

    def test_production_tmall_month_difference_question_uses_pairwise_yoy_plan(self):
        result = self.routed("那欧莱雅旗舰店在天猫 3月和5月的生意有什么区别?")
        self.assertEqual(result.type, "multi_period_business_analysis")
        self.assertEqual((result.brand, result.platform), ("欧莱雅", "TM"))
        self.assertEqual(
            [item["start_date"] for item in result.comparison_spec["analysis_periods"]],
            ["2026-03-01", "2026-05-01"],
        )
        self.assertEqual(
            [item["start_date"] for item in result.comparison_spec["baseline_periods"]],
            ["2025-03-01", "2025-05-01"],
        )

    def test_same_year_months_with_vs_still_compare_pairwise_yoy_by_default(self):
        result = self.routed("比较欧莱雅天猫2026年3月vs2026年5月的生意")
        self.assertEqual(result.type, "multi_period_business_analysis")
        self.assertEqual(result.comparison_spec["source"], "business_default")
        self.assertEqual(result.comparison_spec["comparison_target"], "EVOLUTION")

    def test_market_without_segment_keeps_pure_mass_default(self):
        result = self.routed("分析2026年5月和6月天猫TTL Beauty大盘")
        self.assertEqual(result.type, "multi_period_business_analysis")
        self.assertEqual(result.segment, "PURE MASS")

    def test_complete_complex_market_plan_does_not_ask_for_empty_scope(self):
        result = self.routed(
            "我想知道2026年6月pure mass top3的品牌有谁，先看他们三平台的生意分析，"
            "然后选择他们增长最多的平台再往下分析。最后看他们今年到目前为止最新的BET。"
        )

        self.assertEqual(result.type, "clarify_analysis_scope")
        self.assertEqual(result.segment, "PURE MASS")
        self.assertEqual(result.category, "TOTAL BEAUTY")
        self.assertEqual(result.platform, "TTL")
        self.assertEqual(result.route_decision["missing_slots"], [])
        self.assertNotIn("开始前还需要你补充：。", result.message or "")

    def test_time_role_reply_uses_active_task_and_ignores_stale_domain_context(self):
        original = "欧莱雅旗舰店天猫2026年7月和2025年7月的生意"
        state = SessionState(pending_request={
            "intent": "v2_time_roles",
            "original_text": original,
            "brand": "欧莱雅旗舰店",
            "brand_aliases": ["欧莱雅旗舰店", "L'OREAL PARIS"],
            "platform": "TM",
            "intents": ["EC_BUSINESS"],
        })
        state.task_context = BusinessTaskContext(
            original_question=original,
            brand="欧莱雅旗舰店",
            brand_aliases=["欧莱雅旗舰店", "L'OREAL PARIS"],
            platform="TM",
            intents=["EC_BUSINESS"], goals=["EC_BUSINESS"],
            awaiting_slot="time_roles", status="awaiting",
        )
        state.ec_context.brand = "珀莱雅"
        state.ec_context.period = "2026年6月"
        state.ec_context.platform = "DY"
        state.ec_context.filters = {"category": "面部护理套装"}

        result = self.routed("分析2026年7月，对比2025年7月", state)

        self.assertEqual(result.type, "default_chain")
        self.assertEqual((result.brand, result.platform), ("欧莱雅旗舰店", "TM"))
        self.assertEqual(str(result.period), "2026年7月")
        self.assertEqual(result.period.comparison_start, "2025-07-01")
        self.assertEqual(result.comparison_spec["mode"], "YOY_ALIGNED")
        self.assertEqual(result.route_decision["decision_source"], "context_merge")

    def test_complete_new_brand_request_wins_over_pending_time_roles(self):
        original = "欧莱雅天猫2026年7月和2025年7月的生意"
        state = SessionState(pending_request={
            "intent": "v2_time_roles", "original_text": original,
            "brand": "欧莱雅", "brand_aliases": ["欧莱雅", "L'OREAL PARIS"],
            "platform": "TM", "intents": ["EC_BUSINESS"],
        })
        state.task_context = BusinessTaskContext(
            original_question=original, brand="欧莱雅",
            brand_aliases=["欧莱雅", "L'OREAL PARIS"], platform="TM",
            intents=["EC_BUSINESS"], goals=["EC_BUSINESS"],
            awaiting_slot="time_roles", status="awaiting",
        )

        result = self.routed("珀莱雅天猫分析2026年6月，对比2025年6月", state)

        self.assertEqual(result.type, "default_chain")
        self.assertEqual(result.brand, "珀莱雅")
        self.assertEqual(result.route_decision["task_context_patch"]["relation"], "NEW_TASK")

    def test_complete_same_brand_request_wins_over_pending_time_roles(self):
        original = "欧莱雅天猫2026年7月和2025年7月的生意"
        state = SessionState(pending_request={
            "intent": "v2_time_roles", "original_text": original,
            "brand": "欧莱雅", "brand_aliases": ["欧莱雅", "L'OREAL PARIS"],
            "platform": "TM", "intents": ["EC_BUSINESS"],
        })
        state.task_context = BusinessTaskContext(
            original_question=original, brand="欧莱雅",
            brand_aliases=["欧莱雅", "L'OREAL PARIS"], platform="TM",
            intents=["EC_BUSINESS"], goals=["EC_BUSINESS"],
            awaiting_slot="time_roles", status="awaiting",
        )

        result = self.routed("那欧莱雅旗舰店在天猫 3月和5月的生意有什么区别?", state)

        self.assertEqual(result.type, "multi_period_business_analysis")
        self.assertEqual((result.brand, result.platform), ("欧莱雅", "TM"))
        self.assertEqual(
            [item["start_date"] for item in result.comparison_spec["analysis_periods"]],
            ["2026-03-01", "2026-05-01"],
        )
        self.assertEqual(
            [item["start_date"] for item in result.comparison_spec["baseline_periods"]],
            ["2025-03-01", "2025-05-01"],
        )

    def test_new_brand_multi_period_request_is_not_captured_by_old_time_clarification(self):
        original = "欧莱雅天猫2026年3月和5月的生意"
        state = SessionState(pending_request={
            "intent": "v2_time_roles", "original_text": original,
            "brand": "欧莱雅", "brand_aliases": ["欧莱雅", "L'OREAL PARIS"],
            "platform": "TM", "intents": ["EC_BUSINESS"],
        })
        state.task_context = BusinessTaskContext(
            original_question=original, brand="欧莱雅",
            brand_aliases=["欧莱雅", "L'OREAL PARIS"], platform="TM",
            intents=["EC_BUSINESS"], goals=["EC_BUSINESS"],
            awaiting_slot="time_roles", status="awaiting",
        )

        result = self.routed("你能分析一下谷雨5月和6月在天猫的生意吗", state)

        self.assertEqual(result.type, "multi_period_business_analysis")
        self.assertEqual((result.brand, result.platform), ("谷雨", "TM"))
        self.assertEqual(result.route_decision["task_context_patch"]["relation"], "NEW_TASK")

    def test_year_confirmation_is_task_scoped(self):
        state = SessionState(pending_request={
            "intent": "confirm_current_year",
            "original_text": "谷雨6月怎么样",
            "proposed_year": 2026,
        })
        result = self.routed("是", state)
        self.assertEqual(result.type, "clarify_ec_platform")
        self.assertEqual(str(result.period), "2026年6月")

    def test_campaign_window_reply_is_preserved_structurally(self):
        state = SessionState(pending_request={
            "intent": "provide_campaign_window",
            "original_text": "谷雨618怎么样",
        })
        result = self.routed("2026年5月13日至6月7日", state)
        self.assertEqual(result.type, "clarify_ec_platform")
        self.assertEqual(result.period.start_date, "2026-05-13")
        self.assertEqual(result.period.end_date, "2026-06-07")

    def test_brand_candidate_reply_selects_requested_brand(self):
        first = self.routed("珀莱雅和韩束2026年Q1生意")
        self.assertEqual(first.type, "confirm_brand_candidate")
        candidates = first.brand_resolution["candidates"]
        kans_index = next(
            index for index, item in enumerate(candidates, 1)
            if item["canonical_display_name"] == "KANS"
        )
        state = SessionState(pending_request={
            "intent": "confirm_brand_candidate",
            "original_text": "珀莱雅和韩束2026年Q1生意",
            "candidates": candidates,
        })
        result = self.routed(str(kans_index), state)
        self.assertEqual(result.brand, "韩束")
        self.assertIn("KANS", result.brand_aliases)

    def test_complete_new_request_overrides_entity_pending(self):
        state = SessionState(pending_request={
            "intent": "confirm_current_year",
            "original_text": "旧品牌6月怎么样",
            "proposed_year": 2026,
        })
        result = self.routed("美宝莲2026年Q1抖音生意", state)
        self.assertEqual((result.brand, result.platform), ("美宝莲", "DY"))

    def test_composite_clauses_bind_periods_without_time_clarification(self):
        result = self.routed("珀莱雅6月天猫生意以及截止到6月BET")
        self.assertEqual(result.type, "clarify_analysis_scope")
        self.assertEqual(
            [(item["intent"], item["time_ref"]) for item in result.task_bindings],
            [("EC_BUSINESS", 0), ("BET", 1)],
        )
        self.assertNotIn("time_scope.period_roles", result.route_decision["missing_slots"])

    def test_composite_task_periods_survive_scope_confirmation(self):
        first = self.routed("珀莱雅6月天猫生意以及截止到6月BET")
        state = SessionState(pending_request={
            "intent": "analysis_preflight",
            "target": first.preflight_target,
            "brand": first.brand, "period": first.period,
            "brand_aliases": first.brand_aliases,
            "platform": first.platform,
            "media_mode": first.media_mode,
            "media_channels": first.media_channels,
            "task_bindings": first.task_bindings,
        })
        result = self.routed("确认", state)
        self.assertEqual(result.type, "brand_business_investment_analysis")
        self.assertEqual(result.business_period.start_date, "2026-06-01")
        self.assertEqual(result.bet_period.end_date, "2026-06-30")

    def test_market_request_overrides_unrelated_pending_without_brand(self):
        state = SessionState(pending_request={
            "intent": "confirm_brand_candidate", "original_text": "旧问题",
            "candidates": [],
        })
        result = self.routed("2026年Q2天猫Pure Mass TTL Beauty大盘Top 3", state)
        self.assertEqual(result.type, "market_brand_ranking")
        self.assertIsNone(result.brand)


if __name__ == "__main__":
    unittest.main()
