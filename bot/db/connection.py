from __future__ import annotations

from pathlib import Path
import logging
import os
import re
import time
from typing import Any

import pandas as pd
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")

_ENGINE: Any | None = None
log = logging.getLogger(__name__)


def get_engine():
    global _ENGINE
    if _ENGINE is None:
        from sqlalchemy import create_engine
        from sqlalchemy.engine import URL

        url = URL.create(
            "mysql+pymysql",
            username=os.environ["MYSQL_USER"],
            password=os.environ["MYSQL_PASSWORD"],
            host=os.environ["MYSQL_HOST"],
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            database=os.environ["MYSQL_DATABASE"],
            query={"charset": "utf8mb4"},
        )
        _ENGINE = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=int(os.environ.get("MYSQL_POOL_SIZE", "8")),
            max_overflow=int(os.environ.get("MYSQL_MAX_OVERFLOW", "4")),
            pool_recycle=int(os.environ.get("MYSQL_POOL_RECYCLE", "1800")),
            connect_args={
                "connect_timeout": int(os.environ.get("MYSQL_CONNECT_TIMEOUT", "10")),
                "read_timeout": int(os.environ.get("MYSQL_READ_TIMEOUT", "60")),
                "write_timeout": int(os.environ.get("MYSQL_WRITE_TIMEOUT", "30")),
            },
        )
    return _ENGINE


def fetch_df(sql: str, params: dict | None = None) -> pd.DataFrame:
    from sqlalchemy import text

    started = time.perf_counter()
    result = pd.read_sql(text(sql), get_engine(), params=params or {})
    elapsed = time.perf_counter() - started
    threshold = float(os.environ.get("MYSQL_SLOW_QUERY_SECONDS", "2"))
    if elapsed >= threshold:
        statement = re.sub(r"\s+", " ", sql).strip()[:180]
        log.warning(
            "[mysql] slow query elapsed=%.3fs rows=%s params=%s sql=%s",
            elapsed,
            len(result),
            sorted((params or {}).keys()),
            statement,
        )
    return result


def fetch_one(sql: str, params: dict | None = None) -> dict:
    from sqlalchemy import text

    with get_engine().connect() as conn:
        row = conn.execute(text(sql), params or {}).mappings().first()
    return dict(row) if row else {}
