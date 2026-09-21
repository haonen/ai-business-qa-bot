from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.redis_client import get_redis


log = logging.getLogger(__name__)
RECIPIENT_TTL_SECONDS = 30 * 24 * 60 * 60
CAMPAIGN_TTL_SECONDS = 90 * 24 * 60 * 60
RECIPIENT_INDEX = "ai-bot:proactive:recipients"
CAMPAIGN_INDEX = "ai-bot:proactive:campaigns"
SUPPORTED_TEMPLATE = "market-winner-demo"
SUPPORTED_PROMPT = "hanshu-douyin-last-week"


@dataclass(frozen=True)
class Recipient:
    code: str
    open_id: str
    chat_id: str
    registered_at: str
    expires_at: str


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    recipient_code: str
    template: str
    prompt_id: str
    timezone: str
    send_at: str
    period_start: str
    period_end: str
    status: str
    analysis_prompt: str | None = None
    job_id: str | None = None
    message_id: str | None = None
    error: str | None = None


def _recipient_key(code: str) -> str:
    return f"ai-bot:proactive:recipient:{code.upper()}"


def _campaign_key(campaign_id: str) -> str:
    return f"ai-bot:proactive:campaign:{campaign_id}"


def _text(value) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _text_dict(values: dict) -> dict[str, str]:
    return {_text(key): _text(value) for key, value in values.items()}


def recipient_code(open_id: str) -> str:
    digest = hashlib.sha256(open_id.encode("utf-8")).hexdigest()[:12].upper()
    return f"RCP-{digest}"


def register_recipient(open_id: str, chat_id: str, chat_type: str = "p2p", *, redis=None) -> Recipient:
    if chat_type != "p2p":
        raise ValueError("测试订阅只支持 Bot 私聊")
    if not open_id or not chat_id:
        raise ValueError("open_id and chat_id are required")
    redis = redis or get_redis()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=RECIPIENT_TTL_SECONDS)
    code = recipient_code(open_id)
    values = {
        "code": code,
        "open_id": open_id,
        "chat_id": chat_id,
        "registered_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    }
    key = _recipient_key(code)
    with redis.pipeline() as pipe:
        pipe.hset(key, mapping=values)
        pipe.expire(key, RECIPIENT_TTL_SECONDS)
        pipe.zadd(RECIPIENT_INDEX, {code: expires.timestamp()})
        pipe.execute()
    return Recipient(**values)


def unregister_recipient(open_id: str, *, redis=None) -> bool:
    redis = redis or get_redis()
    code = recipient_code(open_id)
    key = _recipient_key(code)
    with redis.pipeline() as pipe:
        pipe.delete(key)
        pipe.zrem(RECIPIENT_INDEX, code)
        deleted, _ = pipe.execute()
    return bool(deleted)


def get_recipient(code: str, *, redis=None) -> Recipient | None:
    redis = redis or get_redis()
    values = _text_dict(redis.hgetall(_recipient_key(code)))
    return Recipient(**values) if values else None


def list_recipients(*, redis=None) -> list[Recipient]:
    redis = redis or get_redis()
    now = datetime.now(timezone.utc).timestamp()
    redis.zremrangebyscore(RECIPIENT_INDEX, "-inf", now)
    result = []
    for raw_code in redis.zrange(RECIPIENT_INDEX, 0, -1):
        code = _text(raw_code)
        recipient = get_recipient(code, redis=redis)
        if recipient:
            result.append(recipient)
        else:
            redis.zrem(RECIPIENT_INDEX, code)
    return result


def parse_send_at(value: str, timezone_name: str) -> datetime:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区：{timezone_name}") from exc
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("send-at 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    else:
        parsed = parsed.astimezone(zone)
    return parsed


def previous_week(reference: datetime) -> tuple[date, date]:
    current_monday = reference.date() - timedelta(days=reference.weekday())
    end = current_monday - timedelta(days=1)
    return end - timedelta(days=6), end


def _campaign_from_values(values: dict) -> Campaign | None:
    if not values:
        return None
    values = _text_dict(values)
    return Campaign(
        campaign_id=values["campaign_id"],
        recipient_code=values["recipient_code"],
        template=values["template"],
        prompt_id=values["prompt_id"],
        timezone=values["timezone"],
        send_at=values["send_at"],
        period_start=values["period_start"],
        period_end=values["period_end"],
        status=values["status"],
        analysis_prompt=values.get("analysis_prompt") or None,
        job_id=values.get("job_id") or None,
        message_id=values.get("message_id") or None,
        error=values.get("error") or None,
    )


def get_campaign(campaign_id: str, *, redis=None) -> Campaign | None:
    redis = redis or get_redis()
    return _campaign_from_values(redis.hgetall(_campaign_key(campaign_id)))


def save_campaign(campaign: Campaign, *, redis=None) -> None:
    redis = redis or get_redis()
    values = {
        key: value
        for key, value in campaign.__dict__.items()
        if value is not None
    }
    key = _campaign_key(campaign.campaign_id)
    with redis.pipeline() as pipe:
        pipe.hset(key, mapping=values)
        missing_optional = [
            name for name in ("analysis_prompt", "job_id", "message_id", "error")
            if getattr(campaign, name) is None
        ]
        if missing_optional:
            pipe.hdel(key, *missing_optional)
        pipe.expire(key, CAMPAIGN_TTL_SECONDS)
        pipe.zadd(CAMPAIGN_INDEX, {campaign.campaign_id: datetime.fromisoformat(campaign.send_at).timestamp()})
        pipe.execute()


def update_campaign(campaign_id: str, *, redis=None, **changes) -> Campaign:
    redis = redis or get_redis()
    campaign = get_campaign(campaign_id, redis=redis)
    if not campaign:
        raise KeyError(f"campaign not found: {campaign_id}")
    values = campaign.__dict__.copy()
    values.update(changes)
    updated = Campaign(**values)
    save_campaign(updated, redis=redis)
    return updated


def list_campaigns(*, redis=None) -> list[Campaign]:
    redis = redis or get_redis()
    result = []
    for raw_campaign_id in redis.zrevrange(CAMPAIGN_INDEX, 0, -1):
        campaign_id = _text(raw_campaign_id)
        campaign = get_campaign(campaign_id, redis=redis)
        if campaign:
            result.append(campaign)
        else:
            redis.zrem(CAMPAIGN_INDEX, campaign_id)
    return result


def schedule_campaign(
    recipient: str,
    send_at: str,
    timezone_name: str = "Asia/Shanghai",
    template: str = SUPPORTED_TEMPLATE,
    *,
    analysis_prompt: str | None = None,
    redis=None,
    now: datetime | None = None,
) -> Campaign:
    if template != SUPPORTED_TEMPLATE:
        raise ValueError(f"不支持的模板：{template}")
    redis = redis or get_redis()
    recipient_record = get_recipient(recipient, redis=redis)
    if not recipient_record:
        raise ValueError(f"收件人不存在或登记已过期：{recipient}")
    scheduled = parse_send_at(send_at, timezone_name)
    current = now or datetime.now(timezone.utc)
    if scheduled.astimezone(timezone.utc) <= current.astimezone(timezone.utc):
        raise ValueError("预约时间必须晚于当前时间")
    period_start, period_end = previous_week(scheduled)
    resolved_prompt = _resolve_analysis_prompt(analysis_prompt, period_start, period_end)
    campaign_id = secrets.token_hex(8)
    job_id = f"proactive-{campaign_id}"
    campaign = Campaign(
        campaign_id=campaign_id,
        recipient_code=recipient_record.code,
        template=template,
        prompt_id=SUPPORTED_PROMPT,
        timezone=timezone_name,
        send_at=scheduled.isoformat(),
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        status="scheduled",
        analysis_prompt=resolved_prompt,
        job_id=job_id,
    )
    save_campaign(campaign, redis=redis)
    try:
        from rq import Queue, Retry
        from bot.proactive_jobs import send_campaign
        from bot.runtime_config import proactive_queue_name

        queue = Queue(proactive_queue_name(), connection=redis)
        queue.enqueue_at(
            scheduled.astimezone(timezone.utc),
            send_campaign,
            campaign_id,
            job_id=job_id,
            job_timeout=60,
            result_ttl=CAMPAIGN_TTL_SECONDS,
            failure_ttl=CAMPAIGN_TTL_SECONDS,
            retry=Retry(max=3, interval=[10, 30, 60]),
            description=f"Proactive card {campaign_id}",
        )
    except Exception:
        redis.delete(_campaign_key(campaign_id))
        redis.zrem(CAMPAIGN_INDEX, campaign_id)
        raise
    return campaign


def create_immediate_campaign(
    recipient: str,
    timezone_name: str = "Asia/Shanghai",
    template: str = SUPPORTED_TEMPLATE,
    *,
    analysis_prompt: str | None = None,
    redis=None,
    now: datetime | None = None,
) -> Campaign:
    if template != SUPPORTED_TEMPLATE:
        raise ValueError(f"不支持的模板：{template}")
    redis = redis or get_redis()
    recipient_record = get_recipient(recipient, redis=redis)
    if not recipient_record:
        raise ValueError(f"收件人不存在或登记已过期：{recipient}")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区：{timezone_name}") from exc
    instant = (now or datetime.now(timezone.utc)).astimezone(zone)
    period_start, period_end = previous_week(instant)
    resolved_prompt = _resolve_analysis_prompt(analysis_prompt, period_start, period_end)
    campaign_id = secrets.token_hex(8)
    campaign = Campaign(
        campaign_id=campaign_id,
        recipient_code=recipient_record.code,
        template=template,
        prompt_id=SUPPORTED_PROMPT,
        timezone=timezone_name,
        send_at=instant.isoformat(),
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        status="scheduled",
        analysis_prompt=resolved_prompt,
    )
    save_campaign(campaign, redis=redis)
    return campaign


def cancel_campaign(campaign_id: str, *, redis=None) -> Campaign:
    redis = redis or get_redis()
    campaign = get_campaign(campaign_id, redis=redis)
    if not campaign:
        raise ValueError(f"预约不存在：{campaign_id}")
    if campaign.status != "scheduled":
        raise ValueError(f"只有 scheduled 状态可取消，当前状态：{campaign.status}")
    try:
        from rq import Queue
        from rq.job import Job
        from rq.registry import ScheduledJobRegistry
        from bot.runtime_config import proactive_queue_name

        job = Job.fetch(campaign.job_id, connection=redis)
        queue = Queue(proactive_queue_name(), connection=redis)
        ScheduledJobRegistry(queue=queue).remove(job, delete_job=False)
        job.cancel()
    except Exception as exc:
        log.warning("scheduled job cancel fallback campaign=%s error=%s", campaign_id, exc)
    return update_campaign(campaign_id, redis=redis, status="canceled")


def build_market_winner_card(campaign: Campaign) -> dict:
    callback_value = {
        "v": 1,
        "action": "run_analysis",
        "campaign_id": campaign.campaign_id,
        "prompt_id": campaign.prompt_id,
    }
    return {
        "schema": "2.0",
        "config": {
            "width_mode": "default",
            "enable_forward": False,
            "summary": {"content": "今日市场速报 · 链路测试（演示数据）"},
            "style": {
                "text_size": {
                    "focus": {"default": "heading-1", "pc": "heading-1", "mobile": "heading-2"},
                    "caption": {"default": "notation", "pc": "notation", "mobile": "notation"},
                }
            },
        },
        "header": {
            "title": {"tag": "plain_text", "content": "今日市场速报 · 链路测试"},
            "subtitle": {"tag": "plain_text", "content": "看看谁跑赢了市场"},
            "template": "blue",
            "icon": {"tag": "standard_icon", "token": "chart_colorful"},
            "text_tag_list": [
                {"tag": "text_tag", "text": {"tag": "plain_text", "content": "演示数据"}, "color": "blue"}
            ],
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "vertical_spacing": "12px",
            "elements": [
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "blue-50",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "padding": "12px",
                            "vertical_spacing": "4px",
                            "elements": [
                                {"tag": "markdown", "content": "<font color='grey'>跑赢市场品牌</font>", "text_size": "caption"},
                                {"tag": "markdown", "content": "<font color='blue'>**韩束**</font>", "text_size": "focus"},
                                {"tag": "markdown", "content": "增长最快渠道：**抖音**"},
                            ],
                        }
                    ],
                },
                {
                    "tag": "markdown",
                    "content": (
                        f"**观察周期**  {campaign.period_start} 至 {campaign.period_end}\n"
                        "<font color='grey'>本卡片仅用于链路测试，以上结论为演示内容，不代表真实业务排名。</font>"
                    ),
                },
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "grey-50",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "padding": "12px",
                            "vertical_spacing": "8px",
                            "elements": [
                                {
                                    "tag": "markdown",
                                    "content": f"**推荐追问**\n{build_analysis_prompt(campaign)}",
                                },
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "立即分析增长原因"},
                                    "type": "primary_filled",
                                    "width": "fill",
                                    "behaviors": [{"type": "callback", "value": callback_value}],
                                },
                            ],
                        }
                    ],
                },
            ],
        },
    }


def validate_market_winner_card(card: dict) -> None:
    if card.get("schema") != "2.0":
        raise ValueError("proactive card must use Card 2.0")
    elements = (card.get("body") or {}).get("elements") or []
    if len(elements) != 3:
        raise ValueError("proactive card must contain exactly three visual blocks")
    serialized = json.dumps(card, ensure_ascii=False)
    for required in ("演示数据", "不代表真实业务排名", "韩束", "抖音"):
        if required not in serialized:
            raise ValueError(f"proactive card is missing required content: {required}")
    buttons = []
    stack = list(elements)
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        if current.get("tag") == "button":
            buttons.append(current)
        stack.extend(current.get("elements") or [])
        stack.extend(current.get("columns") or [])
    if len(buttons) != 1:
        raise ValueError("proactive card must contain exactly one primary action")
    behaviors = buttons[0].get("behaviors") or []
    callback = next((item for item in behaviors if item.get("type") == "callback"), None)
    value = (callback or {}).get("value") or {}
    if set(value) != {"v", "action", "campaign_id", "prompt_id"}:
        raise ValueError("proactive card callback must contain server-side references only")


def build_analysis_prompt(campaign: Campaign) -> str:
    if campaign.analysis_prompt:
        return campaign.analysis_prompt
    if campaign.prompt_id != SUPPORTED_PROMPT:
        raise ValueError("未知问题模板")
    return _resolve_analysis_prompt(None, campaign.period_start, campaign.period_end)


def _resolve_analysis_prompt(
    analysis_prompt: str | None,
    period_start: date | str,
    period_end: date | str,
) -> str:
    start = period_start.isoformat() if isinstance(period_start, date) else str(period_start)
    end = period_end.isoformat() if isinstance(period_end, date) else str(period_end)
    if analysis_prompt is None:
        prompt = (
            f"生成韩束{start}至{end}抖音生意分析报告。"
            "重点解释销售增长原因，结合现有店铺、货品、价格、流量及媒介数据拆解，"
            "并注明数据截止时间和证据不足项。"
        )
    else:
        prompt = str(analysis_prompt).strip().replace("{period_start}", start).replace("{period_end}", end)
    if not prompt:
        raise ValueError("卡片对应的自然语言问题不能为空")
    if len(prompt) > 4000:
        raise ValueError("卡片对应的自然语言问题不能超过4000字")
    return prompt


def parse_action_value(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
