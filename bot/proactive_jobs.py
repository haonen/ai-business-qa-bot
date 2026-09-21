from __future__ import annotations

import logging

from bot.messaging import create_lark_client, send_card, stable_uuid
from bot.proactive import (
    build_market_winner_card,
    get_campaign,
    get_recipient,
    update_campaign,
    validate_market_winner_card,
)
from bot.redis_client import get_redis


log = logging.getLogger(__name__)


def send_campaign(campaign_id: str) -> dict:
    redis = get_redis()
    lock = redis.lock(f"ai-bot:proactive:send-lock:{campaign_id}", timeout=60, blocking_timeout=5)
    if not lock.acquire(blocking=True):
        raise RuntimeError("could not acquire proactive send lock")
    try:
        campaign = get_campaign(campaign_id, redis=redis)
        if not campaign:
            raise RuntimeError(f"campaign not found: {campaign_id}")
        if campaign.status == "canceled":
            return {"ok": False, "status": "canceled"}
        if campaign.status == "sent" and campaign.message_id:
            return {"ok": True, "status": "sent", "message_id": campaign.message_id}
        recipient = get_recipient(campaign.recipient_code, redis=redis)
        if not recipient:
            update_campaign(campaign_id, redis=redis, status="failed", error="recipient expired")
            raise RuntimeError("recipient registration expired")
        update_campaign(campaign_id, redis=redis, status="sending", error=None)
        client = create_lark_client()
        card = build_market_winner_card(campaign)
        validate_market_winner_card(card)
        message_id = send_card(
            client,
            recipient.chat_id,
            card,
            uuid=stable_uuid("proactive", campaign_id),
        )
        update_campaign(campaign_id, redis=redis, status="sent", message_id=message_id, error=None)
        log.info("proactive campaign sent campaign=%s", campaign_id)
        return {"ok": True, "status": "sent", "message_id": message_id}
    except Exception as exc:
        try:
            update_campaign(campaign_id, redis=redis, status="failed", error=str(exc)[:300])
        except Exception:
            log.exception("failed to persist proactive send failure campaign=%s", campaign_id)
        raise
    finally:
        try:
            lock.release()
        except Exception:
            pass
