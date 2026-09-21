from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from bot.douyin_business_formatter import format_douyin_business_report
from bot.tools.query_douyin_business import _product_analysis_v2, query_douyin_business


def _row(period, item, category, driver, gmv, *, title=None, invalid=0, count=1):
    return {
        "period_key": period,
        "item_id": item,
        "product_name": title or f"商品{item}",
        "product_url": f"https://example.test/{item}",
        "category_level_4": category,
        "key_driver": driver,
        "sales_gmv": gmv,
        "invalid_numeric_rows": invalid,
        "row_count": count,
    }


class DouyinProductDailyAnalysisTest(unittest.TestCase):
    @patch("bot.tools.query_douyin_business._infer_series_mapping", return_value={"红蛮腰套装": "红蛮腰"})
    def test_selects_growth_category_then_builds_each_driver_drilldown(self, _mapping):
        frame = pd.DataFrame([
            _row("current", "1", "面霜", "Store live", 500, title="红蛮腰套装"),
            _row("prior", "1", "面霜", "Store live", 300, title="红蛮腰套装"),
            _row("current", "2", "精华", "Kol live", 300),
            _row("prior", "2", "精华", "Kol live", 250),
            _row("current", "3", "面膜", "Video", 150),
            _row("prior", "3", "面膜", "Video", 200),
            _row("current", "4", "防晒", "Product tab", 50),
            _row("prior", "4", "防晒", "Product tab", 40),
        ])
        result = _product_analysis_v2(
            frame, brand_candidates=["韩束"],
            store_total={"gmv_current": 1250, "gmv_prior": 1000},
        )
        self.assertTrue(result["quality"]["passed"])
        self.assertEqual(result["selected_category"], "面霜")
        self.assertEqual(result["selected_category_series"][0]["series"], "红蛮腰")
        self.assertEqual(result["selected_category_top_links"][0]["product_name"], "红蛮腰套装")
        self.assertEqual([row["key_driver"] for row in result["key_drivers"]], [
            "Store live", "Kol live", "Video", "Product tab",
        ])
        self.assertEqual(len(result["driver_drilldowns"]), 4)
        self.assertEqual(result["driver_drilldowns"][0]["top_category"]["category"], "面霜")
        self.assertAlmostEqual(result["reconciliation"]["coverage_current"], 0.8)

    def test_all_declining_selects_smallest_decline_and_ties_by_current_gmv(self):
        frame = pd.DataFrame([
            _row("current", "1", "A", "Store live", 200),
            _row("prior", "1", "A", "Store live", 230),
            _row("current", "2", "B", "Kol live", 300),
            _row("prior", "2", "B", "Kol live", 330),
        ])
        with patch("bot.tools.query_douyin_business._infer_series_mapping", return_value={}):
            result = _product_analysis_v2(
                frame, brand_candidates=["X"],
                store_total={"gmv_current": 500, "gmv_prior": 560},
            )
        self.assertEqual(result["selected_category"], "B")

    def test_bad_labels_and_numbers_fail_quality_and_never_enter_rankings(self):
        frame = pd.DataFrame([
            _row("current", "1", "A", None, 999, count=2),
            _row("current", "2", "B", None, 888, count=3),
            _row("current", "3", "C", "Store live", 777, invalid=1),
            _row("current", "4", "D", "Video", 10),
            _row("prior", "4", "D", "Video", 5),
        ])
        with patch("bot.tools.query_douyin_business._infer_series_mapping", return_value={}):
            result = _product_analysis_v2(
                frame, brand_candidates=["X"],
                store_total={"gmv_current": 10, "gmv_prior": 5},
            )
        self.assertFalse(result["quality"]["passed"])
        self.assertEqual(result["quality"]["null_driver_rows"], 5)
        self.assertEqual(result["quality"]["invalid_numeric_rows"], 1)
        self.assertEqual(result["selected_category"], "D")
        self.assertEqual(result["product_total"]["gmv_current"], 10)

    def test_driver_uses_exact_database_label(self):
        frame = pd.DataFrame([
            _row("current", "1", "精华", "Kol live", 100),
            _row("prior", "1", "精华", "Kol live", 80),
        ])
        with patch("bot.tools.query_douyin_business._infer_series_mapping", return_value={}):
            result = _product_analysis_v2(
                frame, brand_candidates=["X"],
                store_total={"gmv_current": 100, "gmv_prior": 80},
            )
        self.assertTrue(result["quality"]["passed"])
        kol = next(row for row in result["key_drivers"] if row["key_driver"] == "Kol live")
        self.assertEqual(kol["gmv_current"], 100)

    @patch("bot.tools.query_douyin_business._infer_series_mapping", return_value={
        "已分类面霜": "面霜系列",
        "未分类爆款": "未知系列",
    })
    def test_unclassified_is_visible_but_never_selected_or_drilled(self, _mapping):
        frame = pd.DataFrame([
            _row("current", "u", "未分类", "Store live", 900, title="未分类爆款"),
            _row("prior", "u", "未分类", "Store live", 100, title="未分类爆款"),
            _row("current", "c", "面霜", "Store live", 100, title="已分类面霜"),
            _row("prior", "c", "面霜", "Store live", 50, title="已分类面霜"),
        ])
        result = _product_analysis_v2(
            frame, brand_candidates=["X"],
            store_total={"gmv_current": 1000, "gmv_prior": 150},
        )
        self.assertEqual(result["categories"][0]["category"], "未分类")
        self.assertEqual(result["selected_category"], "面霜")
        self.assertEqual([row["category"] for row in result["analysis_categories"]], ["面霜"])
        store = result["driver_drilldowns"][0]
        self.assertEqual(store["top_category"]["category"], "面霜")
        self.assertEqual(store["top_links"][0]["product_name"], "已分类面霜")
        self.assertEqual(store["series"][0]["series"], "面霜系列")
        self.assertEqual(result["unclassified"]["gmv_current"], 900)


class DouyinProductDailyRuntimeTest(unittest.TestCase):
    @patch("bot.tools.query_douyin_business.resolve_source_brand", return_value={"brand": "KANS"})
    @patch("bot.tools.query_douyin_business._query_s4_products")
    @patch("bot.tools.query_douyin_business._query_s2_categories")
    @patch("bot.tools.query_douyin_business._infer_series_mapping", return_value={})
    def test_v2_product_analysis_survives_store_coverage_gap(
        self, _mapping, categories, products, _resolve
    ):
        categories.return_value = (
            pd.DataFrame([
                {"period_key": "current", "category_level_4": "面霜", "gmv": 50},
                {"period_key": "prior", "category_level_4": "面霜", "gmv": 40},
            ]),
            [{"period": "current", "month": "2026-07", "source": "daily", "table": "store"}],
            [{"period": "current", "month": "2026-07", "source": "daily", "table": "store"}],
        )
        products.return_value = pd.DataFrame([
            _row("current", "1", "面霜", "Store live", 90),
            _row("prior", "1", "面霜", "Store live", 70),
        ])
        with patch.dict("os.environ", {
            "DOUYIN_PRODUCT_DAILY_V2_ENABLED": "1",
            "DOUYIN_PRODUCT_DAILY_V2_SHADOW": "0",
            "DOUYIN_PRODUCT_DAILY_QUALITY_GATE": "1",
        }):
            result = query_douyin_business("KANS", "2026年6月1日至7月19日")
        self.assertNotIn("error", result)
        self.assertEqual(result["brand_result"], {})
        self.assertEqual(result["product_analysis"]["product_total"]["gmv_current"], 90)
        self.assertIn("店铺表数据存在缺口", result["limitations"][0])

    @patch("bot.tools.query_douyin_business.resolve_source_brand", return_value={"brand": "KANS"})
    @patch("bot.tools.query_douyin_business._query_s4_products", return_value=pd.DataFrame())
    @patch("bot.tools.query_douyin_business._query_s2_categories")
    def test_shadow_still_queries_product_when_v1_store_coverage_is_missing(
        self, categories, products, _resolve
    ):
        categories.return_value = (
            pd.DataFrame(), [],
            [{"period": "current", "month": "2026-07", "source": "daily", "table": "store"}],
        )
        with patch.dict("os.environ", {
            "DOUYIN_PRODUCT_DAILY_V2_ENABLED": "0",
            "DOUYIN_PRODUCT_DAILY_V2_SHADOW": "1",
        }):
            result = query_douyin_business("KANS", "2026年6月1日至7月19日")
        self.assertEqual(result["error"], "incomplete_coverage")
        products.assert_called_once()

    @patch("bot.tools.query_douyin_business.resolve_source_brand", return_value={"brand": "KANS"})
    @patch("bot.tools.query_douyin_business._query_s2_channels")
    @patch("bot.tools.query_douyin_business._query_s4_products")
    @patch("bot.tools.query_douyin_business._query_s2_categories")
    @patch("bot.tools.query_douyin_business._infer_series_mapping", return_value={})
    def test_v2_queries_product_once_and_skips_legacy_channels(
        self, _mapping, categories, products, channels, _resolve
    ):
        categories.return_value = (
            pd.DataFrame([
                {"period_key": "current", "category_level_4": "面霜", "gmv": 100},
                {"period_key": "prior", "category_level_4": "面霜", "gmv": 80},
            ]), [{"table": "three_platform_store_rank_monthly"}], [],
        )
        products.return_value = pd.DataFrame([
            _row("current", "1", "面霜", "Store live", 90),
            _row("prior", "1", "面霜", "Store live", 70),
        ])
        with patch.dict("os.environ", {
            "DOUYIN_PRODUCT_DAILY_V2_ENABLED": "1",
            "DOUYIN_PRODUCT_DAILY_V2_SHADOW": "0",
            "DOUYIN_PRODUCT_DAILY_QUALITY_GATE": "1",
        }):
            result = query_douyin_business("KANS", "2026年6月")
        self.assertNotIn("error", result)
        self.assertIn("product_analysis", result)
        products.assert_called_once()
        channels.assert_not_called()

    @patch("bot.tools.query_douyin_business.resolve_source_brand", return_value={"brand": "KANS"})
    @patch("bot.tools.query_douyin_business._query_s4_products")
    @patch("bot.tools.query_douyin_business._query_s2_categories")
    def test_quality_gate_blocks_report(self, categories, products, _resolve):
        categories.return_value = (
            pd.DataFrame([
                {"period_key": "current", "category_level_4": "面霜", "gmv": 100},
                {"period_key": "prior", "category_level_4": "面霜", "gmv": 80},
            ]), [], [],
        )
        products.return_value = pd.DataFrame([
            _row("current", "1", "面霜", None, 90),
        ])
        with patch.dict("os.environ", {
            "DOUYIN_PRODUCT_DAILY_V2_ENABLED": "1",
            "DOUYIN_PRODUCT_DAILY_QUALITY_GATE": "1",
        }):
            result = query_douyin_business("KANS", "2026年6月")
        self.assertEqual(result["error"], "product_daily_quality_blocked")

    @patch("bot.douyin_business_formatter._title_insights_map", return_value={
        "category": "• 标题以抗皱套装和618活动表达为主。",
        "driver_0": "• Store live以抗皱套装为主。",
    })
    def test_v2_formatter_has_tmall_hierarchy_and_title_analysis(self, _bullets):
        product = {
            "quality": {"passed": True, "needs_review_rows": 0, "null_driver_rows": 0, "invalid_numeric_rows": 0},
            "product_total": {"gmv_current": 80, "gmv_prior": 60},
            "categories": [{"category": "面霜", "gmv_current": 80, "gmv_prior": 60, "gmv_change": 20, "evol": 1 / 3, "weight": 1, "weight_change": 0}],
            "selected_category": "面霜",
            "selected_category_series": [{"series": "红蛮腰", "gmv_current": 80, "gmv_prior": 60, "evol": 1 / 3, "weight": 1, "weight_change": 0}],
            "selected_category_top_links": [{
                "item_id": "1", "product_name": "618红蛮腰抗皱套装", "category": "面霜",
                "gmv_current": 80, "gmv_prior": 60, "evol": 1 / 3, "weight": 1,
            }],
            "key_drivers": [{"key_driver": "Store live", "gmv_current": 80, "gmv_prior": 60, "gmv_change": 20, "evol": 1 / 3, "weight": 1, "weight_change": 0}],
            "driver_drilldowns": [{"key_driver": "Store live", "top_category": {"category": "面霜", "gmv_current": 80, "gmv_prior": 60, "evol": 1 / 3}, "series": [{"series": "红蛮腰", "gmv_current": 80, "gmv_prior": 60, "evol": 1 / 3, "weight": 1, "weight_change": 0}], "top_links": [{
                "item_id": "1", "product_name": "618红蛮腰抗皱套装", "category": "面霜",
                "gmv_current": 80, "gmv_prior": 60, "evol": 1 / 3, "weight": 1,
            }]}],
            "reconciliation": {"coverage_current": 0.8, "coverage_prior": 0.75},
        }
        report = format_douyin_business_report({
            "brand": "韩束", "period": "2026年6月",
            "period_meta": {"current_start": "2026-06-01", "current_end": "2026-06-30", "prior_start": "2025-06-01", "prior_end": "2025-06-30"},
            "brand_result": {"gmv_current": 100, "gmv_prior": 80, "evol": 0.25},
            "product_analysis": product,
        })
        for heading in (
            "# 整体生意", "# 品类分析", "## 品类表现", "## 面霜下钻",
            "# Key Driver分析", "## Key Driver表现", "## Key Driver下钻",
            "附录｜口径对账与数据边界",
        ):
            self.assertIn(heading, report)
        self.assertNotIn("## 1.", report)
        self.assertIn("商品链接标题", report)
        self.assertIn("标题以抗皱套装", report)
        self.assertIn("主要产品系列", report)
        self.assertIn("未分类”仅用于展示数据覆盖情况", report)


if __name__ == "__main__":
    unittest.main()
