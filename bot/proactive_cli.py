from __future__ import annotations

import argparse
import json
import sys

from bot.proactive import (
    SUPPORTED_TEMPLATE,
    cancel_campaign,
    create_immediate_campaign,
    list_campaigns,
    list_recipients,
    schedule_campaign,
)
from bot.proactive_jobs import send_campaign
from bot.redis_client import require_redis


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage proactive Bot card tests")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("recipients", help="List active test recipients")

    schedule = commands.add_parser("schedule", help="Schedule a one-shot card")
    schedule.add_argument("--recipient", required=True, help="Recipient code, for example RCP-1234ABCD")
    schedule.add_argument("--send-at", required=True, help="ISO 8601 local or offset-aware datetime")
    schedule.add_argument("--timezone", default="Asia/Shanghai")
    schedule.add_argument("--template", default=SUPPORTED_TEMPLATE)
    schedule.add_argument("--question", help="Bot问题；支持{period_start}和{period_end}")

    send_now = commands.add_parser("send-now", help="Send a test card immediately")
    send_now.add_argument("--recipient", required=True)
    send_now.add_argument("--timezone", default="Asia/Shanghai")
    send_now.add_argument("--template", default=SUPPORTED_TEMPLATE)
    send_now.add_argument("--question", help="Bot问题；支持{period_start}和{period_end}")

    commands.add_parser("schedules", help="List recent schedules")
    cancel = commands.add_parser("cancel", help="Cancel a scheduled card")
    cancel.add_argument("--job-id", required=True, help="Campaign id or proactive-<campaign id>")
    return parser


def _campaign_json(campaign) -> dict:
    return {key: value for key, value in campaign.__dict__.items() if value is not None}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    redis = require_redis()
    if args.command == "recipients":
        data = [
            {
                "code": item.code,
                "registered_at": item.registered_at,
                "expires_at": item.expires_at,
            }
            for item in list_recipients(redis=redis)
        ]
    elif args.command == "schedule":
        data = _campaign_json(
            schedule_campaign(
                args.recipient,
                args.send_at,
                args.timezone,
                args.template,
                analysis_prompt=args.question,
                redis=redis,
            )
        )
    elif args.command == "send-now":
        campaign = create_immediate_campaign(
            args.recipient,
            args.timezone,
            args.template,
            analysis_prompt=args.question,
            redis=redis,
        )
        data = send_campaign(campaign.campaign_id)
        data["campaign_id"] = campaign.campaign_id
    elif args.command == "schedules":
        data = [_campaign_json(item) for item in list_campaigns(redis=redis)]
    else:
        campaign_id = args.job_id.removeprefix("proactive-")
        data = _campaign_json(cancel_campaign(campaign_id, redis=redis))
    print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
