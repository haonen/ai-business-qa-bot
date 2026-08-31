from __future__ import annotations

import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, MagicMock, patch

import fakeredis
from rq import Queue

from bot.redis_client import set_redis_for_tests
from bot.runtime_config import bounded_query_workers, inner_query_worker_limit, resolve_worker_count, worker_settings
from bot.session import get_session, update_context, update_domain_context
from bot.task_queue import claim_recent_key, enqueue_request, job_id_for_message
from bot.worker_pool import build_worker_command


class MemoryRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, **kwargs):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def ping(self):
        return True


class TestLock:
    def acquire(self, blocking=True):
        return True

    def release(self):
        return None


class WorkerConfigTest(unittest.TestCase):
    def test_inner_query_worker_limit_and_bounding(self):
        for value in ("1", "2", "3", "8"):
            with self.subTest(value=value), patch.dict(os.environ, {"BOT_INNER_QUERY_WORKERS": value}):
                self.assertEqual(inner_query_worker_limit(), int(value))
        with patch.dict(os.environ, {"BOT_INNER_QUERY_WORKERS": "3"}):
            self.assertEqual(bounded_query_workers(5), 3)
            self.assertEqual(bounded_query_workers(2), 2)

    def test_rejects_invalid_inner_query_worker_limit(self):
        for value in ("0", "9", "many"):
            with self.subTest(value=value), patch.dict(os.environ, {"BOT_INNER_QUERY_WORKERS": value}):
                with self.assertRaises(ValueError):
                    inner_query_worker_limit()

    def test_auto_matches_cpu_count(self):
        self.assertEqual(resolve_worker_count("auto", cpu_count=4), 4)
        self.assertEqual(resolve_worker_count("auto", cpu_count=8), 8)

    def test_explicit_worker_profiles(self):
        self.assertEqual(resolve_worker_count("4", cpu_count=4), 4)
        self.assertEqual(resolve_worker_count("8", cpu_count=8), 8)
        self.assertEqual(resolve_worker_count("12", cpu_count=8), 12)

    def test_rejects_invalid_and_excessive_workers(self):
        for value in ("0", "17", "many"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_worker_count(value, cpu_count=8)
        with self.assertRaisesRegex(ValueError, "2x CPU"):
            worker_settings("9", cpu_count=4, pool_size=4, max_overflow=1, max_db_budget=60)

    def test_rejects_db_connection_budget_overflow(self):
        with self.assertRaisesRegex(ValueError, "exceeding"):
            worker_settings("12", cpu_count=8, pool_size=4, max_overflow=2, max_db_budget=60)
        settings = worker_settings("12", cpu_count=8, pool_size=4, max_overflow=1, max_db_budget=60)
        self.assertEqual(settings.db_connection_budget, 60)
        self.assertTrue(settings.burst_mode)

    @patch("bot.runtime_config.os.cpu_count", return_value=8)
    def test_worker_pool_command_uses_configured_count(self, _cpu_count):
        with patch.dict(os.environ, {
            "BOT_WORKER_COUNT": "12",
            "MYSQL_POOL_SIZE": "4",
            "MYSQL_MAX_OVERFLOW": "1",
            "BOT_MAX_DB_CONNECTION_BUDGET": "60",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
        }, clear=False):
            command, settings = build_worker_command()
        self.assertEqual(settings.worker_count, 12)
        self.assertEqual(command[command.index("--num-workers") + 1], "12")

    def test_document_worker_pool_uses_its_own_queue_and_count(self):
        with patch.dict(os.environ, {
            "BOT_DOCUMENT_QUEUE_NAME": "test-documents",
            "BOT_DOCUMENT_WORKER_COUNT": "1",
        }, clear=False):
            command, settings = build_worker_command("document")
        self.assertEqual(settings.role, "document")
        self.assertEqual(settings.worker_count, 1)
        self.assertIn("test-documents", command)


class RedisSessionTest(unittest.TestCase):
    def setUp(self):
        self.redis = MemoryRedis()
        set_redis_for_tests(self.redis)
        self.env = patch.dict(os.environ, {
            "BOT_SESSION_BACKEND": "redis",
            "BOT_SESSION_TTL_SECONDS": "1209600",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        set_redis_for_tests(None)

    def test_context_round_trip_between_calls(self):
        update_context("user-1", brand="PROYA", period="2026年6月", brand_aliases=["珀莱雅"])
        update_domain_context(
            "user-1", "bet", brand="PROYA", period="2026年6月",
            filters={"media_mode": "OVERALL_BET"},
        )
        state = get_session("user-1")
        self.assertEqual(state.drilldown_ctx.brand, "PROYA")
        self.assertEqual(state.drilldown_ctx.brand_aliases, ["珀莱雅"])
        self.assertEqual(state.bet_context.filters["media_mode"], "OVERALL_BET")

    def test_payload_is_json_not_pickle(self):
        update_context("user-2", brand="谷雨")
        raw = next(iter(self.redis.values.values()))
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["drilldown_ctx"]["brand"], "谷雨")


class QueueBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.redis = fakeredis.FakeRedis()
        self.redis.lock = lambda *args, **kwargs: TestLock()
        set_redis_for_tests(self.redis)
        self.env = patch.dict(os.environ, {
            "BOT_QUEUE_NAME": "test-ai-bot",
            "BOT_JOB_TIMEOUT_SECONDS": "300",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        set_redis_for_tests(None)

    def payload(self, message_id: str, open_id: str = "same-user") -> dict:
        return {
            "job_id": job_id_for_message(message_id),
            "message_id": message_id,
            "chat_id": "chat-1",
            "open_id": open_id,
            "user_text": f"question-{message_id}",
            "placeholder_id": "placeholder-1",
        }

    def test_dedupe_key_is_atomic_and_reusable_after_ttl_namespace_change(self):
        self.assertTrue(claim_recent_key("message_id", "m-1", 60))
        self.assertFalse(claim_recent_key("message_id", "m-1", 60))
        self.assertTrue(claim_recent_key("request_text", "m-1", 60))

    def test_same_user_jobs_form_dependency_chain(self):
        first = enqueue_request(self.payload("m-1"))
        second = enqueue_request(self.payload("m-2"))
        self.assertEqual(first.get_status(refresh=True).value, "queued")
        self.assertEqual(second.get_status(refresh=True).value, "deferred")
        self.assertEqual(second.dependency_ids, [first.id])

    def test_different_users_are_both_queued(self):
        first = enqueue_request(self.payload("m-1", "user-a"))
        second = enqueue_request(self.payload("m-2", "user-b"))
        queue = Queue("test-ai-bot", connection=self.redis)
        self.assertEqual(first.get_status(refresh=True).value, "queued")
        self.assertEqual(second.get_status(refresh=True).value, "queued")
        self.assertEqual(len(queue), 2)


class AsyncDocumentTest(unittest.TestCase):
    def test_document_report_is_enqueued_without_inline_creation(self):
        result = {
            "route_type": "media_analysis",
            "markdown": "# report",
            "meta": {"brand": "PROYA", "period": "2026-06", "document_ready": True},
        }
        with patch.dict(os.environ, {"BOT_QUEUE_ENABLED": "1", "BOT_ASYNC_DOCUMENTS": "1"}), \
             patch("bot.request_processor.get_session", return_value=MagicMock()), \
             patch("bot.request_processor.run_agent", return_value=result), \
             patch("bot.request_processor.add_message"), \
             patch("bot.request_processor.build_inline_answer", return_value="先给结论。"), \
             patch("bot.request_processor.update_text") as update, \
             patch("bot.request_processor.send_reply") as send, \
             patch("bot.document_queue.enqueue_document") as enqueue:
            from bot.request_processor import process_request

            process_request(
                MagicMock(), open_id="u1", chat_id="c1", user_text="analyse",
                placeholder_id="p1", analysis_job_id="job-1",
            )
        enqueue.assert_called_once()
        payload = enqueue.call_args.args[0]
        self.assertEqual(payload["markdown"], "# report")
        self.assertEqual(payload["analysis_job_id"], "job-1")
        self.assertEqual(payload["inline_answer"], "先给结论。")
        self.assertIn("先给结论", update.call_args.args[-1])
        self.assertIn("正在后台生成文档", update.call_args.args[-1])
        send.assert_not_called()

    def test_document_enqueue_failure_sends_markdown_fallback(self):
        result = {
            "route_type": "default_chain",
            "markdown": "fallback report",
            "meta": {"brand": "PROYA", "period": "2026-06", "document_ready": True},
        }
        with patch.dict(os.environ, {"BOT_QUEUE_ENABLED": "1", "BOT_ASYNC_DOCUMENTS": "1"}), \
             patch("bot.request_processor.get_session", return_value=MagicMock()), \
             patch("bot.request_processor.run_agent", return_value=result), \
             patch("bot.request_processor.add_message"), \
             patch("bot.request_processor.update_text"), \
             patch("bot.request_processor.send_reply") as send, \
             patch("bot.document_queue.enqueue_document", side_effect=RuntimeError("redis down")):
            from bot.request_processor import process_request

            process_request(
                MagicMock(), open_id="u1", chat_id="c1", user_text="analyse",
                placeholder_id="p1", analysis_job_id="job-1",
            )
        send.assert_called_once_with(ANY, "c1", "fallback report")

    def test_non_document_answer_does_not_enter_document_queue(self):
        result = {"route_type": "smalltalk", "markdown": "hello", "meta": {}}
        with patch.dict(os.environ, {"BOT_QUEUE_ENABLED": "1", "BOT_ASYNC_DOCUMENTS": "1"}), \
             patch("bot.request_processor.get_session", return_value=MagicMock()), \
             patch("bot.request_processor.run_agent", return_value=result), \
             patch("bot.request_processor.add_message"), \
             patch("bot.request_processor.update_text"), \
             patch("bot.request_processor.send_reply") as send, \
             patch("bot.document_queue.enqueue_document") as enqueue:
            from bot.request_processor import process_request

            process_request(
                MagicMock(), open_id="u1", chat_id="c1", user_text="hi", placeholder_id="p1",
            )
        enqueue.assert_not_called()
        send.assert_called_once_with(ANY, "c1", "hello")

    def test_document_worker_updates_placeholder_with_link(self):
        payload = {
            "title": "June report", "markdown": "# report", "chat_id": "c1", "placeholder_id": "p1",
            "inline_answer": "先给结论。",
        }
        client = MagicMock()
        with patch("bot.document_jobs.create_lark_client", return_value=client), \
             patch("bot.feishu_doc.create_feishu_doc", return_value="https://doc.example/1"), \
             patch("bot.document_jobs.update_text") as update:
            from bot.document_jobs import execute_document

            result = execute_document(payload)
        self.assertTrue(result["ok"])
        self.assertIn("先给结论", update.call_args.args[-1])
        self.assertIn("https://doc.example/1", update.call_args.args[-1])

    def test_document_failure_callback_sends_markdown_fallback(self):
        payload = {
            "title": "June report", "markdown": "# fallback", "chat_id": "c1", "placeholder_id": "p1",
        }
        job = SimpleNamespace(id="document-1", args=[payload])
        with patch("bot.document_jobs.create_lark_client", return_value=MagicMock()), \
             patch("bot.document_jobs.update_text") as update, \
             patch("bot.document_jobs.send_reply") as send:
            from bot.document_jobs import handle_document_failure

            handle_document_failure(job, None, RuntimeError, RuntimeError("timeout"), None)
        self.assertIn("文档生成失败", update.call_args.args[-1])
        send.assert_called_once_with(ANY, "c1", "# fallback")


class DocumentQueueTest(unittest.TestCase):
    def setUp(self):
        self.redis = fakeredis.FakeRedis()
        set_redis_for_tests(self.redis)
        self.env = patch.dict(os.environ, {
            "BOT_DOCUMENT_QUEUE_NAME": "test-documents",
            "BOT_DOCUMENT_TIMEOUT_SECONDS": "180",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        set_redis_for_tests(None)

    def test_document_job_uses_fifo_queue_and_timeout_without_retry(self):
        from bot.document_queue import enqueue_document

        job = enqueue_document({
            "analysis_job_id": "analysis-1",
            "title": "June report",
            "markdown": "# report",
            "chat_id": "c1",
            "placeholder_id": "p1",
        })
        queue = Queue("test-documents", connection=self.redis)
        self.assertEqual(len(queue), 1)
        self.assertEqual(job.timeout, 180)
        self.assertIsNone(job.retries_left)


if __name__ == "__main__":
    unittest.main()
