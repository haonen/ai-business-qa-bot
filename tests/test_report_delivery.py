from __future__ import annotations

import unittest

from bot.report_delivery import document_title, should_generate_document


class ReportDeliveryTests(unittest.TestCase):
    def test_multi_period_brand_analysis_always_uses_a_document(self):
        meta = {
            "brand": "谷雨", "platform": "TM",
            "period": "2026年5月、2026年6月", "document_ready": True,
        }

        self.assertTrue(should_generate_document("multi_period_business_analysis", meta))
        self.assertEqual(
            document_title("multi_period_business_analysis", meta),
            "谷雨 2026年5月、2026年6月 天猫多时段同比分析",
        )

    def test_document_ready_false_still_blocks_document_generation(self):
        self.assertFalse(should_generate_document(
            "multi_period_business_analysis",
            {"brand": "谷雨", "document_ready": False},
        ))


if __name__ == "__main__":
    unittest.main()
