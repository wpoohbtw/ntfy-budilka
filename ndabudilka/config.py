from dataclasses import dataclass, field
import os
from pathlib import Path
import re

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    admin_id: int
    api_id: int
    api_hash: str = field(repr=False)
    session_path: Path
    string_session: str = field(default="", repr=False)
    database_path: Path = ROOT / "data/bot.sqlite3"
    allow_private_hosts: bool = False
    allow_http: bool = False

    @classmethod
    def load(cls) -> "Config":
        load_dotenv(ROOT / ".env", encoding="utf-8-sig")

        def required(key: str) -> str:
            value = os.getenv(key, "").strip()
            if not value:
                raise ValueError(f"Заполните {key} в .env (образец: .env.example).")
            return value

        def positive(key: str) -> int:
            value = required(key)
            if not value.isdecimal() or int(value) <= 0:
                raise ValueError(f"{key} должен быть положительным числом.")
            return int(value)

        def path(key: str, default: str) -> Path:
            return (ROOT / os.getenv(key, default)).resolve()

        def flag(key: str) -> bool:
            value = os.getenv(key, "false").strip().lower()
            if value not in {"true", "false"}:
                raise ValueError(f"{key}: допустимо true или false.")
            return value == "true"

        token = required("BOT_TOKEN")
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("Некорректный формат BOT_TOKEN.")
        api_hash = required("TELEGRAM_API_HASH")
        if not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise ValueError("TELEGRAM_API_HASH должен содержать 32 hex-символа.")
        return cls(
            bot_token=token,
            admin_id=positive("ADMIN_ID"),
            api_id=positive("TELEGRAM_API_ID"),
            api_hash=api_hash,
            session_path=path("TELETHON_SESSION", "data/watcher.session"),
            string_session=os.getenv("TELETHON_STRING_SESSION", "").strip(),
            database_path=path("DATABASE_PATH", "data/bot.sqlite3"),
            allow_private_hosts=flag("NTFY_ALLOW_PRIVATE_HOSTS"),
            allow_http=flag("NTFY_ALLOW_HTTP"),
        )
