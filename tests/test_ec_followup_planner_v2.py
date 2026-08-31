from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from bot.agent_plan import compile_agent_plan
from bot.followup_plan import build_followup_plan
from bot.chains.followup_v2_chain import run_followup_v2_chain
from bot.router import RouteResult, route
from bot.session import SessionState
from bot.executable_plan import execute_agent_plan


def _state(*, complete_category_evidence: bool = False) -> SessionState:
    state = SessionState()
    state.drilldown_ctx.brand = "花知晓"
    state.drilldown_ctx.period = "2026年4-6月"
    state.ec_context.brand = "珀莱雅"
    state.ec_context.period = "2026年6月1日到7月19日"
    state.ec_context.platform = "DY"
    evidence = {
        "schema_version": "ec-report-evidence.v2", "brand": "珀莱雅",
        "period": {"raw": "2026年6月1日到7月19日"}, "platform": "DY",
        "categories": [{
            "category": "防晒霜", "gmv_current": 12000000, "gmv_prior": 8000000,
            "gmv_change": 4000000, "evol": .5, "weight": .12,
        }],
        "selected_category": "防晒霜", "key_drivers": [],
        "series_scopes": {"防晒霜": [{"series": "羽感防晒", "gmv_current": 7000000, "evol": .4}]}
        if complete_category_evidence else {},
        "product_scopes": {"防晒霜": [{"sku": "1｜羽感防晒霜", "gmv_current": 5000000, "evol": .3}]}
        if complete_category_evidence else {},
    }
    state.ec_context.report_cache = {"ec_report_evidence": evidence}
    state.last_result_cache = state.ec_context.report_cache
    return state


class ECFollowupPlannerV2Test(unittest.TestCase):
    def setUp(self):
        self.old_key = os.environ.pop("DASHSCOPE_API_KEY", None)

    def tearDown(self):
        if self.old_key is not None:
            os.environ["DASHSCOPE_API_KEY"] = self.old_key

    def test_natural_category_followup_routes_to_skill_and_latest_ec_context(self):
        state = _state()
        with patch.dict(os.environ, {
            "ROUTER_V2_ENABLED": "0", "ENTITY_RESOLVER_V2_ENABLED": "0",
            "FOLLOWUP_SKILL_V2_ENABLED": "1",
        }, clear=False):
            result = route("防晒霜涨得不错，再帮我分析一下这个品类", state)
        self.assertEqual(result.type, "skill_dispatch")
        self.assertEqual(result.brand, "珀莱雅")
        self.assertEqual(result.period, "2026年6月1日到7月19日")

    def test_category_pronoun_inherits_latest_report_selection(self):
        plan = build_followup_plan("继续分析这个品类", _state())
        self.assertEqual(plan.brand, "珀莱雅")
        self.assertEqual(plan.platform, "DY")
        self.assertEqual(plan.filters["category"], "防晒霜")

    def test_douyin_category_drill_never_calls_tmall_tool(self):
        state = _state()
        dy_result = {
            "query_meta": {"domain": "ec", "platform": "DY", "table": "ai_bot_dy_product_link"},
            "filters": {"category": "防晒霜"},
            "totals": {"gmv_actual": 12000000, "gmv_evol": .5},
            "rows": [{"_total": "DY", "gmv_actual": 12000000, "gmv_evol": .5}],
            "tables": [
                {"title": "Key Driver结构", "rows": [{"key_driver": "Store live", "gmv_actual": 8000000}]},
                {"title": "系列结构", "rows": [{"series": "羽感防晒", "gmv_actual": 7000000}]},
                {"title": "Top商品标题", "rows": [{"sku": "1｜羽感防晒霜", "gmv_actual": 5000000}]},
            ],
            "evidence": [], "missing": [],
        }
        with patch.dict(os.environ, {"DOUYIN_FOLLOWUP_TOOL_ENABLED": "1"}, clear=False), \
             patch("bot.chains.followup_v2_chain.query_douyin_drill_bundle", return_value=dy_result) as dy, \
             patch("bot.chains.followup_v2_chain.query_ec_followup_table") as tmall:
            result = run_followup_v2_chain(
                "防晒霜涨得不错，再帮我分析一下这个品类", state,
            )
        dy.assert_called_once()
        tmall.assert_not_called()
        self.assertEqual(result["meta"]["platform"], "DY")
        self.assertIn("羽感防晒", result["markdown"])

    def test_complete_report_evidence_answers_without_database(self):
        state = _state(complete_category_evidence=True)
        with patch("bot.chains.followup_v2_chain.query_douyin_drill_bundle") as dy, \
             patch("bot.chains.followup_v2_chain.query_ec_followup_table") as tmall:
            result = run_followup_v2_chain("防晒霜涨得不错，再帮我分析这个品类", state)
        dy.assert_not_called()
        tmall.assert_not_called()
        self.assertTrue(result["meta"]["evidence_reused"])
        self.assertIn("防晒霜", result["markdown"])

    def test_skill_dispatch_plan_has_context_derive_coverage_query_and_synthesis(self):
        plan = compile_agent_plan(
            RouteResult(type="skill_dispatch", brand="珀莱雅", period="2026年6月1日到7月19日"),
            "防晒霜涨得不错，再帮我分析这个品类",
        )
        self.assertEqual(plan.version, "executable-followup-v2")
        self.assertEqual(
            [step.step_id for step in plan.steps],
            ["context", "resolve_subject", "coverage", "query_missing", "synthesize"],
        )
        self.assertEqual(plan.steps[3].condition_ref, "coverage.requires_query")

    def test_executable_followup_skips_query_when_report_evidence_is_complete(self):
        state = _state(complete_category_evidence=True)
        plan = compile_agent_plan(
            RouteResult(type="skill_dispatch", brand="珀莱雅", period="2026年6月1日到7月19日"),
            "防晒霜涨得不错，再帮我分析这个品类",
        )
        with patch("bot.chains.followup_v2_chain.query_douyin_drill_bundle") as dy:
            result = execute_agent_plan(plan, session=state)
        dy.assert_not_called()
        statuses = {
            row["step_id"]: row["status"] for row in result["meta"]["plan_execution"]["steps"]
        }
        self.assertEqual(statuses["query_missing"], "skipped")
        self.assertIn("防晒霜", result["markdown"])


if __name__ == "__main__":
    unittest.main()
