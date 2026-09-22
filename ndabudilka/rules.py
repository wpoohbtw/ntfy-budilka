from datetime import datetime, timedelta, timezone
from functools import lru_cache
import re

MOSCOW = timezone(timedelta(hours=3), "МСК")
DEFAULT_WORDS = ["SHORT", "LONG", "шорт", "лонг"]


def parse_words(value: str) -> list[str]:
    words = []
    seen = set()
    for word in re.split(r"[,\n;]+", value):
        word = word.strip()
        if not word:
            continue
        if len(word) > 64 or not re.fullmatch(r"[\w -]+", word):
            raise ValueError("Слово/фраза: до 64 символов, буквы, цифры, пробел, _ и -.")
        if word.casefold() not in seen:
            words.append(word)
            seen.add(word.casefold())
    if not 1 <= len(words) <= 50 or sum(map(len, words)) > 1000:
        raise ValueError("Укажите от 1 до 50 слов через запятую; всего до 1000 символов.")
    return words


@lru_cache(maxsize=64)
def word_pattern(word: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)", re.IGNORECASE)


def match_words(text: str, words: list[str]) -> list[str]:
    return [word for word in words if word_pattern(word).search(text)]


def parse_schedule(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})\s*", value)
    if not match:
        raise ValueError("Введите время по Москве: 01:00-07:00.")
    h1, m1, h2, m2 = map(int, match.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError("Часы: 00–23, минуты: 00–59.")
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start == end:
        raise ValueError("Начало и конец периода должны различаться.")
    return start, end


def format_time(minutes: int) -> str:
    return f"{minutes // 60:02}:{minutes % 60:02}"


def priority_for(user, now: datetime | None = None) -> int:
    if not user["night_enabled"]:
        return 5
    local = (now or datetime.now(timezone.utc)).astimezone(MOSCOW)
    minute = local.hour * 60 + local.minute
    start, end = user["night_start"], user["night_end"]
    active = start <= minute < end if start < end else minute >= start or minute < end
    return user["night_priority"] if active else 5


def message_topic(message, is_forum: bool) -> int:
    if not is_forum:
        return 0
    # New Telegram layers expose top_msg_id directly; older ones use reply headers.
    top = getattr(message, "top_msg_id", None)
    if top:
        return top
    reply = getattr(message, "reply_to", None)
    if reply and getattr(reply, "forum_topic", False):
        return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None) or 1
    return 1


def parse_source(value: str) -> tuple[int, int]:
    parts = value.replace(",", " ").split()
    if len(parts) not in (1, 2) or not re.fullmatch(r"-100\d+", parts[0]):
        raise ValueError("Введите ID канала/супергруппы -100… и, при необходимости, ID ветки через пробел.")
    if len(parts) == 2 and not parts[1].isdecimal():
        raise ValueError("ID ветки должен быть числом (0 = весь источник).")
    chat_id, topic_id = int(parts[0]), int(parts[1]) if len(parts) == 2 else 0
    if chat_id >= -1000000000000 or topic_id > 2147483647:
        raise ValueError("Проверьте ID группы и ID ветки.")
    return chat_id, topic_id


def clip_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit - 3].decode("utf-8", errors="ignore") + "…"
