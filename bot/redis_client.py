from __future__ import annotations

from typing import Any

from bot.runtime_config import redis_url


_CLIENT: Any | None = None


def get_redis():
    global _CLIENT
    if _CLIENT is None:
        try:
            from redis import Redis
        except ImportError as exc:
            raise RuntimeError("Redis support requires the 'redis' package") from exc
        _CLIENT = Redis.from_url(
            redis_url(),
            socket_connect_timeout=2,
            socket_timeout=5,
            health_check_interval=30,
        )
    return _CLIENT


def require_redis():
    client = get_redis()
    client.ping()
    return client


def set_redis_for_tests(client) -> None:
    global _CLIENT
    _CLIENT = client

