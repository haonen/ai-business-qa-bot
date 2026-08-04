from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import pandas as pd

from bot.followup_formatter import format_followup_result
from bot.followup_plan import build_followup_plan, validate_plan
from bot.router import IntentResult, route
from bot.session import SessionState
from bot.skills.loader import load_meta_answers
from bot.tools.query_bet_followup_table import query_bet_followup_table
from bot.tools.query_change_contribution import query_change_contribution
from bot.tools.query_ec_followup_table import query_ec_followup_table
from bot.chains.followup_v2_chain import _ec_bundle_dimensions, _required_sources
from bot.app import _run_direct


class FollowupRouteV2Test(unittest.TestCase):
    def test_capability_questions_route_to_meta(self):
        for question in ("你会什么？", "你能干什么？", "你可以做什么？"):
            self.assertEqual(route(question, SessionState()).type, "meta")

    def test_capability_answer_covers_four_entry_types(self):
        content = load_meta_answers()
        for heading in ("EC生意分析", "BET媒体投资分析", "深入分析", "数据整理"):
            self.assertIn(heading, content)

    def test_capability_answer_has_two_clear_groups(self):
        content = load_meta_answers()
        self.assertLess(content.index("一、完整分析报告"), content.index("1. EC生意分析"))
        self.assertLess(content.index("1. EC生意分析"), content.index("2. BET媒体投资分析"))
        self.assertLess(content.index("二、继续追问"), content.index("3. 深入分析"))
        self.assertLess(content.index("3. 深入分析"), content.index("4. 数据整理"))

    def test_capability_answer_requires_clear_followup_prefix(self):
        content = load_meta_answers()
        self.assertIn("问题前加上“追问：”", content)
        self.assertNotIn("不需要输入“追问”", content)
        self.assertIn("- 追问：T2主要靠哪些品类？", content)

    @patch("bot.router.classify_user_intent")
    def test_narrow_media_request_uses_followup_skill(self, classify):
        classify.return_value = IntentResult(
            intent="media_analysis", brand="珀莱雅", brand_aliases=["珀莱雅", "PROYA"],
            period="2026年1-6月", media_scope="media_investment", confidence="high",
        )
        result = route("珀莱雅1—6月by month费比", SessionState())
        self.assertEqual(result.type, "skill_dispatch")
        self.assertEqual(result.brand, "珀莱雅")

    @patch("bot.router.classify_user_intent")
    def test_monthly_red_bkfs_request_uses_data_followup(self, classify):
        classify.return_value = IntentResult(
            intent="media_analysis", brand=None, period=None,
            media_scope="media_investment", confidence="high",
        )
        state = SessionState()
        state.bet_context.brand = "珀莱雅"
        state.bet_context.period = "2026年3月"
        result = route("按月看RED的BKFS花费结构。", state)
        self.assertEqual(result.type, "skill_dispatch")
        self.assertEqual(result.brand, "珀莱雅")
        self.assertEqual(result.period, "2026年3月")

    def test_followup_prefix_overrides_broad_media_route(self):
        state = SessionState()
        state.bet_context.brand = "珀莱雅"
        state.bet_context.period = "2026年3月"
        state.bet_context.brand_aliases = ["珀莱雅", "PROYA"]
        result = route("追问：按月看RED的BKFS花费结构。", state)
        self.assertEqual(result.type, "skill_dispatch")
        self.assertEqual(result.followup_text, "按月看RED的BKFS花费结构。")
        self.assertEqual(result.brand, "珀莱雅")
        self.assertEqual(result.period, "2026年3月")
        self.assertEqual(result.brand_aliases, ["珀莱雅", "PROYA"])

    @patch("bot.router.classify_user_intent")
    def test_broad_media_request_stays_full_report(self, classify):
        classify.return_value = IntentResult(
            intent="media_analysis", brand=None, period=None,
            media_scope="media_investment", confidence="high",
        )
        state = SessionState()
        state.ec_context.brand = "珀莱雅"
        state.ec_context.period = "2026年3月"
        state.drilldown_ctx.brand = "珀莱雅"
        state.drilldown_ctx.period = "2026年3月"
        result = route("那它媒体投资如何？", state)
        self.assertEqual(result.type, "media_analysis")

    def test_period_reply_resumes_pending_followup(self):
        state = SessionState()
        state.pending_request = {
            "intent": "followup_v2", "awaiting": "period", "brand": "珀莱雅",
            "brand_aliases": ["PROYA"], "followup_text": "按月整理媒体费比",
        }
        result = route("2026年1-6月", state)
        self.assertEqual(result.type, "skill_dispatch")
        self.assertEqual(result.followup_text, "按月整理媒体费比")

    @patch("bot.app.run_followup_v2_chain")
    @patch("bot.app.route")
    def test_skill_dispatch_executes_followup_v2_chain(self, route_mock, chain_mock):
        route_mock.return_value = type("Route", (), {
            "type": "skill_dispatch", "followup_text": "按月看RED的BKFS花费结构",
            "brand": "珀莱雅", "period": "2026年3月", "brand_aliases": ["珀莱雅", "PROYA"],
            "to_dict": lambda self: {"type": self.type},
        })()
        chain_mock.return_value = {
            "markdown": "| 月份 | BKFST |",
            "meta": {"domain": "bet", "brand": "珀莱雅", "period": "2026年3月"},
        }
        state = {
            "open_id": "test-user", "user_text": "追问：按月看RED的BKFS花费结构",
            "session": SessionState(),
        }
        result = _run_direct(state)
        chain_mock.assert_called_once()
        self.assertEqual(result["markdown"], "| 月份 | BKFST |")


class FollowupPlannerTest(unittest.TestCase):
    def setUp(self):
        self.old_key = os.environ.pop("DASHSCOPE_API_KEY", None)

    def tearDown(self):
        if self.old_key is not None:
            os.environ["DASHSCOPE_API_KEY"] = self.old_key

    def test_monthly_fee_ratio_plan(self):
        state = SessionState()
        state.bet_context.brand = "珀莱雅"
        state.bet_context.period = "2026年1-6月"
        plan = build_followup_plan("按月整理媒体费比", state)
        self.assertEqual(plan.skill, "data_organizer")
        self.assertEqual(plan.domain, "bet")
        self.assertEqual(plan.group_by, ["month"])
        self.assertEqual(plan.metrics, ["fee_ratio", "fee_ratio_change"])
        self.assertEqual(plan.period["start"], "2026-01-01")

    @patch("bot.followup_plan._llm_plan")
    def test_period_fee_ratio_accepts_period_as_time_grain(self, llm_plan):
        llm_plan.return_value = {
            "skill": "data_organizer", "domain": "bet", "mode": "period_summary",
            "brand": "韩束", "period": {"raw": "1-4月"}, "filters": {},
            "group_by": ["period"], "metrics": ["fee_ratio", "fee_ratio_change"],
        }
        plan = build_followup_plan("按1-4月整理出韩束的媒体费比", SessionState())
        self.assertEqual(plan.domain, "bet")
        self.assertEqual(plan.mode, "period_summary")
        self.assertEqual(plan.group_by, [])
        self.assertEqual(plan.metrics, ["fee_ratio", "fee_ratio_change"])

    @patch("bot.followup_plan._llm_plan")
    def test_invalid_llm_dimensions_fall_back_to_supported_rule_plan(self, llm_plan):
        llm_plan.return_value = {
            "skill": "data_organizer", "domain": "bet", "mode": "period_summary",
            "brand": "韩束", "period": {"raw": "1-4月"}, "filters": {},
            "group_by": ["unsupported_dimension"], "metrics": ["fee_ratio"],
        }
        plan = build_followup_plan(
            "按1-4月整理出韩束的媒体费比",
            SessionState(),
            brand="韩束",
            period="1-4月",
        )
        self.assertEqual(plan.domain, "bet")
        self.assertEqual(plan.group_by, [])
        self.assertEqual(plan.metrics, ["fee_ratio", "fee_ratio_change"])

    @patch("bot.followup_plan._llm_plan")
    def test_fee_ratio_metric_forces_bet_domain_and_never_requires_tmall(self, llm_plan):
        llm_plan.return_value = {
            "skill": "data_organizer", "domain": "ec", "mode": "monthly_trend",
            "brand": "韩束", "period": {"raw": "1-4月"}, "filters": {},
            "group_by": ["month"], "metrics": ["fee_ratio", "fee_ratio_change"],
        }
        plan = build_followup_plan("按月整理韩束1-4月的媒体费比", SessionState())
        self.assertEqual(plan.domain, "bet")
        self.assertEqual(plan.metrics, ["fee_ratio", "fee_ratio_change"])
        self.assertEqual(_required_sources(plan), ("topline", "nso"))
        self.assertNotIn("tmall", _required_sources(plan))

    def test_ec_driver_to_category_composition(self):
        state = SessionState()
        state.ec_context.brand = "珀莱雅"
        state.ec_context.period = "2026年7月1日到7月10日"
        plan = build_followup_plan("T2主要靠哪些品类", state)
        self.assertEqual(plan.domain, "ec")
        self.assertEqual(plan.filters["key_driver"], "T2")
        self.assertEqual(plan.group_by, ["category"])

    def test_ec_and_bet_context_are_independent(self):
        state = SessionState()
        state.ec_context.brand, state.ec_context.period = "珀莱雅", "2026年3月"
        state.bet_context.brand, state.bet_context.period = "韩束", "2026年4月"
        plan = build_followup_plan("按月整理媒体费比", state)
        self.assertEqual((plan.brand, plan.period["raw"]), ("韩束", "2026年4月"))

    def test_search_and_business_question_compiles_to_alignment(self):
        state = SessionState()
        state.ec_context.brand, state.ec_context.period = "珀莱雅", "2026年1-3月"
        plan = build_followup_plan("搜索涨了生意有没有涨", state)
        self.assertEqual((plan.domain, plan.mode), ("ec_bet", "trend_alignment"))


class FollowupToolTest(unittest.TestCase):
    def test_platform_fee_ratio_is_rejected_by_contract(self):
        state = SessionState()
        state.bet_context.brand, state.bet_context.period = "珀莱雅", "2026年3月"
        raw = {
            "skill": "data_organizer", "domain": "bet", "mode": "monthly_trend",
            "brand": "珀莱雅", "period": {"raw": "2026年3月"},
            "filters": {}, "group_by": ["month", "platform"],
            "metrics": ["fee_ratio"], "comparison": "yoy", "limit": 20,
        }
        with self.assertRaisesRegex(ValueError, "不计算交易平台费比"):
            validate_plan(raw, state)

    def test_category_performance_compiles_to_three_drill_tables(self):
        self.assertEqual(
            _ec_bundle_dimensions({"category": "面膜"}),
            ["key_driver", "series", "sku"],
        )

    @patch("bot.tools.query_ec_followup_table._raw_rows")
    @patch("bot.tools.query_ec_followup_table.ec_query_context")
    def test_ec_monthly_uses_real_prior_rows(self, context, raw_rows):
        context.return_value = {
            "source_brand": "PROYA", "input_brand": "珀莱雅",
            "current_start": "2026-03-01", "current_end": "2026-03-31",
            "prior_start": "2025-03-01", "prior_end": "2025-03-31",
        }
        raw_rows.return_value = pd.DataFrame([
            {"period_key": "current", "source_month": "2026-03", "category": "面膜", "key_driver": "T2", "sku": "1", "product_title": "A", "gmv": 120, "unit": 12, "row_count": 1},
            {"period_key": "prior", "source_month": "2025-03", "category": "面膜", "key_driver": "T2", "sku": "1", "product_title": "A", "gmv": 100, "unit": 10, "row_count": 1},
        ])
        result = query_ec_followup_table("珀莱雅", "2026年3月", ["month"], {}, ["gmv_actual", "gmv_evol"])
        self.assertEqual(result["rows"][0]["gmv_prior"], 100)
        self.assertEqual(result["rows"][0]["gmv_evol"], 0.2)

    @patch("bot.tools.query_bet_followup_table.fetch_df")
    def test_monthly_fee_ratio_uses_ttl_nso_and_pp_change(self, fetch):
        media = pd.DataFrame([
            {"period_key": "current", "period_month": "2026-03-01", "ait": "Transaction", "media": "JD", "submedia": "", "bkfs_overall": "T", "bkfs_xiaohongshu": None, "bkfs_douyin": None, "spend": 100, "row_count": 1},
            {"period_key": "prior", "period_month": "2025-03-01", "ait": "Transaction", "media": "JD", "submedia": "", "bkfs_overall": "T", "bkfs_xiaohongshu": None, "bkfs_douyin": None, "spend": 80, "row_count": 1},
        ])
        nso = pd.DataFrame([
            {"period_key": "current", "year": 2026, "month": 3, "nso": 200, "row_count": 1},
            {"period_key": "prior", "year": 2025, "month": 3, "nso": 160, "row_count": 1},
        ])
        fetch.side_effect = [media, nso]
        result = query_bet_followup_table(
            "珀莱雅", "2026年3月", ["month"], {},
            ["spend_actual", "fee_ratio", "fee_ratio_change"],
            source_brands={"topline": "PROYA", "nso": "PROYA"},
        )
        self.assertEqual(result["rows"][0]["fee_ratio"], 0.5)
        self.assertEqual(result["rows"][0]["fee_ratio_change"], 0.0)

    @patch("bot.tools.query_change_contribution.query_ec_followup_table")
    def test_change_contribution_separates_growth_and_drag(self, query):
        query.return_value = {"rows": [
            {"category": "A", "gmv_actual": 130, "gmv_prior": 100, "_prior_rows": 1},
            {"category": "B", "gmv_actual": 60, "gmv_prior": 100, "_prior_rows": 1},
        ], "totals": {}, "evidence": []}
        result = query_change_contribution("ec", "品牌", "2026年3月", "category")
        self.assertEqual(result["rows"][0]["decline_drag"], 1.0)
        self.assertEqual(result["rows"][1]["growth_contribution"], 1.0)


class FollowupFormatterTest(unittest.TestCase):
    def test_data_organizer_stays_table_only_and_inline(self):
        plan = {"skill": "data_organizer", "domain": "bet", "mode": "monthly_trend", "brand": "珀莱雅", "period": {"raw": "2026年1-2月"}, "filters": {}, "group_by": ["month"], "metrics": ["fee_ratio", "fee_ratio_change"]}
        result = {"rows": [{"month": "2026-01", "fee_ratio": .2, "fee_ratio_change": .01}], "evidence": []}
        rendered = format_followup_result(plan, result)
        self.assertNotIn("- ", rendered["markdown"])
        self.assertFalse(rendered["meta"]["document_ready"])

    def test_long_table_requests_document(self):
        plan = {"skill": "data_organizer", "domain": "ec", "mode": "ranking", "brand": "珀莱雅", "period": {"raw": "2026年3月"}, "filters": {"category": "面膜"}, "group_by": ["sku"], "metrics": ["gmv_actual"]}
        rows = [{"sku": str(i), "gmv_actual": i} for i in range(21)]
        rendered = format_followup_result(plan, {"rows": rows, "evidence": []})
        self.assertTrue(rendered["meta"]["document_ready"])

    def test_analysis_drill_bundle_requests_document_and_synthesizes_tables(self):
        plan = {
            "skill": "analysis_drill", "domain": "ec", "mode": "performance",
            "brand": "谷雨", "period": {"raw": "2026年7月1日到7月10日"},
            "filters": {"category": "美容护肤/美体/精油-面部精华（新）-次抛精华"},
            "group_by": [], "metrics": ["gmv_actual", "gmv_evol"],
        }
        result = {
            "rows": [{"gmv_actual": 1_330_000, "gmv_evol": 10.157}],
            "tables": [
                {
                    "title": "Key Driver结构", "metrics": ["gmv_actual", "gmv_evol"],
                    "rows": [
                        {"key_driver": "NON-KOL", "gmv_actual": 1_300_000, "gmv_evol": 10.322},
                        {"key_driver": "T2", "gmv_actual": 30_000, "gmv_evol": 5.862},
                    ],
                },
                {
                    "title": "系列结构", "metrics": ["gmv_actual", "gmv_evol"],
                    "rows": [
                        {"series": "黑金针", "gmv_actual": 552_300, "gmv_evol": 125.265},
                        {"series": "细胞沁素", "gmv_actual": 392_500, "gmv_evol": None},
                        {"series": "其他系列", "gmv_actual": 350_600, "gmv_evol": 2.069},
                    ],
                },
                {
                    "title": "Top链接", "metrics": ["gmv_actual", "gmv_evol"],
                    "rows": [{"sku": "SKU-1", "gmv_actual": 500_000, "gmv_evol": 1.0}],
                },
            ],
            "evidence": [],
        }
        rendered = format_followup_result(plan, result)
        self.assertTrue(rendered["meta"]["document_ready"])
        self.assertIn("次抛精华的生意主要由NON-KOL带来", rendered["markdown"])
        self.assertIn("占该品类GMV的97.7%", rendered["markdown"])
        self.assertIn("黑金针和细胞沁素合计贡献944.8K", rendered["markdown"])
        self.assertNotIn("NON-KOL的GMV Actual最高", rendered["markdown"])


if __name__ == "__main__":
    unittest.main()
