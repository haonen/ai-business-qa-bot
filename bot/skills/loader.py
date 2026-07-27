from __future__ import annotations

from pathlib import Path

from bot.utils import DATA_DIR, load_json


SKILL_DIR = Path(__file__).resolve().parent

SKILL_FILES = {
    "category_drill": "category_drill.md",
    "key_driver": "key_driver.md",
    "sku_investigation": "sku_investigation.md",
    "reverse_drill": "reverse_drill.md",
    "playbook_read": "playbook_read.md",
}


def load_skill(name: str) -> str:
    filename = SKILL_FILES.get(name)
    if not filename:
        raise ValueError(f"unknown skill: {name}")
    path = SKILL_DIR / filename
    return path.read_text(encoding="utf-8")


def load_meta_answers() -> str:
    return (SKILL_DIR / "meta_answers.md").read_text(encoding="utf-8")


def load_narrative_config() -> dict:
    return load_json(DATA_DIR / "narrative_config.json")


def build_skill_prompt(name: str, context: dict, followup_text: str) -> str:
    config = load_narrative_config()
    return f"""
你是AI生意问答Bot的业务分析Skill执行器。

当前上下文：
{context}

用户追问：
{followup_text}

叙事配置：
{config}

Skill方法论：
{load_skill(name)}

请严格按Skill方法论分析。只能使用工具返回的数据，不得引入行业、竞品、大盘、消费者画像等未提供信息。
"""

