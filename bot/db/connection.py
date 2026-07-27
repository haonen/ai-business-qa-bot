from __future__ import annotations

from pathlib import Path
import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL


BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")

_ENGINE: Engine | None = None


def get_engine() -> Engine:
    global _ENGINE
    if _ENGINE is None:
        url = URL.create(
            "mysql+pymysql",
            username=os.environ["MYSQL_USER"],
            password=os.environ["MYSQL_PASSWORD"],
            host=os.environ["MYSQL_HOST"],
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            database=os.environ["MYSQL_DATABASE"],
            query={"charset": "utf8mb4"},
        )
        _ENGINE = create_engine(url, pool_pre_ping=True)
    return _ENGINE


def fetch_df(sql: str, params: dict | None = None) -> pd.DataFrame:
    return pd.read_sql(text(sql), get_engine(), params=params or {})


def fetch_one(sql: str, params: dict | None = None) -> dict:
    with get_engine().connect() as conn:
        row = conn.execute(text(sql), params or {}).mappings().first()
    return dict(row) if row else {}

