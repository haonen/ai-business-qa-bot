from __future__ import annotations

import logging
import os
import time
import hashlib

from bot.messaging import create_lark_client, send_reply, update_text
from bot.request_processor import process_request


log = logging.getLogger(__name__)


def _payload_from_job(job) -> dict:
    if getattr(job, "args", None) and isinstance(job.args[0], dict):
        return job.args[0]
    return {}


def execute_request(payload: dict) -> dict:
    try:
        from rq import get_current_job
    except ImportError as exc:
        raise RuntimeError("Queue workers require the 'rq' package") from exc

    job = get_current_job()
    client = create_lark_client()
    min_interval = float(os.environ.get("BOT_PROGRESS_MIN_INTERVAL_SECONDS", "2"))
    last_update_at = 0.0
    last_stage = ""

    def on_progress(stage_text: str) -> None:
        nonlocal last_update_at, last_stage
        now = time.monotonic()
        if job is not None:
            job.meta.update({"stage": stage_text, "stage_updated_at": time.time()})
            job.save_meta()
        if stage_text == last_stage or now - last_update_at < min_interval:
            return
        update_text(client, payload.get("placeholder_id"), stage_text)
        last_stage = stage_text
        last_update_at = now

    started = time.perf_counter()
    user_ref = hashlib.sha256(payload["open_id"].encode("utf-8")).hexdigest()[:10]
    queue_wait = None
    if job is not None and getattr(job, "enqueued_at", None):
        queue_wait = max(0.0, time.time() - job.enqueued_at.timestamp())
    log.info(
        "job started job_id=%s user=%s queue_wait_seconds=%s",
        getattr(job, "id", payload.get("job_id")),
        user_ref,
        round(queue_wait, 3) if queue_wait is not None else None,
    )
    if job is not None:
        job.meta.update({"stage": "started", "started_at": time.time()})
        job.save_meta()
    try:
        result = process_request(
            client,
            open_id=payload["open_id"],
            chat_id=payload["chat_id"],
            user_text=payload["user_text"],
            placeholder_id=payload.get("placeholder_id"),
            on_progress=on_progress,
            analysis_job_id=getattr(job, "id", None) or payload.get("job_id"),
            message_id=payload.get("message_id"),
        )
        if job is not None:
            job.meta.update({
                "stage": "finished",
                "finished_at": time.time(),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            })
            job.save_meta()
        log.info(
            "job finished job_id=%s user=%s elapsed_seconds=%.3f route=%s",
            getattr(job, "id", payload.get("job_id")),
            user_ref,
            time.perf_counter() - started,
            result.get("route_type"),
        )
        return {"route_type": result.get("route_type"), "ok": True}
    except Exception:
        log.exception("queued request failed job_id=%s", getattr(job, "id", None))
        raise


def handle_job_failure(job, connection, exc_type, exc_value, traceback, *args, **kwargs) -> None:
    payload = _payload_from_job(job)
    placeholder_id = payload.get("placeholder_id")
    if not placeholder_id and not payload.get("chat_id"):
        return
    try:
        client = create_lark_client()
        short_id = str(getattr(job, "id", "unknown"))[-8:]
        message = f"分析任务未能完成，请稍后重试。任务编号：{short_id}"
        if placeholder_id:
            update_text(client, placeholder_id, message)
        else:
            send_reply(client, payload["chat_id"], message)
    except Exception:
        log.exception("failed to notify user about job failure job_id=%s", getattr(job, "id", None))
