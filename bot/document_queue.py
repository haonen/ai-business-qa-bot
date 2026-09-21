from __future__ import annotations
from bot.timing import timed, job_timed, sql_timed, span, event

import hashlib
import os

from bot.redis_client import get_redis
from bot.runtime_config import document_queue_name, document_timeout_seconds


def _document_job_id(payload: dict) -> str:
    source = payload.get("analysis_job_id")
    if not source:
        source = "|".join([
            str(payload.get("chat_id") or ""),
            str(payload.get("placeholder_id") or ""),
            str(payload.get("title") or ""),
        ])
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:32]
    return f"document-{digest}"


@timed('document.enqueue')
def enqueue_document(payload: dict):
    try:
        from rq import Callback, Queue
    except ImportError as exc:
        raise RuntimeError("Async document mode requires the 'rq' package") from exc

    from bot.document_jobs import execute_document, handle_document_failure

    queue = Queue(document_queue_name(), connection=get_redis())
    return queue.enqueue(
        execute_document,
        payload,
        job_id=_document_job_id(payload),
        job_timeout=document_timeout_seconds(),
        ttl=int(os.environ.get("BOT_DOCUMENT_QUEUE_TTL_SECONDS", "3600")),
        result_ttl=int(os.environ.get("BOT_RESULT_TTL_SECONDS", "3600")),
        failure_ttl=int(os.environ.get("BOT_FAILURE_TTL_SECONDS", "604800")),
        on_failure=Callback(handle_document_failure, timeout=20),
        description=f"Feishu document {str(payload.get('title') or '')[:80]}",
    )
