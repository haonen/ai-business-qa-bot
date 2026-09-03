from __future__ import annotations

import unittest

from bot.chains.multi_period_business_chain import run_multi_period_business_chain


class MultiPeriodBusinessChainTests(unittest.TestCase):
    def test_brand_macro_category_is_never_silently_executed_as_total_beauty(self):
        calls = []

        def fake_runner(*args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True, "markdown": "wrong unfiltered result", "meta": {}}

        result = run_multi_period_business_chain(
            "珀莱雅", "TM",
            {
                "mode": "YOY_ALIGNED",
                "analysis_periods": [
                    {"label": "2026年5月", "start_date": "2026-05-01", "end_date": "2026-05-31"},
                    {"label": "2026年6月", "start_date": "2026-06-01", "end_date": "2026-06-30"},
                ],
                "baseline_periods": [
                    {"label": "2025年5月", "start_date": "2025-05-01", "end_date": "2025-05-31"},
                    {"label": "2025年6月", "start_date": "2025-06-01", "end_date": "2025-06-30"},
                ],
            },
            category="FEMALE SKINCARE", runner=fake_runner,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["meta"]["error_code"], "unsupported_brand_macro_category")
        self.assertEqual(calls, [])

    def test_douyin_runner_executes_each_observation_against_its_yoy_baseline(self):
        calls = []

        def fake_runner(brand, period, brand_aliases=None, on_progress=None):
            calls.append(period)
            month = period.start_date[5:7]
            current, prior = ((120.0, 100.0) if month == "05" else (150.0, 100.0))
            return {
                "ok": True,
                "markdown": f"{month}月详细分析",
                "meta": {
                    "brand": brand,
                    "last_result_cache": {
                        "douyin_business_result": {
                            "brand_result": {
                                "gmv_current": current,
                                "gmv_prior": prior,
                                "gmv_change": current - prior,
                                "evol": current / prior - 1,
                            }
                        }
                    },
                },
            }

        result = run_multi_period_business_chain(
            "珀莱雅", "DY",
            {
                "mode": "YOY_ALIGNED",
                "analysis_periods": [
                    {"label": "2026年5月", "start_date": "2026-05-01", "end_date": "2026-05-31"},
                    {"label": "2026年6月", "start_date": "2026-06-01", "end_date": "2026-06-30"},
                ],
                "baseline_periods": [
                    {"label": "2025年5月", "start_date": "2025-05-01", "end_date": "2025-05-31"},
                    {"label": "2025年6月", "start_date": "2025-06-01", "end_date": "2025-06-30"},
                ],
            },
            brand_aliases=["珀莱雅", "PROYA"], runner=fake_runner,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].comparison_start, "2025-05-01")
        self.assertEqual(calls[1].comparison_start, "2025-06-01")
        self.assertIn("+20.0%", result["markdown"])
        self.assertIn("+50.0%", result["markdown"])
        self.assertIn("扩大 +30.0pp", result["markdown"])

    def test_normalizes_existing_platform_and_market_result_contracts(self):
        result_shapes = {
            ("brand", "TM"): {"last_result_cache": {"ttl_result": {"total": {"gmv_current": 120, "gmv_prior": 100, "evol": .2}}}},
            ("brand", "DY"): {"last_result_cache": {"douyin_business_result": {"brand_result": {"gmv_current": 120, "gmv_prior": 100, "evol": .2}}}},
            ("brand", "JD"): {"last_result_cache": {"jd_business_result": {"brand_result": {"gmv_current": 120, "gmv_prior": 100, "evol": .2}}}},
            ("brand", "TTL"): {"last_result_cache": {"three_platform_competitor_result": {"overall": [{"platform": "TTL", "gmv_current": 120, "gmv_prior": 100, "evol": .2}]}}},
            ("market", "TM"): {"market_result": {"rows": [{"platform": "TM", "gmv_actual": 120, "gmv_prior": 100, "gmv_growth": 20, "evol": .2}]}},
            ("market", "TTL"): {"market_result": {"rows": [{"platform": "TTL", "gmv_actual": 120, "gmv_prior": 100, "gmv_growth": 20, "evol": .2}]}},
        }
        comparison = {
            "mode": "YOY_ALIGNED",
            "analysis_periods": [
                {"label": "2026年5月", "start_date": "2026-05-01", "end_date": "2026-05-31"},
                {"label": "2026年6月", "start_date": "2026-06-01", "end_date": "2026-06-30"},
            ],
            "baseline_periods": [
                {"label": "2025年5月", "start_date": "2025-05-01", "end_date": "2025-05-31"},
                {"label": "2025年6月", "start_date": "2025-06-01", "end_date": "2025-06-30"},
            ],
        }
        for (subject, platform), meta in result_shapes.items():
            with self.subTest(subject=subject, platform=platform):
                def fake_runner(brand, period, brand_aliases=None, on_progress=None, _meta=meta):
                    return {"ok": True, "markdown": "detail", "meta": _meta}

                result = run_multi_period_business_chain(
                    "" if subject == "market" else "珀莱雅",
                    platform, comparison, subject_scope=subject, runner=fake_runner,
                )
                self.assertTrue(result["ok"], result)
                self.assertIn("+20.0%", result["markdown"])


if __name__ == "__main__":
    unittest.main()
