from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from typing import Any


log = logging.getLogger(__name__)


def audit_enabled() -> bool:
    return os.environ.get("BOT_REQUEST_AUDIT_ENABLED", "0") == "1"


def _salt() -> str:
    return os.environ.get("BOT_AUDIT_HASH_SALT") or os.environ.get("FEISHU_APP_ID") or "ai-business-qa-bot"


def _ref(value: str) -> str:
    return hashlib.sha256(f"{_salt()}\0{value}".encode("utf-8")).hexdigest()


def _question_hash(value: str) -> str:
    normalized = " ".join(str(value or "").split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _received_at(value) -> datetime:
    try:
        number = int(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).replace(tzinfo=None)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def record_received(
    *, message_id: str, chat_id: str, open_id: str, user_text: str, create_time=None,
) -> None:
    if not audit_enabled():
        return
    try:
        from sqlalchemy import text
        from bot.db.connection import get_engine

        with get_engine().begin() as connection:
            connection.execute(text("""
                INSERT INTO ai_bot_request_audit (
                    message_ref, chat_ref, user_ref, question_hash,
                    question_text, received_at, status
                ) VALUES (
                    :message_ref, :chat_ref, :user_ref, :question_hash,
                    :question_text, :received_at, 'received'
                )
                ON DUPLICATE KEY UPDATE
                    question_text = VALUES(question_text),
                    question_hash = VALUES(question_hash),
                    received_at = LEAST(received_at, VALUES(received_at))
            """), {
                "message_ref": _ref(message_id),
                "chat_ref": _ref(chat_id),
                "user_ref": _ref(open_id),
                "question_hash": _question_hash(user_text),
                "question_text": user_text,
                "received_at": _received_at(create_time),
            })
    except Exception as exc:
        log.warning("[request_audit] received write failed: %s", exc)


def outcome_fields(result: dict | None) -> dict:
    result = result or {}
    route = result.get("route") or {}
    decision = route.get("route_decision") or {}
    trace = decision.get("trace") or {}
    commerce = decision.get("commerce_scope") or {}
    media = decision.get("media_scope") or {}
    meta = result.get("meta") or {}
    return {
        "route_type": result.get("route_type") or route.get("type"),
        "route_action": decision.get("action") or trace.get("final_action") or result.get("route_type"),
        "route_version": trace.get("router_version"),
        "decision_source": decision.get("decision_source"),
        "question_mode": decision.get("question_mode") or route.get("question_mode"),
        "intents": decision.get("intents"),
        "brand_surface": decision.get("brand_surface") or route.get("brand"),
        "period_text": decision.get("period") or route.get("period"),
        "commerce_platform": next(iter(commerce.get("platforms") or []), route.get("platform")),
        "media_mode": media.get("mode") or route.get("media_mode"),
        "missing_slots": decision.get("missing_slots") or trace.get("missing_parameters") or [],
        "route_decision": decision or None,
        "document_ready": meta.get("document_ready"),
    }


def record_outcome(
    *, message_id: str, result: dict | None, elapsed_ms: int,
    status: str = "completed", error_message: str | None = None,
) -> None:
    if not audit_enabled() or not message_id:
        return
    try:
        from sqlalchemy import text
        from bot.db.connection import get_engine

        fields = outcome_fields(result)
        with get_engine().begin() as connection:
            connection.execute(text("""
                UPDATE ai_bot_request_audit SET
                    status = :status,
                    route_type = :route_type,
                    route_action = :route_action,
                    route_version = :route_version,
                    decision_source = :decision_source,
                    question_mode = :question_mode,
                    intents = :intents,
                    brand_surface = :brand_surface,
                    period_text = :period_text,
                    commerce_platform = :commerce_platform,
                    media_mode = :media_mode,
                    missing_slots = :missing_slots,
                    route_decision = :route_decision,
                    document_ready = :document_ready,
                    elapsed_ms = :elapsed_ms,
                    error_message = :error_message,
                    completed_at = UTC_TIMESTAMP(3)
                WHERE message_ref = :message_ref
            """), {
                **fields,
                "message_ref": _ref(message_id),
                "status": status,
                "intents": _json(fields["intents"]),
                "missing_slots": _json(fields["missing_slots"]),
                "route_decision": _json(fields["route_decision"]),
                "elapsed_ms": max(0, int(elapsed_ms)),
                "error_message": str(error_message or "")[:4000] or None,
            })
    except Exception as exc:
        log.warning("[request_audit] outcome write failed: %s", exc)
