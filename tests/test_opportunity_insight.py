from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from bot.chains.followup_v2_chain import run_followup_v2_chain
from bot.opportunity_insight import (
    asks_for_opportunity,
    generate_opportunity_bullets,
    select_opportunity_candidates,
)
from bot.router import route
from bot.session import SessionState


def _fake_llm(responses: list[str]):
    """A fake OpenAI-style client whose .chat.completions.create() returns
    each response in `responses` in order, one per call."""
    calls = {"n": 0}

    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeChoice:
        def __init__(self, content):
            self.message = FakeMessage(content)

    class FakeResponse:
        def __init__(self, content):
            self.choices = [FakeChoice(content)]

    def create(**kwargs):
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return FakeResponse(responses[index])

    client = MagicMock()
    client.chat.completions.create.side_effect = create
    return client, calls


class AsksForOpportunityTest(unittest.TestCase):
    def test_recognizes_opportunity_phrasing(self):
        for text in (
            "分析欧莱雅生意表现，找出增长机会点行动方案",
            "有什么增长机会点",
            "给我一些增长的行动方案建议",
            "还能怎么做",
        ):
            with self.subTest(text=text):
                self.assertTrue(asks_for_opportunity(text))

    def test_plain_lookup_is_not_opportunity(self):
        self.assertFalse(asks_for_opportunity("欧莱雅8月20日在天猫的生意是多少"))


class SelectOpportunityCandidatesTest(unittest.TestCase):
    def test_filters_out_noise_and_shrinking_rows(self):
        evidence = {
            "categories": [
                # Large and growing: qualifies.
                {"category_cn": "护肤-面霜", "gmv_current": 5_000_000, "gmv_prior": 4_000_000, "evol": 0.25},
                # Large but shrinking share and negative evol: must not qualify.
                {"category_cn": "护肤-精华", "gmv_current": 3_000_000, "gmv_prior": 3_400_000, "evol": -0.12},
                # Tiny share even though evol is huge: noise floor should drop it.
                {"category_cn": "彩妆-口红", "gmv_current": 1_000, "gmv_prior": 100, "evol": 9.0},
            ],
        }
        candidates = select_opportunity_candidates(evidence)
        names = {candidate["name"] for candidate in candidates}
        self.assertIn("护肤-面霜", names)
        self.assertNotIn("护肤-精华", names)
        self.assertNotIn("彩妆-口红", names)

    def test_empty_evidence_returns_no_candidates(self):
        self.assertEqual(select_opportunity_candidates({}), [])

    def test_candidates_are_ranked_by_share_delta_then_evol(self):
        evidence = {
            "categories": [
                {"category_cn": "A", "gmv_current": 100, "gmv_prior": 90, "evol": 0.5},
                {"category_cn": "B", "gmv_current": 400, "gmv_prior": 300, "evol": 0.4},
            ],
        }
        candidates = select_opportunity_candidates(evidence)
        # B has the larger share_delta once normalized against the combined
        # category total, even though A has the higher evol.
        self.assertEqual(candidates[0]["name"], "B")


class GenerateOpportunityBulletsTest(unittest.TestCase):
    def setUp(self):
        self.candidates = [
            {
                "candidate_id": "cand_1", "dimension": "category", "name": "护肤-面霜",
                "gmv_current": 5_000_000.0, "share": 0.5, "share_delta": 0.05, "evol": 0.25,
            },
        ]

    def test_without_api_key_uses_deterministic_fallback(self):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}, clear=False):
            bullets, used_llm = generate_opportunity_bullets(
                "找增长机会点", self.candidates, brand="欧莱雅", period="2026年8月",
            )
        self.assertFalse(used_llm)
        self.assertEqual(len(bullets), 1)
        self.assertIn("护肤-面霜", bullets[0])
        self.assertIn("50.0%", bullets[0])

    def test_causal_wording_is_rejected_and_retried(self):
        client, calls = _fake_llm([
            '{"picks": [{"candidate_id": "cand_1", "reason": "该品类同比大涨，证明了打法有效"}]}',
            '{"picks": [{"candidate_id": "cand_1", "reason": "份额持续走高，可以考虑优先追加资源"}]}',
        ])
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "fake"}, clear=False), \
             patch("bot.opportunity_insight.llm_client", return_value=client):
            bullets, used_llm = generate_opportunity_bullets(
                "找增长机会点", self.candidates, brand="欧莱雅", period="2026年8月",
            )
        self.assertEqual(calls["n"], 2)
        self.assertTrue(used_llm)
        self.assertIn("可以考虑优先追加资源", bullets[0])
        self.assertNotIn("证明了", bullets[0])

    def test_reason_containing_a_digit_is_rejected_and_retried(self):
        client, calls = _fake_llm([
            '{"picks": [{"candidate_id": "cand_1", "reason": "建议追加50%预算"}]}',
            '{"picks": [{"candidate_id": "cand_1", "reason": "值得关注并考虑追加预算"}]}',
        ])
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "fake"}, clear=False), \
             patch("bot.opportunity_insight.llm_client", return_value=client):
            bullets, used_llm = generate_opportunity_bullets(
                "找增长机会点", self.candidates, brand="欧莱雅", period="2026年8月",
            )
        self.assertEqual(calls["n"], 2)
        self.assertTrue(used_llm)

    def test_unknown_candidate_id_is_rejected(self):
        client, calls = _fake_llm([
            '{"picks": [{"candidate_id": "cand_999", "reason": "值得关注"}]}',
        ] * 3)
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "fake"}, clear=False), \
             patch("bot.opportunity_insight.llm_client", return_value=client):
            bullets, used_llm = generate_opportunity_bullets(
                "找增长机会点", self.candidates, brand="欧莱雅", period="2026年8月",
            )
        # All three attempts reference a candidate that doesn't exist, so the
        # deterministic fallback must still produce a grounded answer rather
        # than an empty result.
        self.assertEqual(calls["n"], 3)
        self.assertFalse(used_llm)
        self.assertEqual(len(bullets), 1)


class OpportunityInsightRouteTest(unittest.TestCase):
    def _context_state(self):
        state = SessionState()
        state.ec_context.brand = "欧莱雅"
        state.ec_context.period = "2026年8月"
        state.ec_context.platform = "TM"
        state.drilldown_ctx.brand = "欧莱雅"
        state.drilldown_ctx.period = "2026年8月"
        return state

    def test_bare_opportunity_followup_reaches_skill_dispatch_with_inherited_brand(self):
        result = route("有什么增长机会点", self._context_state())
        self.assertEqual(result.type, "skill_dispatch")
        self.assertEqual(result.brand, "欧莱雅")

    def test_plain_lookup_still_does_not_route_as_narrow_followup(self):
        result = route("欧莱雅8月20日在天猫的生意是多少", SessionState())
        self.assertNotEqual(result.type, "skill_dispatch")

    def test_compound_report_plus_opportunity_message_reuses_existing_context(self):
        # A single message that both repeats the brand/store-name/period the
        # user already has a cached report for *and* asks for opportunity
        # insight used to be caught by is_unspecified_platform_business_question
        # purely on its "生意表现" wording, which asked to re-clarify the
        # platform and dropped the opportunity request entirely.
        state = self._context_state()
        result = route(
            "分析一下欧莱雅官方旗舰店昨天的生意表现，找出你认为有生意增长机会点的行动方案",
            state,
        )
        self.assertEqual(result.type, "skill_dispatch")
        self.assertEqual(result.brand, "欧莱雅")

    def test_compound_message_without_existing_context_still_asks_platform(self):
        # Without a cached report to reason over, the same compound message
        # must not be silently treated as a followup with no evidence behind
        # it — asking which platform to build the report on is still correct.
        result = route(
            "分析一下欧莱雅官方旗舰店昨天的生意表现，找出你认为有生意增长机会点的行动方案",
            SessionState(),
        )
        self.assertEqual(result.type, "clarify_ec_platform")
        self.assertEqual(result.brand, "欧莱雅")


class OpportunityInsightChainTest(unittest.TestCase):
    def _evidence_state(self):
        state = SessionState()
        evidence = {
            "brand": "欧莱雅", "platform": "TM", "period": {"raw": "2026年8月"},
            "categories": [
                {"category_cn": "护肤-面霜/乳液", "gmv_current": 5_000_000, "gmv_prior": 4_000_000, "evol": 0.25},
                {"category_cn": "护肤-精华", "gmv_current": 3_000_000, "gmv_prior": 3_400_000, "evol": -0.12},
            ],
            "key_drivers": [
                {"key_driver": "李佳琦", "gmv_current": 4_000_000, "gmv_prior": 3_000_000, "evol": 0.33},
            ],
            "series_scopes": {},
        }
        state.ec_context.brand = "欧莱雅"
        state.ec_context.period = "2026年8月"
        state.ec_context.platform = "TM"
        state.ec_context.report_cache = {"ec_report_evidence": evidence}
        state.drilldown_ctx.brand = "欧莱雅"
        state.drilldown_ctx.period = "2026年8月"
        return state

    def test_chain_produces_grounded_bullets_from_cached_report_evidence(self):
        result = run_followup_v2_chain(
            "分析一下欧莱雅生意表现，找出增长机会点行动方案", self._evidence_state(),
        )
        self.assertEqual(result["meta"]["report_type"], "opportunity_insight")
        self.assertIn("护肤-面霜/乳液", result["markdown"])
        self.assertIn("62.5%", result["markdown"])  # 5M / (5M+3M) share, rendered by Python not the LLM
        self.assertNotIn("护肤-精华", result["markdown"])  # shrinking share must not be recommended

    def test_chain_asks_for_a_report_first_when_no_evidence_is_cached(self):
        result = run_followup_v2_chain(
            "有什么增长机会点", SessionState(), brand="欧莱雅", period="2026年8月",
        )
        self.assertEqual(result["meta"]["report_type"], "opportunity_insight")
        self.assertIn("生成一份完整的生意分析报告", result["markdown"])


if __name__ == "__main__":
    unittest.main()
