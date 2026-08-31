from __future__ import annotations

from dataclasses import dataclass
import os


TRUE_VALUES = {"1", "true", "yes", "on"}


def queue_enabled() -> bool:
    return os.environ.get("BOT_QUEUE_ENABLED", "0").strip().lower() in TRUE_VALUES


def async_documents_enabled() -> bool:
    return os.environ.get("BOT_ASYNC_DOCUMENTS", "1").strip().lower() in TRUE_VALUES


def inner_query_worker_limit() -> int:
    raw = os.environ.get("BOT_INNER_QUERY_WORKERS", "3").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("BOT_INNER_QUERY_WORKERS must be an integer from 1 to 8") from exc
    if not 1 <= value <= 8:
        raise ValueError("BOT_INNER_QUERY_WORKERS must be between 1 and 8")
    return value


def bounded_query_workers(requested: int) -> int:
    """Apply the process-wide limit to an individual query fan-out."""
    try:
        requested_count = int(requested)
    except (TypeError, ValueError) as exc:
        raise ValueError("requested query worker count must be a positive integer") from exc
    if requested_count < 1:
        raise ValueError("requested query worker count must be at least 1")
    return min(requested_count, inner_query_worker_limit())


@dataclass(frozen=True)
class WorkerSettings:
    cpu_count: int
    worker_count: int
    mysql_pool_size: int
    mysql_max_overflow: int
    db_connection_budget: int
    max_db_connection_budget: int
    burst_mode: bool


def resolve_worker_count(value: str | None = None, *, cpu_count: int | None = None) -> int:
    cpus = max(1, int(cpu_count or os.cpu_count() or 1))
    raw = (value if value is not None else os.environ.get("BOT_WORKER_COUNT", "auto")).strip().lower()
    if raw == "auto":
        return cpus
    try:
        count = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("BOT_WORKER_COUNT must be 'auto' or an integer from 1 to 16") from exc
    if not 1 <= count <= 16:
        raise ValueError("BOT_WORKER_COUNT must be between 1 and 16")
    return count


def worker_settings(
    value: str | None = None,
    *,
    cpu_count: int | None = None,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    max_db_budget: int | None = None,
) -> WorkerSettings:
    cpus = max(1, int(cpu_count or os.cpu_count() or 1))
    workers = resolve_worker_count(value, cpu_count=cpus)
    if workers > cpus * 2:
        raise ValueError(
            f"BOT_WORKER_COUNT={workers} exceeds the safety limit of 2x CPU cores ({cpus * 2})"
        )

    resolved_pool = int(pool_size if pool_size is not None else os.environ.get("MYSQL_POOL_SIZE", "3"))
    resolved_overflow = int(
        max_overflow if max_overflow is not None else os.environ.get("MYSQL_MAX_OVERFLOW", "1")
    )
    resolved_max_budget = int(
        max_db_budget
        if max_db_budget is not None
        else os.environ.get("BOT_MAX_DB_CONNECTION_BUDGET", "60")
    )
    if resolved_pool < 1 or resolved_overflow < 0 or resolved_max_budget < 1:
        raise ValueError("MySQL pool and connection-budget settings must be positive")
    budget = workers * (resolved_pool + resolved_overflow)
    if budget > resolved_max_budget:
        raise ValueError(
            f"worker configuration allows {budget} MySQL connections, exceeding "
            f"BOT_MAX_DB_CONNECTION_BUDGET={resolved_max_budget}"
        )
    return WorkerSettings(
        cpu_count=cpus,
        worker_count=workers,
        mysql_pool_size=resolved_pool,
        mysql_max_overflow=resolved_overflow,
        db_connection_budget=budget,
        max_db_connection_budget=resolved_max_budget,
        burst_mode=workers > cpus,
    )


def redis_url() -> str:
    value = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0").strip()
    if not value:
        raise ValueError("REDIS_URL cannot be empty")
    return value


def queue_name() -> str:
    return os.environ.get("BOT_QUEUE_NAME", "ai-bot").strip() or "ai-bot"


def document_queue_name() -> str:
    return os.environ.get("BOT_DOCUMENT_QUEUE_NAME", "ai-bot-documents").strip() or "ai-bot-documents"


def document_worker_count() -> int:
    raw = os.environ.get("BOT_DOCUMENT_WORKER_COUNT", "1").strip()
    try:
        count = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("BOT_DOCUMENT_WORKER_COUNT must be an integer from 1 to 4") from exc
    if not 1 <= count <= 4:
        raise ValueError("BOT_DOCUMENT_WORKER_COUNT must be between 1 and 4")
    return count


def document_timeout_seconds() -> int:
    raw = os.environ.get("BOT_DOCUMENT_TIMEOUT_SECONDS", "180").strip()
    try:
        timeout = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("BOT_DOCUMENT_TIMEOUT_SECONDS must be a positive integer") from exc
    if timeout < 1:
        raise ValueError("BOT_DOCUMENT_TIMEOUT_SECONDS must be a positive integer")
    return timeout
