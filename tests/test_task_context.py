from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from bot.router import route
from bot.session import (
    CLEAR,
    SET,
    BusinessTaskContext,
    SessionState,
    SlotUpdate,
    TaskContextPatch,
    reduce_task_context,
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


class TaskContextRouterTests(unittest.TestCase):
    def test_slot_reply_keeps_brand_period_and_adds_platform(self):
        state = SessionState()
        state.task_context = BusinessTaskContext(
            original_question="分析花知晓2026年4-6月的生意",
            brand="花知晓", period="2026年4-6月",
            intents=["EC_BUSINESS"], goals=["EC_BUSINESS"],
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


if __name__ == "__main__":
    unittest.main()
