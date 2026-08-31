from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import fcntl

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
)

from bot.config import get_env, validate_runtime_env
from bot.messaging import send_reply, send_text, update_text
from bot.request_processor import process_request
from bot.runtime_config import queue_enabled, worker_settings
from bot.request_audit import record_outcome, record_received
from bot.agent_plan import initial_status


validate_runtime_env()
APP_ID = get_env("FEISHU_APP_ID", "APP_ID")
APP_SECRET = get_env("FEISHU_APP_SECRET", "APP_SECRET")

cli = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
_lock_file = None
_processed: set[str] = set()


def _acquire_process_lock():
    global _lock_file
    lock_path = os.path.join(os.path.dirname(__file__), ".main.py.lock")
    _lock_file = open(lock_path, "w")
    try:
        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lark.logger.error("Another bot/main.py process is already running; exiting.")
        sys.exit(1)
    _lock_file.write(str(os.getpid()))
    _lock_file.flush()


def _claim_recent_key(namespace: str, key_src: str, ttl_seconds: int) -> bool:
    base_dir = os.path.dirname(__file__)
    lock_path = os.path.join(base_dir, ".recent_requests.lock")
    state_path = os.path.join(base_dir, ".recent_requests.json")
    key = hashlib.sha256(f"{namespace}\n{key_src}".encode("utf-8")).hexdigest()
    now = time.time()
    with open(lock_path, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with open(state_path, "r") as f:
                state = json.load(f)
        except Exception:
            state = {}
        state = {k: ts for k, ts in state.items() if isinstance(ts, (int, float)) and now - ts <= ttl_seconds}
        if key in state:
            return False
        state[key] = now
        with open(state_path, "w") as f:
            json.dump(state, f)
        return True


def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    message = data.event.message
    if message.chat_type != "p2p":
        return
    if queue_enabled():
        try:
            from bot.task_queue import claim_recent_key

            if not claim_recent_key("message_id", message.message_id, 86400):
                return
        except Exception as exc:
            lark.logger.exception(f"Redis dedupe unavailable: {exc}")
            send_reply(cli, message.chat_id, "任务队列暂时不可用，请稍后重新发送。")
            return
    else:
        if message.message_id in _processed or not _claim_recent_key("message_id", message.message_id, 86400):
            return
        _processed.add(message.message_id)
    if message.message_type != "text":
        send_reply(cli, message.chat_id, "抱歉，目前只支持文字消息。")
        return
    open_id = data.event.sender.sender_id.open_id
    user_text = json.loads(message.content).get("text", "").strip()
    if not user_text:
        return
    record_received(
        message_id=message.message_id,
        chat_id=message.chat_id,
        open_id=open_id,
        user_text=user_text,
        create_time=message.create_time,
    )

    if queue_enabled():
        from bot.task_queue import (
            claim_recent_key,
            enqueue_request,
            job_id_for_message,
            release_recent_key,
        )

        text_key = f"{open_id}\n{user_text}"
        placeholder_id = None
        try:
            if not claim_recent_key("request_text", text_key, 180):
                send_reply(cli, message.chat_id, "相同请求已经提交，正在处理中，请稍候。")
                return
            job_id = job_id_for_message(message.message_id)
            placeholder_id = send_text(
                cli,
                message.chat_id,
                initial_status(queued=True, job_id=job_id),
            )
            payload = {
                "job_id": job_id,
                "message_id": message.message_id,
                "chat_id": message.chat_id,
                "open_id": open_id,
                "user_text": user_text,
                "placeholder_id": placeholder_id,
            }
            enqueue_request(payload)
        except Exception as exc:
            try:
                release_recent_key("request_text", text_key)
            except Exception:
                pass
            lark.logger.exception(f"enqueue failed: {exc}")
            record_outcome(
                message_id=message.message_id,
                result=None,
                elapsed_ms=0,
                status="failed",
                error_message=f"enqueue failed: {exc}",
            )
            if placeholder_id:
                update_text(cli, placeholder_id, "任务队列暂时不可用，请稍后重新发送。")
            else:
                send_reply(cli, message.chat_id, "任务队列暂时不可用，请稍后重新发送。")
        return

    placeholder_id = send_text(cli, message.chat_id, initial_status(queued=False))

    try:
        process_request(
            cli,
            open_id=open_id,
            chat_id=message.chat_id,
            user_text=user_text,
            placeholder_id=placeholder_id,
            message_id=message.message_id,
        )
    except Exception as exc:
        lark.logger.exception(f"agent failed: {exc}")
        update_text(cli, placeholder_id, f"处理时遇到问题：{exc}，请重新尝试或换个问题。")


def main():
    _acquire_process_lock()
    if queue_enabled():
        from bot.redis_client import require_redis

        require_redis()
        settings = worker_settings()
        lark.logger.info(
            "Queue mode enabled: cpu=%s workers=%s mysql_budget=%s/%s",
            settings.cpu_count,
            settings.worker_count,
            settings.db_connection_budget,
            settings.max_db_connection_budget,
        )
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .build()
    )
    ws_client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG,
    )
    lark.logger.info("Starting AI Business QA Bot WebSocket long-connection...")
    ws_client.start()


if __name__ == "__main__":
    main()
