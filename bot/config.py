from __future__ import annotations

from pathlib import Path
import os

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")


def get_env(name: str, *fallback_names: str, required: bool = True, default: str | None = None) -> str | None:
    for key in (name, *fallback_names):
        value = os.environ.get(key)
        if value:
            return value
    if required:
        aliases = ", ".join((name, *fallback_names))
        raise RuntimeError(
            f"Missing environment variable: {name}. "
            f"Please set one of [{aliases}] in {BASE_DIR / '.env'}."
        )
    return default


def validate_runtime_env() -> None:
    required = {
        "FEISHU_APP_ID": ("APP_ID",),
        "FEISHU_APP_SECRET": ("APP_SECRET",),
        "MYSQL_HOST": (),
        "MYSQL_USER": (),
        "MYSQL_PASSWORD": (),
        "MYSQL_DATABASE": (),
        "DASHSCOPE_API_KEY": (),
    }
    missing = []
    for name, aliases in required.items():
        if not any(os.environ.get(key) for key in (name, *aliases)):
            alias_text = f" 或 {'/'.join(aliases)}" if aliases else ""
            missing.append(f"{name}{alias_text}")
    if missing:
        raise RuntimeError(
            "Missing required .env settings:\n"
            + "\n".join(f"- {item}" for item in missing)
            + f"\nPlease edit {BASE_DIR / '.env'} and restart."
        )

