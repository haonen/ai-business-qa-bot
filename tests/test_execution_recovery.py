from __future__ import annotations

import unittest

from bot.execution_recovery import build_period_revision_request


class ExecutionRecoveryTests(unittest.TestCase):
    def test_period_coverage_error_preserves_original_platform_and_task(self):
        pending = build_period_revision_request(
            {
                "ok": False,
                "meta": {
                    "error_code": "period_after_latest_date",
                    "latest_date": "2026-07-19",
                },
            },
            original_text="欧莱雅旗舰店 天猫 2026年7月的生意vs 2025年7月的生意",
            brand="欧莱雅", brand_aliases=["欧莱雅", "L'OREAL PARIS"],
            platform="TM", intents=["EC_BUSINESS"],
            comparison_spec={"mode": "YOY_ALIGNED"},
            time_scope={"status": "exact"},
        )
        self.assertEqual(pending["intent"], "v2_period")
        self.assertEqual((pending["brand"], pending["platform"]), ("欧莱雅", "TM"))
        self.assertEqual(pending["comparison_spec"], {"mode": "YOY_ALIGNED"})

    def test_non_recoverable_error_does_not_open_period_slot(self):
        self.assertIsNone(build_period_revision_request(
            {"ok": False, "meta": {"error_code": "database_error"}},
            original_text="问题", brand="欧莱雅", brand_aliases=[], platform="TM",
            intents=["EC_BUSINESS"], comparison_spec={}, time_scope={},
        ))


if __name__ == "__main__":
    unittest.main()
