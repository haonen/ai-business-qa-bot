from __future__ import annotations

import json
import os
import re

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    UpdateMessageRequest,
    UpdateMessageRequestBody,
)

from bot.config import get_env


def _table_count(markdown: str) -> int:
    return sum(
        1 for line in str(markdown or "").splitlines()
        if re.match(r"^\s*\|(?:\s*:?-{3,}:?\s*\|){2,}\s*$", line)
    )


def split_markdown_cards(text: str) -> list[str]:
    """Split a report without breaking contiguous Markdown table blocks."""
    max_tables = max(1, int(os.environ.get("FEISHU_CARD_MAX_TABLES", "3")))
    max_chars = max(2000, int(os.environ.get("FEISHU_CARD_MAX_CHARS", "12000")))
    blocks = [block.strip() for block in re.split(r"\n{2,}", str(text or "")) if block.strip()]
    if not blocks:
        return [str(text or "")]
    chunks: list[str] = []
    current: list[str] = []
    current_tables = 0
    current_chars = 0
    for block in blocks:
        block_tables = _table_count(block)
        added_chars = len(block) + (2 if current else 0)
        if current and (
            current_tables + block_tables > max_tables
            or current_chars + added_chars > max_chars
        ):
            chunks.append("\n\n".join(current))
            current, current_tables, current_chars = [], 0, 0
        current.append(block)
        current_tables += block_tables
        current_chars += len(block) + (2 if len(current) > 1 else 0)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def create_lark_client() -> lark.Client:
    return (
        lark.Client.builder()
        .app_id(get_env("FEISHU_APP_ID", "APP_ID"))
        .app_secret(get_env("FEISHU_APP_SECRET", "APP_SECRET"))
        .build()
    )


def send_reply(client: lark.Client, chat_id: str, text: str) -> None:
    chunks = split_markdown_cards(text)
    for index, chunk in enumerate(chunks, 1):
        content = f"**分析结果 {index}/{len(chunks)}**\n\n{chunk}" if len(chunks) > 1 else chunk
        card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": content}]}}
        resp = client.im.v1.message.create(
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        if not resp.success():
            lark.logger.error(f"send_reply failed chunk={index}/{len(chunks)}: {resp.code} {resp.msg}")
            # A rejected interactive card must never leave only an empty
            # completion placeholder in the conversation.
            send_text(client, chat_id, content)


def send_text(client: lark.Client, chat_id: str, text: str) -> str | None:
    resp = client.im.v1.message.create(
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    if not resp.success():
        lark.logger.error(f"send_text failed: {resp.code} {resp.msg}")
        return None
    return resp.data.message_id


def update_text(client: lark.Client, message_id: str | None, text: str) -> None:
    if not message_id:
        return
    resp = client.im.v1.message.update(
        UpdateMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            UpdateMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    if not resp.success():
        lark.logger.error(f"update_text failed: {resp.code} {resp.msg}")
