from __future__ import annotations

import hashlib
import logging
import os
import time

from bot.redis_client import get_redis
from bot.runtime_config import queue_name


log = logging.getLogger(__name__)
TERMINAL_STATUSES = {"finished", "failed", "stopped", "canceled"}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def job_id_for_message(message_id: str) -> str:
    return f"msg-{_digest(message_id)[:32]}"


def _status_name(status) -> str:
    return str(getattr(status, "value", status)).lower()


def claim_recent_key(namespace: str, value: str, ttl_seconds: int) -> bool:
    redis = get_redis()
    key = f"ai-bot:dedupe:{namespace}:{_digest(value)}"
    return bool(redis.set(key, str(time.time()), nx=True, ex=ttl_seconds))


def release_recent_key(namespace: str, value: str) -> None:
    get_redis().delete(f"ai-bot:dedupe:{namespace}:{_digest(value)}")


def enqueue_request(payload: dict):
    try:
        from rq import Callback, Queue
        from rq.job import Dependency, Job
    except ImportError as exc:
        raise RuntimeError("Queue mode requires the 'rq' package") from exc

    from bot.jobs import execute_request, handle_job_failure

    redis = get_redis()
    queue = Queue(queue_name(), connection=redis)
    open_id = payload["open_id"]
    user_key = f"ai-bot:user-last-job:{_digest(open_id)}"
    lock = redis.lock(f"{user_key}:lock", timeout=5, blocking_timeout=2)
    if not lock.acquire(blocking=True):
        raise RuntimeError("Could not acquire the per-user enqueue lock")
    try:
        dependency = None
        previous_id = redis.get(user_key)
        if isinstance(previous_id, bytes):
            previous_id = previous_id.decode("utf-8")
        if previous_id:
            try:
                previous = Job.fetch(previous_id, connection=redis)
                status = _status_name(previous.get_status(refresh=True))
                if status not in TERMINAL_STATUSES:
                    dependency = Dependency(jobs=[previous], allow_failure=True)
            except Exception:
                log.warning("discarding missing previous user job id=%s", previous_id)

        job = queue.enqueue(
            execute_request,
            payload,
            job_id=payload["job_id"],
            depends_on=dependency,
            job_timeout=int(os.environ.get("BOT_JOB_TIMEOUT_SECONDS", "300")),
            ttl=int(os.environ.get("BOT_QUEUE_TTL_SECONDS", "3600")),
            result_ttl=int(os.environ.get("BOT_RESULT_TTL_SECONDS", "3600")),
            failure_ttl=int(os.environ.get("BOT_FAILURE_TTL_SECONDS", "604800")),
            on_failure=Callback(handle_job_failure, timeout=10),
            description=f"AI business QA request {payload['job_id'][-8:]}",
        )
        redis.set(user_key, job.id, ex=86400)
        return job
    finally:
        try:
            lock.release()
        except Exception:
            pass
