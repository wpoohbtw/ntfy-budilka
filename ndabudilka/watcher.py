import asyncio
from datetime import datetime, timezone
import json
import logging

from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import FloodWaitError, RPCError

from .delivery import DeliveryWorker
from .rules import clip_utf8, match_words, message_topic
from .storage import Database

log = logging.getLogger(__name__)


class Watcher:
    def __init__(self, client: TelegramClient, db: Database, worker: DeliveryWorker):
        self.client = client
        self.db = db
        self.worker = worker
        self.started_at = datetime.now(timezone.utc).replace(microsecond=0)
        self.last_error = ""
        self.client.add_event_handler(self.on_message, events.NewMessage())

    async def validate_source(self, chat_id: int, topic_id: int) -> dict:
        if not self.client.is_connected():
            raise ValueError("Аккаунт чтения не подключён. Проверьте консоль приложения.")
        try:
            entity = None
            # A dialog proves membership; resolving a public channel alone does not.
            async with asyncio.timeout(30):
                async for dialog in self.client.iter_dialogs():
                    if dialog.id == chat_id:
                        entity = dialog.entity
                        break
                if not isinstance(entity, types.Channel) or getattr(entity, "left", False):
                    raise ValueError("Канал/супергруппа не найдены среди подписок аккаунта чтения.")
                forum = bool(getattr(entity, "forum", False))
                title = entity.title
                if topic_id:
                    if not forum:
                        raise ValueError("В этой группе нет веток. Для всего источника укажите ID ветки 0.")
                    result = await self.client(functions.messages.GetForumTopicsByIDRequest(peer=entity, topics=[topic_id]))
                    topic = next((t for t in result.topics if isinstance(t, types.ForumTopic) and t.id == topic_id), None)
                    if topic is None:
                        raise ValueError("Ветка с таким ID не найдена в этой группе.")
                    title += " / " + topic.title
                elif forum:
                    title += " / все ветки"
                return {"chat_id": utils.get_peer_id(entity), "topic_id": topic_id,
                        "title": title[:250], "forum": forum}
        except FloodWaitError as error:
            raise ValueError(f"Telegram просит подождать {error.seconds} сек. Повторите позже.") from None
        except (RPCError, OSError, asyncio.TimeoutError):
            raise ValueError("Не удалось проверить источник через Telegram. Проверьте ID и повторите позже.") from None

    async def on_message(self, event):
        try:
            self.process_message(event.chat_id, event.message)
        except Exception as error:
            self.last_error = "Ошибка обработки сообщения; проверьте журнал приложения."
            # Telegram objects/exception strings may contain private post data.
            log.error("Watcher processing failed: %s", type(error).__name__)

    def process_message(self, chat_id: int, message) -> int:
        sources = self.db.sources(chat_id)
        if not sources or getattr(message, "action", None):
            return 0
        if message.date and message.date < self.started_at:
            return 0
        topic_id = message_topic(message, any(source["forum"] for source in sources))
        source = next((s for s in sources if s["topic_id"] in (0, topic_id)), None)
        if source is None:
            return 0
        message_text = message.message or ""
        if source["trigger_mode"] == "keywords" and not match_words(message_text, self.db.words()):
            return 0
        body = clip_utf8(source["title"] + ": " + (message_text or "Новое сообщение без текста"), 3000)
        channel = str(chat_id)[4:]
        path = f"{channel}/{topic_id}/{message.id}" if topic_id > 1 else f"{channel}/{message.id}"
        link = "https://t.me/c/" + path
        queued = 0
        for user in self.db.users():
            if user["enabled"] and json.loads(user["destination"]):
                queued += self.db.enqueue(user["id"], chat_id, message.id, topic_id, source["title"], body, link)
        if queued:
            log.info("Найден сигнал: источник=%r режим=%s chat_id=%s сообщение=%s получателей=%s",
                     source["title"], source["trigger_mode"], chat_id, message.id, queued)
            self.worker.wakeup.set()
        self.last_error = ""
        return queued
