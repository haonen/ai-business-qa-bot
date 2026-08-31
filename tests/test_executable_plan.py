from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from bot.agent_plan import compile_agent_plan
from bot.derive_ops import run_derive
from bot.executable_plan import execute_agent_plan
from bot.feishu_doc import _TableSpec, markdown_to_items
from bot.messaging import split_markdown_cards
from bot.router import RouteResult, route
from bot.router_v2 import build_route_decision
from bot.session import ActivePlanState, SessionState


class DeriveOperatorTest(unittest.TestCase):
    def test_argmax_selects_each_brand_independently_and_ignores_missing(self):
        result = run_derive(
            "per_group_argmax",
            rows=[
                {"brand": "A", "platform": "TM", "gmv_growth": 10, "gmv_actual": 100},
                {"brand": "A", "platform": "DY", "gmv_growth": 30, "gmv_actual": 80},
                {"brand": "B", "platform": "TM", "gmv_growth": None, "gmv_actual": None},
                {"brand": "B", "platform": "JD", "gmv_growth": 5, "gmv_actual": 20},
            ],
            group_by="brand", rank_by="gmv_growth", supported_values=["TM", "DY", "JD"],
            expected_groups=["A", "B", "C"],
        )
        self.assertEqual(
            [(row["brand"], row["platform"]) for row in result["brand_platform_pairs"]],
            [("A", "DY"), ("B", "JD")],
        )
        self.assertEqual(result["missing_groups"], ["C"])

    def test_argmax_marks_least_decline_and_tie(self):
        result = run_derive(
            "per_group_argmax",
            rows=[
                {"brand": "A", "platform": "TM", "gmv_growth": -10, "gmv_actual": 100},
                {"brand": "A", "platform": "DY", "gmv_growth": -10, "gmv_actual": 80},
            ],
            group_by="brand", rank_by="gmv_growth", supported_values=["TM", "DY"],
        )
        selected = result["brand_platform_pairs"][0]
        self.assertEqual(selected["platform"], "TM")
        self.assertTrue(selected["all_negative"])
        self.assertTrue(selected["tie"])


class FeishuCardChunkingTest(unittest.TestCase):
    def test_long_report_is_split_without_breaking_table_blocks(self):
        table = "| 平台 | GMV |\n|---|---:|\n| 天猫 | 10 |"
        report = "\n\n".join(f"## 第{index}部分\n\n{table}" for index in range(1, 8))
        with patch.dict(os.environ, {"FEISHU_CARD_MAX_TABLES": "3"}, clear=False):
            chunks = split_markdown_cards(report)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(chunk.count("|---|---:|") <= 3 for chunk in chunks))
        self.assertEqual(sum(chunk.count("|---|---:|") for chunk in chunks), 7)


class ExecutableRankThenDrillTest(unittest.TestCase):
    def _plan(self):
        return compile_agent_plan(RouteResult(
            type="market_brand_deep_dive", period="2026年6月", platform="TTL",
            segment="PURE MASS", category="TOTAL BEAUTY", ranking_metric="gmv_actual",
            ranking_limit=3,
        ), "2026年6月Pure Mass TTL Beauty Top3，选增长最好的平台下钻")

    @patch("bot.executable_plan.run_jd_business_chain")
    @patch("bot.executable_plan.run_douyin_business_chain")
    @patch("bot.executable_plan.run_default_chain")
    @patch("bot.executable_plan.query_market_top_brands")
    def test_plan_executes_compare_then_different_platform_templates(
        self, query_ranking, run_tmall, run_douyin, run_jd,
    ):
        query_ranking.return_value = {
            "query_meta": {"tool": "query_market_top_brands"},
            "rows": [
                {"rank": 1, "brand": "A", "gmv_actual": 100, "gmv_prior": 80, "gmv_growth": 20, "evol": .25},
                {"rank": 2, "brand": "B", "gmv_actual": 90, "gmv_prior": 70, "gmv_growth": 20, "evol": .286},
                {"rank": 3, "brand": "C", "gmv_actual": 80, "gmv_prior": 60, "gmv_growth": 20, "evol": .333},
            ],
            "coverage": [
                {"period_key": period, "month": month, "platform": platform, "brand": brand, "gmv": value}
                for brand, platform, current, prior in (
                    ("A", "DY", 60, 20), ("A", "TM", 40, 60),
                    ("B", "JD", 70, 30), ("B", "TM", 20, 40),
                    ("C", "TM", 70, 40), ("C", "DY", 10, 20),
                )
                for period, month, value in (("current", "2026-06", current), ("prior", "2025-06", prior))
            ],
        }
        run_tmall.return_value = {"ok": True, "markdown": "C TM detail", "meta": {}}
        run_douyin.return_value = {"ok": True, "markdown": "A DY detail", "meta": {}}
        run_jd.return_value = {"ok": True, "markdown": "B JD detail", "meta": {}}

        result = execute_agent_plan(self._plan())

        run_douyin.assert_called_once_with("A", "2026年6月", brand_aliases=["A"])
        run_jd.assert_called_once_with("B", "2026年6月", brand_aliases=["B"])
        run_tmall.assert_called_once_with("C", "2026年6月", brand_aliases=["C"])
        self.assertIn("A DY detail", result["markdown"])
        self.assertIn("B JD detail", result["markdown"])
        self.assertIn("C TM detail", result["markdown"])
        self.assertEqual(result["meta"]["plan_execution"]["status"], "success")
        self.assertEqual(
            [row["platform"] for row in result["meta"]["selected_platform_by_brand"]],
            ["DY", "JD", "TM"],
        )
        native_tables = [
            item for item in markdown_to_items(result["markdown"])
            if isinstance(item, _TableSpec)
        ]
        self.assertEqual(len(native_tables), 4)
        self.assertEqual(native_tables[0].headers[:2], ["排名", "品牌"])

    def test_plan_contract_contains_explicit_derive_step(self):
        plan = self._plan()
        derive = next(step for step in plan.steps if step.kind == "DERIVE")
        self.assertEqual(derive.capability_id, "derive.per_group_argmax")
        self.assertEqual(derive.inputs["input_ref"], "platform_matrix.rows")

    @patch("bot.executable_plan.run_douyin_business_chain")
    @patch("bot.executable_plan.query_three_platform_competitor")
    def test_single_brand_plan_compares_then_drills_without_losing_entities(
        self, query_three_platform, run_douyin,
    ):
        query_three_platform.return_value = {
            "brand": "dirovo", "period": "2026年4-6月", "period_meta": {},
            "is_pure_mass": False,
            "overall": [
                {"platform": "TTL", "gmv_current": 250, "gmv_prior": 210, "evol": .19},
                {"platform": "TM", "gmv_current": 100, "gmv_prior": 90, "evol": .11},
                {"platform": "DY", "gmv_current": 100, "gmv_prior": 60, "evol": .667},
                {"platform": "JD", "gmv_current": 50, "gmv_prior": 60, "evol": -.167},
            ],
            "categories": {"TM": [], "DY": [], "JD": []}, "channels": [],
        }
        run_douyin.return_value = {"ok": True, "markdown": "DY deep detail", "meta": {}}
        plan = compile_agent_plan(RouteResult(
            type="brand_platform_deep_dive", brand="dirovo", period="2026年4-6月",
            platform="TTL",
        ), "三平台看增长最多的平台再下钻")

        result = execute_agent_plan(plan)

        query_three_platform.assert_called_once_with("dirovo", "2026年4-6月")
        run_douyin.assert_called_once_with("dirovo", "2026年4-6月", brand_aliases=["dirovo"])
        self.assertIn("DY deep detail", result["markdown"])
        self.assertEqual(result["meta"]["selected_platform_by_brand"][0]["platform"], "DY")

    def test_comparison_wording_selects_registered_metric(self):
        route_result = RouteResult(
            type="market_brand_deep_dive", period="2026年6月", platform="TTL",
            segment="PURE MASS", category="TOTAL BEAUTY", ranking_limit=3,
        )
        cases = {
            "找每个品牌增速最高的平台再下钻": "evol",
            "找每个品牌生意最好的平台再下钻": "gmv_actual",
            "找每个品牌增长贡献最大的平台再下钻": "growth_contribution",
            "找每个品牌涨得最好的平台再下钻": "gmv_growth",
        }
        for text, metric in cases.items():
            with self.subTest(text=text):
                plan = compile_agent_plan(route_result, text)
                derive = next(step for step in plan.steps if step.kind == "DERIVE")
                self.assertEqual(derive.inputs["rank_by"], metric)


class ActivePlanFollowupTest(unittest.TestCase):
    @patch("bot.router.classify_user_intent", return_value=None)
    def test_ordinal_three_platform_followup_keeps_plan_brand_and_period(self, _mock):
        state = SessionState(active_plan=ActivePlanState(
            top_brands=["A", "B", "C"], entities={"period": "2026年Q2"},
            selected_platforms=[{"brand": "B", "platform": "JD"}],
        ))
        result = route("第二名品牌三平台分析呢？", state)
        self.assertEqual(result.type, "three_platform_competitor_analysis")
        self.assertEqual(result.brand, "B")
        self.assertEqual(result.period, "2026年Q2")

    @patch("bot.router.classify_user_intent", return_value=None)
    def test_ordinal_selected_platform_followup_uses_derived_platform(self, _mock):
        state = SessionState(active_plan=ActivePlanState(
            top_brands=["A", "B", "C"], entities={"period": "2026年Q2"},
            selected_platforms=[{"brand": "B", "platform": "JD"}],
        ))
        result = route("第二名增长最好的平台再往下看", state)
        self.assertEqual(result.type, "jd_business_analysis")
        self.assertEqual(result.brand, "B")


class ExecutablePlanAppIntegrationTest(unittest.TestCase):
    def test_complete_v2_market_plan_auto_executes_when_enabled(self):
        with patch.dict(os.environ, {"EXECUTABLE_PLAN_V2_ENABLED": "1"}, clear=False):
            decision = build_route_decision(
                "2026年Q2 Pure Mass TTL Beauty Top3品牌，比较三平台后下钻",
                "2026年Q2",
            )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "market_brand_deep_dive")
        self.assertFalse(decision.requires_confirmation)

    def test_enabled_executor_bypasses_v1_direct_runner(self):
        from bot.app import run_agent

        route_result = RouteResult(
            type="market_brand_deep_dive", original_text="complex market question",
            period="2026年6月", platform="TTL", segment="PURE MASS",
            category="TOTAL BEAUTY", ranking_metric="gmv_actual", ranking_limit=3,
        )
        executed = {
            "ok": True, "markdown": "executable report", "meta": {
                "top_brands": ["A", "B", "C"],
                "selected_platform_by_brand": [{"brand": "A", "platform": "DY"}],
                "market_result": {"rows": []},
                "plan_execution": {"status": "success", "steps": []},
            },
        }
        with patch.dict(os.environ, {
            "EXECUTABLE_PLAN_V2_ENABLED": "1",
            "EXECUTABLE_PLAN_DERIVE_ENABLED": "1",
            "EXECUTABLE_PLAN_V2_SHADOW": "0",
        }, clear=False), patch("bot.app.route", return_value=route_result), \
             patch("bot.app.execute_agent_plan", return_value=executed) as execute, \
             patch("bot.app._run_direct") as direct, \
             patch("bot.app.update_active_plan"), patch("bot.app.update_market_context"):
            result = run_agent("u-plan", "complex market question", SessionState())
        execute.assert_called_once()
        direct.assert_not_called()
        self.assertEqual(result["markdown"], "executable report")
        self.assertEqual(result["meta"]["agent_plan"]["version"], "executable-plan-v2")


if __name__ == "__main__":
    unittest.main()
