from __future__ import annotations
from bot.timing import timed, job_timed, sql_timed, span, event

import logging
import time

from bot.app import run_agent
run_agent = timed('analysis.run_agent')(run_agent)
from bot.messaging import send_reply, update_text
from bot.runtime_config import async_documents_enabled, queue_enabled
from bot.session import add_message, get_session
from bot.request_audit import record_outcome
from bot.agent_plan import completion_status
from bot.inline_answer import build_inline_answer, document_status
from bot.report_delivery import document_title, should_generate_document


log = logging.getLogger(__name__)


def process_request(
    client,
    *,
    open_id: str,
    chat_id: str,
    user_text: str,
    placeholder_id: str | None,
    on_progress=None,
    analysis_job_id: str | None = None,
    message_id: str | None = None,
    received_at=None,
) -> dict:
    def progress(stage_text: str) -> None:
        if on_progress:
            on_progress(stage_text)
        else:
            update_text(client, placeholder_id, stage_text)

    started = time.perf_counter()
    state = get_session(open_id)
    try:
        result = run_agent(open_id, user_text, state, on_progress=progress, received_at=received_at)
    except Exception as exc:
        record_outcome(
            message_id=message_id or "",
            result=None,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            status="failed",
            error_message=str(exc),
        )
        raise
    record_outcome(
        message_id=message_id or "",
        result=result,
        elapsed_ms=round((time.perf_counter() - started) * 1000),
    )
    markdown = result.get("markdown") or "没有生成可用结果。"
    add_message(open_id, "user", user_text)
    add_message(open_id, "assistant", markdown)

    route_type = result.get("route_type")
    report_meta = result.get("meta", {})
    if should_generate_document(route_type, report_meta):
        doc_title = document_title(route_type, report_meta)
        inline_answer = build_inline_answer(user_text, markdown)
        if queue_enabled() and async_documents_enabled():
            update_text(
                client,
                placeholder_id,
                document_status(inline_answer, "完整报告正在后台生成文档…"),
            )
            try:
                from bot.document_queue import enqueue_document

                enqueue_document({
                    "analysis_job_id": analysis_job_id,
                    "title": doc_title,
                    "markdown": markdown,
                    "chat_id": chat_id,
                    "placeholder_id": placeholder_id,
                    "inline_answer": inline_answer,
                })
            except Exception as exc:
                log.exception("document enqueue failed: %s", exc)
                update_text(
                    client,
                    placeholder_id,
                    document_status(inline_answer, "文档服务暂不可用，已将完整结果发送到当前会话。"),
                )
                send_reply(client, chat_id, markdown)
            return result

        import bot.feishu_doc as feishu_doc

        progress("正在生成分析报告文档…")
        try:
            doc_url = feishu_doc.create_feishu_doc(client, doc_title, markdown)
            completion = document_status(inline_answer, f"完整分析报告：{doc_url}")
            if placeholder_id:
                update_text(client, placeholder_id, completion)
            else:
                send_reply(client, chat_id, completion)
        except Exception as exc:
            log.exception("document generation failed: %s", exc)
            update_text(
                client,
                placeholder_id,
                document_status(inline_answer, "飞书文档服务暂时限流，已将完整结果发送到当前会话。"),
            )
            send_reply(client, chat_id, markdown)
    else:
        update_text(client, placeholder_id, completion_status(route_type, report_meta))
        send_reply(client, chat_id, markdown)
    return result
