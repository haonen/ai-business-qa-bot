from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
import shutil
import sys

from bot.redis_client import require_redis
from bot.runtime_config import (
    document_queue_name,
    document_worker_count,
    inner_query_worker_limit,
    queue_name,
    redis_url,
    worker_settings,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PoolSettings:
    role: str
    queue: str
    worker_count: int
    cpu_count: int | None = None
    burst_mode: bool = False
    db_connection_budget: int | None = None
    max_db_connection_budget: int | None = None


def build_worker_command(role: str | None = None) -> tuple[list[str], PoolSettings]:
    resolved_role = (role or os.environ.get("BOT_WORKER_ROLE", "analysis")).strip().lower()
    if resolved_role == "analysis":
        analysis = worker_settings()
        inner_query_worker_limit()  # Reject invalid fan-out configuration before forking workers.
        settings = PoolSettings(
            role="analysis",
            queue=queue_name(),
            worker_count=analysis.worker_count,
            cpu_count=analysis.cpu_count,
            burst_mode=analysis.burst_mode,
            db_connection_budget=analysis.db_connection_budget,
            max_db_connection_budget=analysis.max_db_connection_budget,
        )
    elif resolved_role == "document":
        settings = PoolSettings(
            role="document",
            queue=document_queue_name(),
            worker_count=document_worker_count(),
        )
    else:
        raise ValueError("BOT_WORKER_ROLE must be 'analysis' or 'document'")

    venv_rq = Path(sys.executable).with_name("rq")
    rq_executable = str(venv_rq) if venv_rq.exists() else shutil.which("rq")
    if not rq_executable:
        raise RuntimeError("Cannot find the RQ executable in the active virtual environment")
    if not Path(rq_executable).exists():
        raise RuntimeError("Cannot find the RQ executable in the active virtual environment")
    command = [
        rq_executable,
        "worker-pool",
        settings.queue,
        "--num-workers",
        str(settings.worker_count),
        "--url",
        redis_url(),
    ]
    return command, settings


def main() -> None:
    require_redis()
    command, settings = build_worker_command()
    # Workers may be started before the receiver rollout flag is enabled. They
    # must still persist sessions in Redis once queued jobs arrive.
    os.environ["BOT_SESSION_BACKEND"] = "redis"
    log.info(
        "starting worker pool role=%s queue=%s cpu=%s workers=%s burst=%s mysql_budget=%s/%s",
        settings.role,
        settings.queue,
        settings.cpu_count,
        settings.worker_count,
        settings.burst_mode,
        settings.db_connection_budget,
        settings.max_db_connection_budget,
    )
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
