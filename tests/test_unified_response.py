from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from bot.agent_plan import compile_agent_plan
from bot.meta_capability import render_meta_answer
from bot.router import RouteResult, route
from bot.routing_contracts import RouteDecision
from bot.session import DomainContext, SessionState
from bot.tools.query_data_availability import query_data_availability


class ResponseStrategyTest(unittest.TestCase):
    def test_legacy_routes_expose_unified_strategy(self):
        self.assertEqual(RouteResult(type="default_chain").response_strategy, "FULL_REPORT")
        self.assertEqual(RouteResult(type="skill_dispatch").response_strategy, "TARGETED_ANSWER")
        self.assertEqual(RouteResult(type="market_brand_deep_dive").response_strategy, "MULTI_STEP_ANALYSIS")
        self.assertEqual(RouteResult(type="meta").response_strategy, "META_ANSWER")
        self.assertEqual(RouteResult(type="clarify_period").response_strategy, "CLARIFY")

    def test_route_decision_and_plan_publish_strategy(self):
        decision = RouteDecision(intents=["EC_BUSINESS"], question_mode="report", action="default_chain")
        self.assertEqual(decision.to_dict()["response_strategy"], "FULL_REPORT")
        plan = compile_agent_plan(RouteResult(type="skill_dispatch"), "再看这个品类")
        self.assertEqual(plan.to_dict()["response_strategy"], "TARGETED_ANSWER")

    def test_no_prefix_targeted_question_reuses_context(self):
        state = SessionState(ec_context=DomainContext(
            brand="珀莱雅", period="2026年6月", platform="DY", brand_aliases=["PROYA"],
        ))
        with patch.dict(os.environ, {
            "UNIFIED_RESPONSE_STRATEGY_ENABLED": "1", "ROUTER_V2_ENABLED": "0",
        }, clear=False):
            result = route("防晒品类表现如何", state)
        self.assertEqual(result.type, "skill_dispatch")
        self.assertEqual(result.response_strategy, "TARGETED_ANSWER")
        self.assertEqual((result.brand, result.period, result.platform), ("珀莱雅", "2026年6月", "DY"))

    def test_complete_business_request_still_uses_report_recipe(self):
        with patch.dict(os.environ, {
            "UNIFIED_RESPONSE_STRATEGY_ENABLED": "1", "ROUTER_V2_ENABLED": "0",
        }, clear=False):
            result = route("珀莱雅2026年6月抖音生意怎么样", SessionState())
        self.assertEqual(result.type, "douyin_business_analysis")
        self.assertEqual(result.response_strategy, "FULL_REPORT")

    def test_explicit_scoped_targeted_question_does_not_pollute_brand(self):
        with patch.dict(os.environ, {
            "UNIFIED_RESPONSE_STRATEGY_ENABLED": "1", "ROUTER_V2_ENABLED": "0",
        }, clear=False):
            result = route("珀莱雅2026年6月抖音防晒品类表现如何", SessionState())
        self.assertEqual(result.type, "skill_dispatch")
        self.assertEqual((result.brand, result.period, result.platform), ("珀莱雅", "2026年6月", "DY"))


class DynamicMetaTest(unittest.TestCase):
    def test_platform_answer_comes_from_registry(self):
        result = render_meta_answer("抖音可以分析什么？", SessionState())
        self.assertIn("抖音目前可分析", result["markdown"])
        self.assertIn("Key Driver", result["markdown"])
        self.assertEqual(result["meta"]["capability_source"], "registry")

    def test_runtime_flag_changes_advertised_douyin_dimensions(self):
        with patch.dict(os.environ, {"DOUYIN_PRODUCT_DAILY_V2_ENABLED": "0"}, clear=False):
            disabled = render_meta_answer("抖音支持商品链接吗？", SessionState())
        with patch.dict(os.environ, {"DOUYIN_PRODUCT_DAILY_V2_ENABLED": "1"}, clear=False):
            enabled = render_meta_answer("抖音支持商品链接吗？", SessionState())
        self.assertIn("目前不支持", disabled["markdown"])
        self.assertIn("可以", enabled["markdown"])

    def test_unsupported_platform_dimension_is_explicit(self):
        result = render_meta_answer("京东支持Key Driver吗？", SessionState())
        self.assertIn("目前不支持", result["markdown"])
        self.assertIn("不会降级", result["markdown"])

    def test_current_context_next_steps_are_personalized(self):
        evidence = {
            "brand": "珀莱雅", "platform": "DY", "period": {"raw": "2026年6月"},
            "selected_category": "防晒霜",
            "available_dimensions": ["category", "series", "key_driver", "product_link"],
        }
        state = SessionState(ec_context=DomainContext(
            brand="珀莱雅", period="2026年6月", platform="DY", recent_evidence=[evidence],
        ))
        result = render_meta_answer("接下来还能看什么？", state)
        self.assertIn("珀莱雅", result["markdown"])
        self.assertIn("防晒霜", result["markdown"])
        self.assertIn("Key Driver", result["markdown"])

    def test_meta_question_routes_without_data_analysis(self):
        with patch.dict(os.environ, {"DYNAMIC_META_CAPABILITY_ENABLED": "1"}, clear=False):
            result = route("抖音可以分析什么？", SessionState())
        self.assertEqual(result.type, "meta")
        self.assertEqual(result.response_strategy, "META_ANSWER")

    def test_dynamic_meta_emits_no_data_progress_or_document(self):
        from bot.app import run_agent

        progress = []
        with patch.dict(os.environ, {
            "DYNAMIC_META_CAPABILITY_ENABLED": "1",
            "AGENT_PLAN_LAYER_ENABLED": "1",
            "AGENT_PLAN_LAYER_SHADOW": "0",
        }, clear=False):
            result = run_agent("dynamic-meta-test", "京东支持Key Driver吗？", SessionState(), on_progress=progress.append)
        self.assertEqual(progress, [])
        self.assertFalse(result["meta"]["document_ready"])
        self.assertEqual(result["meta"]["response_strategy"], "META_ANSWER")

    def test_data_availability_uses_controlled_lookup(self):
        state = SessionState(ec_context=DomainContext(brand="珀莱雅", platform="DY"))
        with patch("bot.tools.query_data_availability.fetch_one", return_value={"max_date": "2026-07-19"}) as query:
            result = query_data_availability("抖音数据最新到什么时候？", state)
        self.assertIn("2026-07-19", result["markdown"])
        self.assertEqual(query.call_count, 1)
        self.assertEqual(route("数据最新到什么时候？", state).type, "data_availability")


if __name__ == "__main__":
    unittest.main()
