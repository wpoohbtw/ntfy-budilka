import asyncio
from datetime import datetime
import json
import logging
import time

from .notifications import DeliveryError, Notification, NotificationSender
from .rules import night_active, priority_for
from .storage import Database

log = logging.getLogger(__name__)


class DeliveryWorker:
    def __init__(self, db: Database, sender: NotificationSender, telegram_alerts=None):
        self.db = db
        self.sender = sender
        self.telegram_alerts = telegram_alerts
        self.wakeup = asyncio.Event()

    async def deliver(self, item, now: datetime | None = None):
        user = self.db.user(item["user_id"])
        if (not user or not user["granted"] or not user["enabled"]
                or not self.db.has_source(item["chat_id"], item["topic_id"])
                or time.time() - item["created_at"] > 3600):
            self.db.finish(item["id"], "cancelled")
            log.info("Уведомление отменено: id=%s источник=%r; доступ, источник, пауза или срок ожидания изменились",
                     item["id"], item["title"])
            return
        destination = json.loads(user["destination"])
        if not destination:
            self.db.finish(item["id"], "cancelled")
            log.info("Уведомление отменено: id=%s; подключение не настроено", item["id"])
            return
        notification = Notification(item["title"], item["body"], priority_for(user, now), item["link"])
        log.info("Отправка уведомления: id=%s источник=%r получатель=%s сообщение=%s приоритет=%s попытка=%s",
                 item["id"], item["title"], user["id"], item["message_id"], notification.priority, item["attempts"] + 1)
        try:
            await self.sender.send(destination, notification)
        except DeliveryError as error:
            attempts = item["attempts"] + 1
            self.db.update_user(user["id"], last_error=str(error))
            if error.retryable and attempts < 6:
                delay = max(error.retry_after, min(5 * 2 ** (attempts - 1), 120))
                self.db.retry(item["id"], attempts, delay)
                log.warning("Повтор отправки: id=%s источник=%r через=%s сек.; %s", item["id"], item["title"], delay, error)
            else:
                self.db.finish(item["id"], "failed")
                log.error("Уведомление не доставлено: id=%s источник=%r попыток=%s; %s", item["id"], item["title"], attempts, error)
        else:
            self.db.finish(item["id"], "sent")
            self.db.update_user(user["id"], last_error="")
            log.info("Уведомление доставлено: id=%s источник=%r получатель=%s приоритет=%s",
                     item["id"], item["title"], user["id"], notification.priority)
            if self.telegram_alerts:
                try:
                    await self.telegram_alerts.notify(user["id"], item["title"], night_active(user, now))
                except Exception as error:
                    log.warning("Telegram-уведомление не отправлено: получатель=%s источник=%r; %s",
                                user["id"], item["title"], type(error).__name__)

    async def run(self):
        while True:
            self.wakeup.clear()
            items = self.db.due(8)
            if items:
                # Each row is only taken by this one worker; slow destinations run independently.
                await asyncio.gather(*(self.deliver(item) for item in items))
                continue
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
