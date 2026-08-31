from __future__ import annotations

import json
import sys

from bot.db.connection import fetch_one
from bot.redis_client import require_redis
from bot.runtime_config import (
    async_documents_enabled,
    document_queue_name,
    document_worker_count,
    inner_query_worker_limit,
    queue_enabled,
    queue_name,
    worker_settings,
)


def collect_health() -> tuple[dict, bool]:
    settings = worker_settings()
    report = {
        "queue_enabled": queue_enabled(),
        "async_documents_enabled": async_documents_enabled(),
        "cpu_count": settings.cpu_count,
        "worker_target": settings.worker_count,
        "inner_query_worker_limit": inner_query_worker_limit(),
        "mysql_connection_budget": settings.db_connection_budget,
        "mysql_connection_budget_limit": settings.max_db_connection_budget,
    }
    healthy = True
    try:
        redis = require_redis()
        from rq import Queue, Worker

        analysis_queue = Queue(queue_name(), connection=redis)
        document_queue = Queue(document_queue_name(), connection=redis)
        analysis_online = Worker.count(queue=analysis_queue)
        document_online = Worker.count(queue=document_queue)
        document_target = document_worker_count()
        report.update({
            "redis": "ok",
            "queue": analysis_queue.name,
            "queue_length": len(analysis_queue),
            "workers_online": analysis_online,
            "analysis_queue": {
                "name": analysis_queue.name,
                "length": len(analysis_queue),
                "worker_target": settings.worker_count,
                "workers_online": analysis_online,
            },
            "document_queue": {
                "name": document_queue.name,
                "length": len(document_queue),
                "worker_target": document_target,
                "workers_online": document_online,
            },
        })
        if queue_enabled() and analysis_online < settings.worker_count:
            healthy = False
            report["analysis_worker_error"] = "fewer online analysis workers than configured"
        if queue_enabled() and async_documents_enabled() and document_online < document_target:
            healthy = False
            report["document_worker_error"] = "fewer online document workers than configured"
    except Exception as exc:
        healthy = False
        report.update({"redis": "error", "redis_error": str(exc)})
    try:
        fetch_one("SELECT 1 AS ok")
        report["mysql"] = "ok"
    except Exception as exc:
        healthy = False
        report.update({"mysql": "error", "mysql_error": str(exc)})
    return report, healthy


def main() -> None:
    report, healthy = collect_health()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
