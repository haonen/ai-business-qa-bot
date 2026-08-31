from __future__ import annotations

import logging

from bot.messaging import create_lark_client, send_reply, update_text
from bot.inline_answer import document_status


log = logging.getLogger(__name__)


def _payload_from_job(job) -> dict:
    if getattr(job, "args", None) and isinstance(job.args[0], dict):
        return job.args[0]
    return {}


def execute_document(payload: dict) -> dict:
    # Import only the document adapter in this process; the analysis application
    # and its Pandas/query chain are intentionally not loaded by document workers.
    import bot.feishu_doc as feishu_doc

    client = create_lark_client()
    doc_url = feishu_doc.create_feishu_doc(client, payload["title"], payload["markdown"])
    completion = document_status(
        payload.get("inline_answer"),
        f"完整分析报告：{doc_url}",
    )
    if payload.get("placeholder_id"):
        update_text(client, payload["placeholder_id"], completion)
    else:
        send_reply(client, payload["chat_id"], completion)
    return {"ok": True, "document_url": doc_url}


def handle_document_failure(job, connection, exc_type, exc_value, traceback, *args, **kwargs) -> None:
    payload = _payload_from_job(job)
    if not payload.get("chat_id"):
        return
    try:
        client = create_lark_client()
        update_text(
            client,
            payload.get("placeholder_id"),
            document_status(
                payload.get("inline_answer"),
                "文档生成失败，已将完整结果发送到当前会话。",
            ),
        )
        send_reply(client, payload["chat_id"], payload.get("markdown") or "没有生成可用结果。")
    except Exception:
        log.exception("failed to send document fallback job_id=%s", getattr(job, "id", None))
