from __future__ import annotations

import hashlib
import json

from bot.proactive import build_analysis_prompt, get_campaign, get_recipient
from bot.redis_client import get_redis
from bot.task_queue import claim_recent_key, enqueue_request, job_id_for_message, release_recent_key


def _read(value, *names, default=None):
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _action_value(event) -> dict:
    action = _read(event, "action", default={}) or {}
    value = _read(action, "value", default={})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _event_id(event, action_value: dict) -> str:
    raw = _read(event, "raw", default={}) or {}
    header = _read(raw, "header", default={}) or {}
    explicit = (
        _read(event, "event_id", "eventId")
        or _read(header, "event_id", "eventId")
        or _read(raw, "event_id", "eventId")
    )
    if explicit:
        return str(explicit)
    operator = _read(event, "operator", default={}) or {}
    source = json.dumps(
        {
            "message_id": _read(event, "message_id", "messageId"),
            "chat_id": _read(event, "chat_id", "chatId"),
            "operator": _read(operator, "open_id", "openId"),
            "action": action_value,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def handle_card_action(event, *, redis=None) -> dict:
    redis = redis or get_redis()
    root_event = event
    event = _read(root_event, "event", default=root_event) or root_event
    value = _action_value(event)
    if value.get("v") != 1 or value.get("action") != "run_analysis":
        return {"toast": {"type": "warning", "content": "无法识别这个操作，请刷新后重试。"}}

    campaign = get_campaign(str(value.get("campaign_id") or ""), redis=redis)
    if not campaign or campaign.status != "sent":
        return {"toast": {"type": "warning", "content": "这张测试卡片已失效。"}}
    if value.get("prompt_id") != campaign.prompt_id:
        return {"toast": {"type": "warning", "content": "问题模板校验失败。"}}

    recipient = get_recipient(campaign.recipient_code, redis=redis)
    operator = _read(event, "operator", default={}) or {}
    operator_id = str(_read(operator, "open_id", "openId", default="") or "")
    context = _read(event, "context", default={}) or {}
    chat_id = str(
        _read(event, "chat_id", "chatId")
        or _read(context, "open_chat_id", "openChatId", default="")
        or ""
    )
    message_id = str(
        _read(event, "message_id", "messageId")
        or _read(context, "open_message_id", "openMessageId", default="")
        or ""
    )
    if (
        not recipient
        or recipient.open_id != operator_id
        or recipient.chat_id != chat_id
        or campaign.message_id != message_id
    ):
        return {"toast": {"type": "warning", "content": "你不是这张测试卡片的指定收件人。"}}

    event_id = _event_id(root_event, value)
    delivery_key = f"card-delivery:{event_id}"
    action_key = f"card-action:{campaign.campaign_id}:{operator_id}:{campaign.prompt_id}"
    if not claim_recent_key("card_delivery", delivery_key, 86400, redis=redis):
        return {"toast": {"type": "info", "content": "这个分析请求已经提交。"}}
    if not claim_recent_key("card_action", action_key, 90 * 24 * 60 * 60, redis=redis):
        return {"toast": {"type": "info", "content": "这个分析请求已经提交。"}}
    try:
        enqueue_request({
            "job_id": job_id_for_message(action_key),
            "message_id": action_key,
            "open_id": recipient.open_id,
            "chat_id": recipient.chat_id,
            "user_text": build_analysis_prompt(campaign),
            "placeholder_id": None,
            "campaign_id": campaign.campaign_id,
        }, redis=redis)
    except Exception:
        release_recent_key("card_delivery", delivery_key, redis=redis)
        release_recent_key("card_action", action_key, redis=redis)
        raise
    return {"toast": {"type": "success", "content": "已开始分析，结果会发送到当前私聊。"}}
