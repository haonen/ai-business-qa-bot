from __future__ import annotations

import unittest

import pandas as pd

from data_import.ksi_pipeline import transform_chunk, unique_headers


class KsiPipelineTest(unittest.TestCase):
    def test_duplicate_tier_header_is_preserved(self):
        headers = unique_headers(["Tier", "Big V", "Tier"])
        self.assertEqual(headers, ["Tier", "BigV", "Tier__2"])

    def test_2025_extended_columns(self):
        source = pd.DataFrame(
            {
                "Platform": ["red"],
                "Brand": ["Brand A"],
                "Tier": ["T2"],
                "BigV": [1000],
                "BET": [0.1],
                "NickName": ["KOL A"],
                "PublishedAt": [pd.Timestamp("2025-03-15")],
                "MONTH": [3],
                "TTLengagement": [200],
                "KOLtype": ["Beauty"],
                "Selectivity": ["MASS"],
                "Tier__2": ["T2"],
                "_source_row_number": [2],
            }
        )
        cleaned, stats, _ = transform_chunk(
            source,
            source_file="KSI 2025.xlsx",
            source_sheet="Sheet1",
            batch_id="test",
        )
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned.loc[0, "year"], 2025)
        self.assertEqual(cleaned.loc[0, "month"], 3)
        self.assertEqual(cleaned.loc[0, "big_v_cost"], 1000)
        self.assertEqual(cleaned.loc[0, "ttl_engagement"], 200)
        self.assertEqual(stats["tier_mismatch_rows"], 0)
        self.assertEqual(stats["source_month_mismatch_rows"], 0)

    def test_2026_missing_optional_columns(self):
        source = pd.DataFrame(
            {
                "Platform": ["douyin"],
                "Brand": ["Brand B"],
                "Tier": ["T4"],
                "BigV": [500],
                "NickName": ["KOL B"],
                "PublishedAt": [pd.Timestamp("2026-01-20")],
                "TTLengagement": [0],
                "KOLtype": ["Seeding"],
                "_source_row_number": [2],
            }
        )
        cleaned, _, missing = transform_chunk(
            source,
            source_file="KSI 2026.xlsx",
            source_sheet="Sheet1",
            batch_id="test",
        )
        self.assertTrue(pd.isna(cleaned.loc[0, "bet"]))
        self.assertTrue(pd.isna(cleaned.loc[0, "selectivity"]))
        self.assertIn("bet", missing)
        self.assertIn("tier_secondary", missing)

    def test_invalid_required_row_is_removed(self):
        source = pd.DataFrame(
            {
                "Platform": ["red"],
                "Brand": ["Brand A"],
                "Tier": ["T2"],
                "BigV": [None],
                "NickName": ["KOL A"],
                "PublishedAt": [pd.Timestamp("2025-03-15")],
                "TTLengagement": [200],
                "KOLtype": ["Beauty"],
                "_source_row_number": [2],
            }
        )
        cleaned, stats, _ = transform_chunk(
            source,
            source_file="KSI.xlsx",
            source_sheet="Sheet1",
            batch_id="test",
        )
        self.assertTrue(cleaned.empty)
        self.assertEqual(stats["invalid_rows"], 1)

    def test_fractional_big_v_keeps_decimal_dtype(self):
        source = pd.DataFrame(
            {
                "Platform": ["red"],
                "Brand": ["Brand A"],
                "Tier": ["T2"],
                "BigV": [123.456789],
                "BET": [0.000131],
                "NickName": ["KOL A"],
                "PublishedAt": [pd.Timestamp("2025-03-15")],
                "TTLengagement": [200],
                "KOLtype": ["Beauty"],
                "_source_row_number": [2],
            }
        )
        cleaned, _, _ = transform_chunk(
            source,
            source_file="KSI.xlsx",
            source_sheet="Sheet1",
            batch_id="test",
        )
        self.assertAlmostEqual(cleaned.loc[0, "big_v_cost"], 123.456789)
        self.assertEqual(str(cleaned["big_v_cost"].dtype), "Float64")


if __name__ == "__main__":
    unittest.main()
