from __future__ import annotations

import unittest

from bot.brand_reference import english_brand_for_chinese


class BrandReferenceTest(unittest.TestCase):
    def test_unambiguous_chinese_names_map_to_english(self):
        self.assertEqual(english_brand_for_chinese("珀莱雅"), "PROYA")
        self.assertEqual(english_brand_for_chinese("花西子"), "Florasis")
        self.assertEqual(english_brand_for_chinese(" 欧莱雅 "), "L'OREAL PARIS")

    def test_unknown_and_conflicting_names_do_not_auto_map(self):
        self.assertIsNone(english_brand_for_chinese("不存在的品牌"))
        self.assertIsNone(english_brand_for_chinese("多芬"))
        self.assertIsNone(english_brand_for_chinese("马应龙"))


if __name__ == "__main__":
    unittest.main()
