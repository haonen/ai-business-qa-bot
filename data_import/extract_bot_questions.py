from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Iterator


@dataclass
class QuestionRecord:
    message_id: str
    created_at: str
    chat_ref: str
    user_ref: str
    question: str
    source: str
    conversation_turn: int = 0


def _stable_ref(value: str, salt: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(f"{salt}\0{value}".encode("utf-8")).hexdigest()[:20]


def _iso_time(value) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    if number > 10_000_000_000:
        number /= 1000
    return datetime.fromtimestamp(number, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _text_content(content: str | None) -> str:
    if not content:
        return ""
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return str(content).strip()
    if isinstance(parsed, dict):
        return str(parsed.get("text") or "").strip()
    return ""


def _event_question(payload: dict, *, salt: str) -> QuestionRecord | None:
    event = payload.get("event") or {}
    message = event.get("message") or {}
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    if message.get("chat_type") != "p2p" or message.get("message_type") != "text":
        return None
    if sender.get("sender_type") not in {None, "user"}:
        return None
    question = _text_content(message.get("content"))
    message_id = str(message.get("message_id") or "").strip()
    chat_id = str(message.get("chat_id") or "").strip()
    open_id = str(sender_id.get("open_id") or sender_id.get("user_id") or "").strip()
    if not question or not message_id or not chat_id:
        return None
    return QuestionRecord(
        message_id=message_id,
        created_at=_iso_time(message.get("create_time")),
        chat_ref=_stable_ref(chat_id, salt),
        user_ref=_stable_ref(open_id, salt),
        question=question,
        source="log",
    )


def _json_payloads(line: str) -> Iterator[dict]:
    decoder = json.JSONDecoder()
    starts = []
    marker = "payload:"
    index = line.find(marker)
    if index >= 0:
        starts.append(index + len(marker))
    stripped = line.lstrip()
    if stripped.startswith("{"):
        starts.append(len(line) - len(stripped))
    for start in starts:
        fragment = line[start:].lstrip()
        try:
            payload, _ = decoder.raw_decode(fragment)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def questions_from_logs(paths: Iterable[Path], *, salt: str) -> list[QuestionRecord]:
    records = []
    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                for payload in _json_payloads(line):
                    record = _event_question(payload, salt=salt)
                    if record:
                        records.append(record)
    return records


def discovered_chat_ids(paths: Iterable[Path]) -> set[str]:
    chat_ids: set[str] = set()
    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                for payload in _json_payloads(line):
                    message = ((payload.get("event") or {}).get("message") or {})
                    if message.get("chat_type") == "p2p" and message.get("chat_id"):
                        chat_ids.add(str(message["chat_id"]))
    return chat_ids


def _message_question(item, *, salt: str) -> QuestionRecord | None:
    sender = getattr(item, "sender", None)
    if getattr(item, "msg_type", None) != "text" or getattr(item, "deleted", False):
        return None
    if getattr(sender, "sender_type", None) != "user":
        return None
    question = _text_content(getattr(getattr(item, "body", None), "content", None))
    message_id = str(getattr(item, "message_id", None) or "").strip()
    chat_id = str(getattr(item, "chat_id", None) or "").strip()
    sender_id = str(getattr(sender, "id", None) or "").strip()
    if not question or not message_id or not chat_id:
        return None
    return QuestionRecord(
        message_id=message_id,
        created_at=_iso_time(getattr(item, "create_time", None)),
        chat_ref=_stable_ref(chat_id, salt),
        user_ref=_stable_ref(sender_id, salt),
        question=question,
        source="feishu",
    )


def questions_from_feishu(chat_ids: Iterable[str], *, salt: str) -> list[QuestionRecord]:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import ListMessageRequest

    app_id = os.environ.get("FEISHU_APP_ID") or os.environ.get("APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET") or os.environ.get("APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID/FEISHU_APP_SECRET are required for --fetch-feishu")
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    records: list[QuestionRecord] = []
    for chat_id in sorted(set(chat_ids)):
        page_token = None
        while True:
            builder = (
                ListMessageRequest.builder()
                .container_id_type("chat")
                .container_id(chat_id)
                .sort_type("ByCreateTimeAsc")
                .page_size(50)
            )
            if page_token:
                builder = builder.page_token(page_token)
            response = client.im.v1.message.list(builder.build())
            if not response.success():
                raise RuntimeError(
                    f"Feishu message history failed for chat {chat_id[-6:]}: "
                    f"{response.code} {response.msg}"
                )
            data = response.data
            for item in list(getattr(data, "items", None) or []):
                record = _message_question(item, salt=salt)
                if record:
                    records.append(record)
            if not getattr(data, "has_more", False):
                break
            page_token = getattr(data, "page_token", None)
            if not page_token:
                break
    return records


def merge_records(*groups: Iterable[QuestionRecord]) -> list[QuestionRecord]:
    merged: dict[str, QuestionRecord] = {}
    for group in groups:
        for record in group:
            existing = merged.get(record.message_id)
            if existing:
                sources = set(existing.source.split("+")) | set(record.source.split("+"))
                existing.source = "+".join(sorted(sources))
                if not existing.created_at and record.created_at:
                    existing.created_at = record.created_at
                continue
            merged[record.message_id] = record
    records = sorted(merged.values(), key=lambda item: (item.chat_ref, item.created_at, item.message_id))
    turns: dict[str, int] = {}
    for record in records:
        turns[record.chat_ref] = turns.get(record.chat_ref, 0) + 1
        record.conversation_turn = turns[record.chat_ref]
    return records


def write_dataset(records: list[QuestionRecord], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "bot_questions.jsonl"
    csv_path = output_dir / "bot_questions.csv"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    fieldnames = list(asdict(QuestionRecord("", "", "", "", "", "")).keys())
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(record) for record in records)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract user questions sent to the Feishu bot.")
    parser.add_argument("--log", action="append", default=[], help="Receiver journal/bot log; repeatable.")
    parser.add_argument("--fetch-feishu", action="store_true", help="Backfill full history for chat_ids found in logs.")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    log_paths = [Path(value).expanduser().resolve() for value in args.log]
    missing = [str(path) for path in log_paths if not path.is_file()]
    if missing:
        raise SystemExit(f"Log files not found: {', '.join(missing)}")
    if not log_paths:
        raise SystemExit("At least one --log is required to discover bot P2P conversations.")

    salt = os.environ.get("BOT_AUDIT_HASH_SALT") or os.environ.get("FEISHU_APP_ID") or "ai-business-qa-bot"
    log_records = questions_from_logs(log_paths, salt=salt)
    feishu_records = []
    if args.fetch_feishu:
        chat_ids = discovered_chat_ids(log_paths)
        if not chat_ids:
            raise SystemExit("No P2P chat_id was discovered in the supplied logs.")
        feishu_records = questions_from_feishu(chat_ids, salt=salt)
    records = merge_records(log_records, feishu_records)
    write_dataset(records, Path(args.output_dir).expanduser().resolve())
    print(json.dumps({
        "questions": len(records),
        "conversations": len({record.chat_ref for record in records}),
        "from_logs": len(log_records),
        "from_feishu": len(feishu_records),
        "output_dir": str(Path(args.output_dir).expanduser().resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
