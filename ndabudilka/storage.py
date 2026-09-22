"""Small, short SQLite transactions; used only on the application's event loop."""

import json
from pathlib import Path
import sqlite3
import time

from .rules import DEFAULT_WORDS


class Database:
    def __init__(self, path: Path | str, admin_id: int):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.admin_id = admin_id
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                granted INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                destination TEXT NOT NULL DEFAULT '{}',
                night_enabled INTEGER NOT NULL DEFAULT 0,
                night_start INTEGER NOT NULL DEFAULT 60,
                night_end INTEGER NOT NULL DEFAULT 420,
                night_priority INTEGER NOT NULL DEFAULT 2 CHECK(night_priority BETWEEN 1 AND 5),
                main_message_id INTEGER,
                prompt_message_id INTEGER,
                pending TEXT,
                draft TEXT,
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL,
                forum INTEGER NOT NULL DEFAULT 0,
                trigger_mode TEXT NOT NULL DEFAULT 'keywords' CHECK(trigger_mode IN ('keywords','all')),
                UNIQUE(chat_id, topic_id)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                link TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                UNIQUE(user_id, chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS delivery_due ON deliveries(state, next_attempt);
        """)
        source_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(sources)")}
        if "trigger_mode" not in source_columns:
            self.execute("ALTER TABLE sources ADD COLUMN trigger_mode TEXT NOT NULL DEFAULT 'keywords' "
                         "CHECK(trigger_mode IN ('keywords','all'))")
        self.execute("INSERT OR IGNORE INTO settings VALUES ('words', ?)", (json.dumps(DEFAULT_WORDS),))
        self.ensure_user(admin_id)
        self.update_user(admin_id, granted=1)

    def close(self):
        self.connection.close()

    def execute(self, sql: str, args=()):
        with self.connection:
            return self.connection.execute(sql, args)

    def ensure_user(self, user_id: int):
        self.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (user_id,))
        return self.user(user_id)

    def user(self, user_id: int):
        return self.connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def update_user(self, user_id: int, **values):
        allowed = {"granted", "enabled", "destination", "night_enabled", "night_start", "night_end",
                   "night_priority", "main_message_id", "prompt_message_id", "pending", "draft", "last_error"}
        if not values or not values.keys() <= allowed:
            raise ValueError("Unknown user settings")
        assignments = ", ".join(f"{key}=?" for key in values)
        self.execute(f"UPDATE users SET {assignments} WHERE id=?", (*values.values(), user_id))

    def set_access(self, user_id: int, granted: bool):
        if user_id == self.admin_id and not granted:
            raise ValueError("Нельзя отозвать доступ администратора.")
        self.ensure_user(user_id)
        self.update_user(user_id, granted=int(granted), draft=None, pending=None)
        if not granted:
            self.cancel_user_deliveries(user_id)

    def cancel_user_deliveries(self, user_id: int):
        self.execute("UPDATE deliveries SET state='cancelled', body='', title='', link='' "
                     "WHERE user_id=? AND state='pending'", (user_id,))

    def users(self):
        return self.connection.execute("SELECT * FROM users WHERE granted=1 ORDER BY id").fetchall()

    def words(self) -> list[str]:
        return json.loads(self.connection.execute("SELECT value FROM settings WHERE key='words'").fetchone()[0])

    def set_words(self, words: list[str]):
        self.execute("UPDATE settings SET value=? WHERE key='words'", (json.dumps(words, ensure_ascii=False),))

    def add_words(self, words: list[str]) -> list[str]:
        current = self.words()
        known = {word.casefold() for word in current}
        additions = []
        for word in words:
            key = word.casefold()
            if key not in known:
                additions.append(word)
                known.add(key)
        if len(current) + len(additions) > 50:
            raise ValueError("В общем списке может быть не больше 50 слов и фраз.")
        self.set_words(current + additions)
        return additions

    def remove_words(self, words: list[str]) -> list[str]:
        requested = {word.casefold() for word in words}
        current = self.words()
        removed = [word for word in current if word.casefold() in requested]
        self.set_words([word for word in current if word.casefold() not in requested])
        return removed

    def sources(self, chat_id: int | None = None):
        if chat_id is not None:
            return self.connection.execute("SELECT * FROM sources WHERE chat_id=? ORDER BY topic_id DESC", (chat_id,)).fetchall()
        return self.connection.execute("SELECT * FROM sources ORDER BY id").fetchall()

    def source(self, source_id: int):
        return self.connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()

    def add_source(self, chat_id: int, topic_id: int, title: str, forum: bool,
                   trigger_mode: str = "keywords"):
        if trigger_mode not in {"keywords", "all"}:
            raise ValueError("Unknown source trigger mode")
        self.execute("INSERT INTO sources(chat_id,topic_id,title,forum,trigger_mode) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(chat_id,topic_id) DO UPDATE SET title=excluded.title,forum=excluded.forum",
                     (chat_id, topic_id, title, int(forum), trigger_mode))

    def set_source_mode(self, source_id: int, trigger_mode: str):
        if trigger_mode not in {"keywords", "all"}:
            raise ValueError("Unknown source trigger mode")
        cursor = self.execute("UPDATE sources SET trigger_mode=? WHERE id=?", (trigger_mode, source_id))
        if cursor.rowcount != 1:
            raise ValueError("Источник уже удалён.")

    def remove_source(self, source_id: int):
        self.execute("DELETE FROM sources WHERE id=?", (source_id,))

    def has_source(self, chat_id: int, topic_id: int) -> bool:
        return any(row["topic_id"] in (0, topic_id) for row in self.sources(chat_id))

    def enqueue(self, user_id: int, chat_id: int, message_id: int, topic_id: int,
                title: str, body: str, link: str) -> bool:
        cursor = self.execute("INSERT OR IGNORE INTO deliveries "
                              "(user_id,chat_id,message_id,topic_id,title,body,link,created_at) VALUES (?,?,?,?,?,?,?,?)",
                              (user_id, chat_id, message_id, topic_id, title, body, link, time.time()))
        return cursor.rowcount == 1

    def due(self, limit: int = 20):
        return self.connection.execute("SELECT * FROM deliveries WHERE state='pending' AND next_attempt<=? "
                                       "ORDER BY next_attempt,id LIMIT ?", (time.time(), limit)).fetchall()

    def finish(self, delivery_id: int, state: str):
        if state not in {"sent", "cancelled", "failed"}:
            raise ValueError("Invalid delivery state")
        # Keep only the deduplication key/metadata after delivery, not private post text.
        self.execute("UPDATE deliveries SET state=?,body='',title='',link='' WHERE id=?", (state, delivery_id))

    def retry(self, delivery_id: int, attempts: int, delay: float):
        self.execute("UPDATE deliveries SET attempts=?, next_attempt=? WHERE id=?",
                     (attempts, time.time() + delay, delivery_id))

    def counts(self, user_id: int) -> dict[str, int]:
        return dict(self.connection.execute("SELECT state,count(*) FROM deliveries WHERE user_id=? GROUP BY state", (user_id,)))
