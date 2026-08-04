from __future__ import annotations

import unittest

import pandas as pd

from data_import.search_pipeline import (
    GRAIN_BRAND,
    GRAIN_BRAND_CATEGORY,
    calculate_yoy,
    parse_rate,
    transform_sheet,
)


class SearchPipelineTest(unittest.TestCase):
    def test_parse_rate(self):
        self.assertAlmostEqual(parse_rate("11.41%"), 0.1141)
        self.assertEqual(parse_rate(-0.2), -0.2)
        self.assertIsNone(parse_rate(""))

    def test_calculate_yoy_handles_zero(self):
        self.assertEqual(calculate_yoy(110, 100), 0.1)
        self.assertIsNone(calculate_yoy(10, 0))

    def test_brand_grain(self):
        frame = pd.DataFrame(
            {
                "排名": [1],
                "品牌": ["兰蔻"],
                "2026年01月搜索指数": [110],
                "2025年01月搜索指数": [100],
                "同比增长率": ["10.00%"],
            }
        )
        cleaned, summary, warnings = transform_sheet(
            frame,
            source_file="Search Report.xlsx",
            sheet_name="1月",
            fallback_report_year=2026,
            batch_id="test",
        )
        self.assertFalse(warnings)
        self.assertEqual(summary["grain_level"], GRAIN_BRAND)
        self.assertEqual(cleaned.loc[0, "grain_level"], GRAIN_BRAND)
        self.assertIsNone(cleaned.loc[0, "category"])

    def test_category_grain(self):
        frame = pd.DataFrame(
            {
                "排名": [1],
                "品牌": ["CeraVe"],
                "类目": ["乳液"],
                "2026年03月搜索指数": [451000],
                "2025年03月搜索指数": [457000],
                "同比增长率": ["-1.31%"],
            }
        )
        cleaned, summary, _ = transform_sheet(
            frame,
            source_file="Search Report.xlsx",
            sheet_name="3月",
            fallback_report_year=2026,
            batch_id="test",
        )
        self.assertEqual(summary["grain_level"], GRAIN_BRAND_CATEGORY)
        self.assertEqual(cleaned.loc[0, "category"], "乳液")

    def test_sheet_month_is_authoritative(self):
        frame = pd.DataFrame(
            {
                "排名": [1],
                "品牌": ["YSL"],
                "2026年06月搜索指数": [100],
                "2025年06月搜索指数": [80],
                "同比增长率": ["25%"],
            }
        )
        cleaned, _, warnings = transform_sheet(
            frame,
            source_file="Search Report.xlsx",
            sheet_name="5月",
            fallback_report_year=2026,
            batch_id="test",
        )
        self.assertEqual(cleaned.loc[0, "report_month_num"], 5)
        self.assertTrue(any("按 sheet 月份入库" in item for item in warnings))

    def test_generic_columns_use_report_year(self):
        frame = pd.DataFrame(
            {
                "排名": [1],
                "品牌": ["YSL"],
                "now_date_search_index": [100],
                "last_date_search_index": [80],
                "同比增长率": ["25%"],
            }
        )
        cleaned, _, warnings = transform_sheet(
            frame,
            source_file="Search Report.xlsx",
            sheet_name="6月",
            fallback_report_year=2026,
            batch_id="test",
        )
        self.assertEqual(cleaned.loc[0, "report_year"], 2026)
        self.assertTrue(any("通用搜索指数字段" in item for item in warnings))


if __name__ == "__main__":
    unittest.main()
