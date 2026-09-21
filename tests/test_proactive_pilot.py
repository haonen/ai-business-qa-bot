from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from bot.card_actions import handle_card_action
from bot.proactive import (
    Campaign,
    build_analysis_prompt,
    build_market_winner_card,
    create_immediate_campaign,
    get_recipient,
    parse_send_at,
    previous_week,
    register_recipient,
    save_campaign,
    schedule_campaign,
    unregister_recipient,
    validate_market_winner_card,
)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self
        return queue

    def execute(self):
        results = []
        for name, args, kwargs in self.calls:
            results.append(getattr(self.redis, name)(*args, **kwargs))
        return results


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.zsets = {}
        self.values = {}

    def pipeline(self):
        return FakePipeline(self)

    def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update({str(k): str(v) for k, v in mapping.items()})
        return len(mapping)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, *names):
        removed = 0
        for name in names:
            if name in self.hashes.get(key, {}):
                removed += 1
                del self.hashes[key][name]
        return removed

    def expire(self, key, seconds):
        return key in self.hashes

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zrem(self, key, member):
        return int(self.zsets.get(key, {}).pop(member, None) is not None)

    def zrange(self, key, start, end):
        items = sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1])
        values = [item[0] for item in items]
        return values[start:] if end == -1 else values[start:end + 1]

    def zrevrange(self, key, start, end):
        return list(reversed(self.zrange(key, 0, -1)))

    def zremrangebyscore(self, key, minimum, maximum):
        maximum = float(maximum)
        members = [member for member, score in self.zsets.get(key, {}).items() if score <= maximum]
        for member in members:
            self.zsets[key].pop(member, None)
        return len(members)

    def delete(self, key):
        removed = int(key in self.hashes or key in self.values)
        self.hashes.pop(key, None)
        self.values.pop(key, None)
        return removed

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = str(value)
        return True


def sent_campaign(recipient_code: str) -> Campaign:
    return Campaign(
        campaign_id="campaign-1",
        recipient_code=recipient_code,
        template="market-winner-demo",
        prompt_id="hanshu-douyin-last-week",
        timezone="Asia/Shanghai",
        send_at="2026-09-04T16:30:00+08:00",
        period_start="2026-08-24",
        period_end="2026-08-30",
        status="sent",
        analysis_prompt="请分析韩束2026-08-24至2026-08-30的抖音生意，并解释增长原因。",
        message_id="om-card",
    )


class ProactivePilotTest(unittest.TestCase):
    def test_registration_is_stable_and_unsubscribe_removes_record(self):
        redis = FakeRedis()
        first = register_recipient("ou-user", "oc-chat", redis=redis)
        second = register_recipient("ou-user", "oc-chat", redis=redis)
        self.assertEqual(first.code, second.code)
        self.assertEqual(get_recipient(first.code, redis=redis).chat_id, "oc-chat")
        self.assertTrue(unregister_recipient("ou-user", redis=redis))
        self.assertIsNone(get_recipient(first.code, redis=redis))

    def test_registration_rejects_groups(self):
        with self.assertRaisesRegex(ValueError, "只支持"):
            register_recipient("ou-user", "oc-chat", "group", redis=FakeRedis())

    def test_previous_week_is_fixed_monday_to_sunday(self):
        current = parse_send_at("2026-09-04T16:30:00", "Asia/Shanghai")
        self.assertEqual(previous_week(current), (
            datetime(2026, 8, 24).date(),
            datetime(2026, 8, 30).date(),
        ))

    def test_schedule_rejects_past_time_and_bad_timezone(self):
        redis = FakeRedis()
        recipient = register_recipient("ou-user", "oc-chat", redis=redis)
        with self.assertRaisesRegex(ValueError, "晚于"):
            schedule_campaign(
                recipient.code,
                "2026-09-04T10:00:00",
                redis=redis,
                now=datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc),
            )
        with self.assertRaisesRegex(ValueError, "未知时区"):
            parse_send_at("2026-09-04T10:00:00", "Mars/Olympus")

    @patch("rq.Queue")
    @patch("bot.proactive.secrets.token_hex", return_value="0123456789abcdef")
    def test_schedule_uses_durable_rq_scheduled_job(self, _, queue_type):
        redis = FakeRedis()
        recipient = register_recipient("ou-user", "oc-chat", redis=redis)
        queue = queue_type.return_value
        campaign = schedule_campaign(
            recipient.code,
            "2026-09-04T16:30:00",
            redis=redis,
            now=datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(campaign.job_id, "proactive-0123456789abcdef")
        queue.enqueue_at.assert_called_once()

    def test_card_is_v2_labeled_demo_and_contains_only_references(self):
        card = build_market_winner_card(sent_campaign("RCP-12345678"))
        self.assertEqual(card["schema"], "2.0")
        serialized = str(card)
        self.assertIn("演示数据", serialized)
        self.assertIn("立即分析增长原因", serialized)
        self.assertEqual(len(card["body"]["elements"]), 3)
        button = card["body"]["elements"][-1]["columns"][0]["elements"][-1]
        value = button["behaviors"][0]["value"]
        self.assertEqual(set(value), {"v", "action", "campaign_id", "prompt_id"})
        self.assertNotIn("韩束为什么", str(value))
        validate_market_winner_card(card)

    @patch("bot.card_actions.enqueue_request")
    def test_card_action_validates_and_deduplicates(self, enqueue):
        redis = FakeRedis()
        recipient = register_recipient("ou-user", "oc-chat", redis=redis)
        save_campaign(sent_campaign(recipient.code), redis=redis)
        event = {
            "message_id": "om-card",
            "chat_id": "oc-chat",
            "operator": {"open_id": "ou-user"},
            "action": {
                "value": {
                    "v": 1,
                    "action": "run_analysis",
                    "campaign_id": "campaign-1",
                    "prompt_id": "hanshu-douyin-last-week",
                }
            },
            "raw": {"header": {"event_id": "evt-1"}},
        }
        response = handle_card_action(event, redis=redis)
        self.assertEqual(response["toast"]["type"], "success")
        payload = enqueue.call_args.args[0]
        self.assertEqual(
            payload["user_text"],
            "请分析韩束2026-08-24至2026-08-30的抖音生意，并解释增长原因。",
        )
        self.assertNotIn("capability", payload)
        self.assertNotIn("brand", payload)
        self.assertNotIn("period", payload)

        repeat = handle_card_action(event, redis=redis)
        self.assertEqual(repeat["toast"]["type"], "info")
        enqueue.assert_called_once()

        event["raw"]["header"]["event_id"] = "evt-2"
        second_click = handle_card_action(event, redis=redis)
        self.assertEqual(second_click["toast"]["type"], "info")
        enqueue.assert_called_once()

    @patch("bot.card_actions.enqueue_request")
    def test_card_action_rejects_wrong_operator_and_tampered_prompt(self, enqueue):
        redis = FakeRedis()
        recipient = register_recipient("ou-user", "oc-chat", redis=redis)
        save_campaign(sent_campaign(recipient.code), redis=redis)
        base = {
            "message_id": "om-card",
            "chat_id": "oc-chat",
            "operator": {"open_id": "ou-other"},
            "action": {"value": {
                "v": 1,
                "action": "run_analysis",
                "campaign_id": "campaign-1",
                "prompt_id": "hanshu-douyin-last-week",
            }},
        }
        self.assertEqual(handle_card_action(base, redis=redis)["toast"]["type"], "warning")
        base["operator"]["open_id"] = "ou-user"
        base["action"]["value"]["prompt_id"] = "tampered"
        self.assertEqual(handle_card_action(base, redis=redis)["toast"]["type"], "warning")
        enqueue.assert_not_called()

    def test_custom_question_is_stored_server_side_and_supports_period_placeholders(self):
        redis = FakeRedis()
        recipient = register_recipient("ou-user", "oc-chat", redis=redis)
        campaign = create_immediate_campaign(
            recipient.code,
            analysis_prompt="比较{period_start}到{period_end}韩束与竞品的抖音增速",
            redis=redis,
            now=datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            build_analysis_prompt(campaign),
            "比较2026-08-24到2026-08-30韩束与竞品的抖音增速",
        )
        stored = redis.hgetall(f"ai-bot:proactive:campaign:{campaign.campaign_id}")
        self.assertIn("analysis_prompt", stored)

    @patch("bot.card_actions.enqueue_request")
    def test_official_lark_callback_shape_is_supported(self, enqueue):
        redis = FakeRedis()
        recipient = register_recipient("ou-user", "oc-chat", redis=redis)
        save_campaign(sent_campaign(recipient.code), redis=redis)
        event = {
            "event": {
                "operator": {"open_id": "ou-user"},
                "context": {"open_message_id": "om-card", "open_chat_id": "oc-chat"},
                "action": {"value": {
                    "v": 1,
                    "action": "run_analysis",
                    "campaign_id": "campaign-1",
                    "prompt_id": "hanshu-douyin-last-week",
                }},
            },
            "event_id": "evt-official",
        }
        self.assertEqual(handle_card_action(event, redis=redis)["toast"]["type"], "success")
        enqueue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
