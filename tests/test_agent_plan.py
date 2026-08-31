from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from bot.agent_plan import (
    AgentPlan,
    PlanStep,
    PlanValidationError,
    compile_agent_plan,
    completion_status,
    initial_status,
    validate_agent_plan,
)
from bot.router import RouteResult
from bot.session import SessionState


class AgentPlanCompilerTest(unittest.TestCase):
    def test_meta_question_has_no_data_step(self):
        plan = compile_agent_plan(RouteResult(type="meta"), "你能做什么？")
        self.assertFalse(plan.requires_data)
        self.assertEqual(plan.steps[0].executor, "answer_from_knowledge")

    def test_single_brand_compare_then_drill_is_executable_recipe(self):
        route = RouteResult(
            type="brand_platform_deep_dive", brand="dirovo", period="2026年4-6月",
            platform="TTL", include_bet=True,
        )
        plan = compile_agent_plan(route, "三平台比较最好的平台，再继续下钻，并看投资情况")
        self.assertEqual(plan.version, "executable-plan-v2")
        self.assertEqual(
            [step.kind for step in plan.steps],
            ["QUERY", "DERIVE", "TEMPLATE", "TEMPLATE", "SYNTHESIS"],
        )
        self.assertEqual(plan.steps[0].capability_id, "brand.three_platform_matrix")
        self.assertEqual(plan.steps[1].inputs["rank_by"], "gmv_growth")

    def test_ec_and_bet_is_a_parallel_recipe_with_synthesis(self):
        route = RouteResult(
            type="brand_business_investment_analysis",
            brand="PROYA",
            period="2026年1-6月",
            platform="TM",
        )
        plan = compile_agent_plan(route, "分析PROYA 2026年1-6月天猫生意和BET")
        self.assertEqual(plan.source, "recipe")
        self.assertEqual([step.step_id for step in plan.steps], ["ec", "bet", "synthesize"])
        self.assertEqual(set(plan.steps[-1].depends_on), {"ec", "bet"})

    def test_complex_market_question_keeps_requested_dimensions(self):
        route = RouteResult(
            type="market_brand_deep_dive",
            period="2026年1-6月",
            platform="TTL",
            segment="PURE MASS",
            category="TOTAL BEAUTY",
            ranking_metric="gmv_actual",
            ranking_limit=5,
        )
        text = (
            "分析2026年1-6月mass top品牌的三平台表现，"
            "告诉我在平台、选品、生意节奏、价格和促销上有什么值得学习"
        )
        plan = compile_agent_plan(route, text)
        self.assertEqual(plan.source, "recipe")
        self.assertEqual(
            set(plan.requested_dimensions),
            {"platform", "assortment", "business_cadence", "price", "promotion", "ranking"},
        )
        self.assertEqual(plan.steps[1].depends_on, ("ranking",))

    def test_market_bet_plan_fans_out_after_ranking(self):
        route = RouteResult(
            type="market_brand_deep_dive", period="2026年6月", platform="TTL",
            segment="PURE MASS", category="TOTAL BEAUTY", ranking_metric="gmv_actual",
            ranking_limit=3, include_bet=True, bet_latest_ytd=True,
        )
        plan = compile_agent_plan(route, "Top3三平台分析，最后看今年最新BET")
        executors = [step.executor for step in plan.steps]
        self.assertIn("market_brand_platform_template_fanout", executors)
        self.assertIn("market_brand_bet_fanout", executors)
        synthesis = plan.steps[-1]
        self.assertEqual(set(synthesis.depends_on), {"platform_templates", "bet_fanout"})

    def test_validator_rejects_cycles(self):
        plan = AgentPlan(
            plan_id="p1", route_type="x", title="x", source="recipe",
            execution_adapter="x", requires_data=True, requires_synthesis=False,
            steps=(
                PlanStep("a", "a", "market_summary", ("b",)),
                PlanStep("b", "b", "market_summary", ("a",)),
            ),
        )
        with self.assertRaisesRegex(PlanValidationError, "cycle"):
            validate_agent_plan(plan)

    def test_validator_rejects_unregistered_executor(self):
        plan = AgentPlan(
            plan_id="p1", route_type="x", title="x", source="generated",
            execution_adapter="x", requires_data=True, requires_synthesis=False,
            steps=(PlanStep("a", "a", "arbitrary_shell"),),
        )
        with self.assertRaisesRegex(PlanValidationError, "not registered"):
            validate_agent_plan(plan)


class SmartMessageTest(unittest.TestCase):
    def test_initial_message_is_neutral_before_routing(self):
        with patch.dict(os.environ, {"BOT_SMART_PROGRESS_ENABLED": "1"}, clear=False):
            message = initial_status(queued=False)
        self.assertIn("理解", message)
        self.assertNotIn("分析数据", message)

    def test_meta_and_clarification_completion_are_not_data_copy(self):
        with patch.dict(os.environ, {"BOT_SMART_PROGRESS_ENABLED": "1"}, clear=False):
            self.assertIn("整理好回答", completion_status("meta"))
            self.assertIn("确认", completion_status("clarify_market_scope"))


class AgentPlanIntegrationTest(unittest.TestCase):
    def test_enabled_plan_is_attached_but_meta_emits_no_data_progress(self):
        from bot.app import run_agent

        progress: list[str] = []
        result = {"route_type": "meta", "markdown": "capabilities", "meta": {}}
        with patch.dict(os.environ, {
            "AGENT_PLAN_LAYER_ENABLED": "1",
            "AGENT_PLAN_LAYER_SHADOW": "0",
            "BOT_SMART_PROGRESS_ENABLED": "1",
        }, clear=False), patch("bot.app.route", return_value=RouteResult(type="meta")), \
             patch("bot.app._run_direct", return_value=result):
            output = run_agent("u1", "你能做什么", SessionState(), on_progress=progress.append)
        self.assertEqual(progress, [])
        self.assertFalse(output["meta"]["agent_plan"]["requires_data"])

    def test_enabled_composite_plan_emits_specific_progress(self):
        from bot.app import run_agent

        progress: list[str] = []
        route = RouteResult(
            type="brand_business_investment_analysis",
            brand="PROYA", period="2026年1-6月", platform="TM",
        )
        result = {
            "route_type": route.type,
            "markdown": "report",
            "meta": {"document_ready": True},
        }
        with patch.dict(os.environ, {
            "AGENT_PLAN_LAYER_ENABLED": "1",
            "AGENT_PLAN_LAYER_SHADOW": "0",
            "BOT_SMART_PROGRESS_ENABLED": "1",
        }, clear=False), patch("bot.app.route", return_value=route), \
             patch("bot.app._run_direct", return_value=result):
            output = run_agent("u1", "PROYA天猫生意和BET", SessionState(), on_progress=progress.append)
        self.assertIn("电商生意和BET", progress[0])
        self.assertIn("整理成可读结论", progress[-1])
        self.assertEqual(output["meta"]["agent_plan"]["source"], "recipe")

    def test_confirmation_keeps_dimensions_from_original_long_question(self):
        from bot.app import run_agent

        original = "分析mass top品牌在平台、选品、生意节奏、价格和促销上的表现"
        route = RouteResult(
            type="market_brand_deep_dive",
            original_text=original,
            period="2026年1-6月",
            platform="TTL",
            segment="PURE MASS",
            ranking_metric="gmv_actual",
            ranking_limit=5,
        )
        result = {"route_type": route.type, "markdown": "report", "meta": {}}
        with patch.dict(os.environ, {
            "AGENT_PLAN_LAYER_ENABLED": "1",
            "AGENT_PLAN_LAYER_SHADOW": "0",
        }, clear=False), patch("bot.app.route", return_value=route), \
             patch("bot.app._run_direct", return_value=result):
            output = run_agent("u1", "确认", SessionState())
        dimensions = set(output["meta"]["agent_plan"]["requested_dimensions"])
        self.assertTrue({"platform", "assortment", "business_cadence", "price", "promotion"} <= dimensions)


if __name__ == "__main__":
    unittest.main()
