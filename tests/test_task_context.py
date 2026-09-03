from __future__ import annotations

import os
import unittest
from uuid import uuid4
from unittest.mock import patch

from bot.router import route
from bot.session import (
    CLEAR,
    SET,
    BusinessTaskContext,
    SessionState,
    SlotUpdate,
    TaskContextPatch,
    get_session,
    reduce_task_context,
    set_pending_request,
)


class TaskContextReducerTests(unittest.TestCase):
    def test_absent_slots_are_kept(self):
        before = BusinessTaskContext(
            brand="花知晓", period="2026年4-6月", platform=None,
            intents=["EC_BUSINESS"], goals=["EC_BUSINESS"],
        )
        after = reduce_task_context(before, TaskContextPatch(
            relation="SUPPLY_MISSING_SLOT",
            slots={"platform": SlotUpdate(SET, "TTL")},
        ))
        self.assertEqual((after.brand, after.period, after.platform), (
            "花知晓", "2026年4-6月", "TTL",
        ))

    def test_clear_is_different_from_absent(self):
        before = BusinessTaskContext(platform="TTL", media_mode="OVERALL_BET")
        after = reduce_task_context(before, TaskContextPatch(
            slots={"media_mode": SlotUpdate(CLEAR)},
        ))
        self.assertEqual(after.platform, "TTL")
        self.assertIsNone(after.media_mode)

    def test_new_task_does_not_inherit_old_goals(self):
        before = BusinessTaskContext(
            brand="旧品牌", period="2026年1月", platform="TM",
            intents=["EC_BUSINESS", "BET"], goals=["EC_BUSINESS", "BET"],
        )
        after = reduce_task_context(before, TaskContextPatch(
            relation="NEW_TASK",
            slots={"brand": SlotUpdate(SET, "花知晓")},
            add_intents=["EC_BUSINESS"], add_goals=["EC_BUSINESS"],
        ))
        self.assertEqual(after.brand, "花知晓")
        self.assertIsNone(after.period)
        self.assertEqual(after.goals, ["EC_BUSINESS"])
        self.assertNotEqual(after.task_id, before.task_id)

    def test_goals_can_be_removed(self):
        before = BusinessTaskContext(
            intents=["EC_BUSINESS", "BET"], goals=["EC_BUSINESS", "BET"],
        )
        after = reduce_task_context(before, TaskContextPatch(
            remove_intents=["BET"], remove_goals=["BET"],
        ))
        self.assertEqual(after.intents, ["EC_BUSINESS"])
        self.assertEqual(after.goals, ["EC_BUSINESS"])

    def test_structured_time_contract_survives_slot_merge(self):
        before = BusinessTaskContext(
            brand="欧莱雅", awaiting_slot="platform",
            time_scope={"status": "exact"},
            comparison_spec={"mode": "YOY_ALIGNED"},
        )
        after = reduce_task_context(before, TaskContextPatch(
            relation="SUPPLY_MISSING_SLOT",
            slots={"platform": SlotUpdate(SET, "TM")},
        ))
        self.assertEqual(after.time_scope, {"status": "exact"})
        self.assertEqual(after.comparison_spec, {"mode": "YOY_ALIGNED"})

    def test_time_role_pending_request_is_persisted_as_an_awaiting_slot(self):
        open_id = f"time-role-{uuid4().hex}"
        with patch.dict(os.environ, {
            "BOT_QUEUE_ENABLED": "0", "BOT_SESSION_BACKEND": "memory",
        }, clear=False):
            set_pending_request(open_id, {
                "intent": "v2_time_roles",
                "original_text": "欧莱雅天猫2026年7月和2025年7月的生意",
                "brand": "欧莱雅", "platform": "TM",
                "time_scope": {"status": "needs_clarification"},
            })
            state = get_session(open_id)
        self.assertEqual(state.pending_request["intent"], "v2_time_roles")
        self.assertEqual(state.task_context.awaiting_slot, "time_roles")
        self.assertEqual(state.task_context.status, "awaiting")


class TaskContextRouterTests(unittest.TestCase):
    def test_revised_period_resumes_failed_tmall_request_without_forgetting_platform(self):
        state = SessionState()
        state.pending_request = {
            "intent": "v2_period",
            "original_text": "欧莱雅旗舰店 天猫 2026年7月的生意vs 2025年7月的生意",
            "brand": "欧莱雅", "brand_aliases": ["欧莱雅"], "platform": "TM",
            "intents": ["EC_BUSINESS"],
        }
        state.task_context = BusinessTaskContext(
            original_question=state.pending_request["original_text"],
            brand="欧莱雅", brand_aliases=["欧莱雅"], platform="TM",
            intents=["EC_BUSINESS"], goals=["EC_BUSINESS"],
            awaiting_slot="period", status="awaiting",
        )
        with patch.dict(os.environ, {
            "TASK_CONTEXT_V2_ENABLED": "1", "TASK_CONTEXT_LLM_ENABLED": "0",
            "ROUTER_V2_ENABLED": "1", "ENTITY_RESOLVER_V2_ENABLED": "1",
        }, clear=False):
            result = route("就用2026年7月1日到7月19日就可以", state)
        self.assertEqual(result.type, "default_chain")
        self.assertEqual((result.brand, result.platform), ("欧莱雅", "TM"))
        self.assertEqual(result.period, "2026年7月1日至7月19日")
        spec = result.comparison_spec
        self.assertEqual(spec["analysis_periods"][0]["end_date"], "2026-07-19")
        self.assertEqual(spec["baseline_periods"][0]["end_date"], "2025-07-19")
        self.assertEqual(result.business_spec["platforms"], ["TM"])
        self.assertEqual(
            result.business_spec["comparison_spec"]["analysis_periods"][0]["end_date"],
            "2026-07-19",
        )

    def test_slot_reply_keeps_brand_period_and_adds_platform(self):
        state = SessionState()
        state.task_context = BusinessTaskContext(
            original_question="分析花知晓2026年4-6月的生意",
            brand="花知晓", period="2026年4-6月",
            intents=["EC_BUSINESS"], goals=["EC_BUSINESS"],
            business_spec={
                "subject_scope": "brand", "brand": "花知晓",
                "platforms": [], "platform_mode": "single",
                "category": "TOTAL BEAUTY",
            },
            awaiting_slot="platform", status="awaiting",
        )
        with patch.dict(os.environ, {
            "TASK_CONTEXT_V2_ENABLED": "1", "TASK_CONTEXT_LLM_ENABLED": "0",
            "ROUTER_V2_ENABLED": "1",
        }, clear=False):
            result = route("三平台", state)
        self.assertEqual(result.type, "three_platform_competitor_analysis")
        self.assertEqual((result.brand, result.period, result.platform), (
            "花知晓", "2026年4-6月", "TTL",
        ))
        self.assertEqual(result.business_spec["platforms"], ["TM", "DY", "JD"])
        self.assertEqual(result.business_spec["platform_mode"], "combined")
        trace = result.route_decision["task_context_trace"]
        self.assertEqual(trace["explicit_patch"]["platform"], "TTL")

    def test_conflicting_brand_is_not_merged_into_old_task(self):
        state = SessionState()
        state.task_context = BusinessTaskContext(
            brand="花知晓", period="2026年4-6月",
            intents=["EC_BUSINESS"], goals=["EC_BUSINESS"],
            awaiting_slot="platform", status="awaiting",
        )
        with patch.dict(os.environ, {
            "TASK_CONTEXT_V2_ENABLED": "1", "TASK_CONTEXT_LLM_ENABLED": "0",
            "ROUTER_V2_ENABLED": "1",
        }, clear=False):
            result = route("珀莱雅2026年6月天猫生意", state)
        self.assertEqual(result.brand, "珀莱雅")
        self.assertEqual(result.period, "2026年6月")
        self.assertEqual(result.platform, "TM")
        self.assertEqual(
            result.route_decision["task_context_patch"]["relation"], "NEW_TASK",
        )

    def test_brand_alias_does_not_start_a_new_task(self):
        state = SessionState()
        state.task_context = BusinessTaskContext(
            original_question="分析PROYA的生意",
            brand="PROYA", brand_aliases=["PROYA", "珀莱雅"],
            period="2026年6月", intents=["EC_BUSINESS"], goals=["EC_BUSINESS"],
            awaiting_slot="platform", status="awaiting",
        )
        with patch.dict(os.environ, {
            "TASK_CONTEXT_V2_ENABLED": "1", "TASK_CONTEXT_LLM_ENABLED": "0",
            "ROUTER_V2_ENABLED": "1",
        }, clear=False):
            result = route("珀莱雅天猫", state)
        self.assertEqual(result.type, "default_chain")
        self.assertEqual(result.route_decision["decision_source"], "active_task_merge")
        self.assertEqual((result.period, result.platform), ("2026年6月", "TM"))


if __name__ == "__main__":
    unittest.main()
