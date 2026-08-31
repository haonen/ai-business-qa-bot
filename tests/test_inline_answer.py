from __future__ import annotations

import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from bot.inline_answer import build_inline_answer, document_status


def _response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class InlineAnswerTest(unittest.TestCase):
    def test_grounded_llm_answer_is_used(self):
        client = MagicMock()
        client.chat.completions.create.return_value = _response("韩束GMV为56.9M，同比下降40%。")
        report = "# 整体生意\n韩束GMV为56.9M，同比-40%。"
        with patch.dict(os.environ, {"BOT_INLINE_ANSWER_ENABLED": "1", "DASHSCOPE_API_KEY": "x"}), \
             patch("bot.inline_answer.llm_client", return_value=client):
            answer = build_inline_answer("韩束生意怎么样", report)
        self.assertIn("56.9M", answer)
        self.assertIn("40%", answer)

    def test_ungrounded_number_is_rejected_and_falls_back_to_report(self):
        client = MagicMock()
        client.chat.completions.create.return_value = _response("韩束GMV为99M。")
        report = "# 整体生意\n韩束GMV为56.9M，同比-40%。"
        with patch.dict(os.environ, {"BOT_INLINE_ANSWER_ENABLED": "1", "DASHSCOPE_API_KEY": "x"}), \
             patch("bot.inline_answer.llm_client", return_value=client):
            answer = build_inline_answer("韩束生意怎么样", report)
        self.assertNotIn("99M", answer)
        self.assertIn("56.9M", answer)

    def test_feature_can_be_disabled(self):
        with patch.dict(os.environ, {"BOT_INLINE_ANSWER_ENABLED": "0"}):
            self.assertIsNone(build_inline_answer("生意怎么样", "GMV为1M"))

    def test_document_status_preserves_answer(self):
        rendered = document_status("结论句。", "完整报告：link")
        self.assertIn("结论句", rendered)
        self.assertIn("link", rendered)
