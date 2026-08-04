from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import fcntl

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
    UpdateMessageRequest,
    UpdateMessageRequestBody,
)

from bot.app import run_agent
from bot.config import get_env, validate_runtime_env
from bot.session import add_message, get_session


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


def _send_reply(chat_id: str, text: str):
    card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": text}]}}
    resp = cli.im.v1.message.create(
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    if not resp.success():
        lark.logger.error(f"send_reply failed: {resp.code} {resp.msg}")


def _send_text(chat_id: str, text: str) -> str | None:
    resp = cli.im.v1.message.create(
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    return resp.data.message_id if resp.success() else None


def _update_text(message_id: str | None, text: str):
    if not message_id:
        return
    cli.im.v1.message.update(
        UpdateMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            UpdateMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )


def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    message = data.event.message
    if message.chat_type != "p2p":
        return
    if message.message_id in _processed or not _claim_recent_key("message_id", message.message_id, 86400):
        return
    _processed.add(message.message_id)
    if message.message_type != "text":
        _send_reply(message.chat_id, "抱歉，目前只支持文字消息。")
        return
    open_id = data.event.sender.sender_id.open_id
    user_text = json.loads(message.content).get("text", "").strip()
    if not user_text or not _claim_recent_key("request_text", f"{open_id}\n{user_text}", 180):
        return

    placeholder_id = _send_text(message.chat_id, "正在分析数据，请稍候…")

    def on_progress(stage_text: str):
        _update_text(placeholder_id, stage_text)

    try:
        state = get_session(open_id)
        result = run_agent(open_id, user_text, state, on_progress=on_progress)
        markdown = result.get("markdown") or "没有生成可用结果。"
        add_message(open_id, "user", user_text)
        add_message(open_id, "assistant", markdown)

        route_type = result.get("route_type")
        report_meta = result.get("meta", {})
        market_document = route_type in {"market_analysis", "market_brand_ranking"}
        brand_document = route_type in {"default_chain", "media_analysis", "skill_dispatch"} and report_meta.get("brand")
        if (market_document or brand_document) and report_meta.get("document_ready", True):
            import bot.feishu_doc as feishu_doc

            brand = result["meta"].get("brand")
            period = result["meta"].get("period")
            if market_document:
                doc_title = result["meta"].get("document_title") or f"{period} 大盘分析"
            elif route_type == "skill_dispatch":
                doc_title = result["meta"].get("document_title") or f"{brand} {period} 数据分析"
            elif route_type == "media_analysis":
                period_title = result["meta"].get("period_display") or period
                doc_title = f"{brand} {period_title} BET媒体投资分析报告"
            else:
                doc_title = f"{brand} {period} 生意分析报告"
            on_progress("正在生成分析报告文档…")
            doc_url = feishu_doc.create_feishu_doc(cli, doc_title, markdown)
            _update_text(placeholder_id, f"已生成「{doc_title}」分析报告：{doc_url}")
        else:
            _update_text(placeholder_id, "已完成，结果如下：")
            _send_reply(message.chat_id, markdown)
    except Exception as exc:
        lark.logger.exception(f"agent failed: {exc}")
        _update_text(placeholder_id, f"分析时遇到问题：{exc}，请重新尝试或换个问题。")


def main():
    _acquire_process_lock()
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
