import asyncio
from dataclasses import dataclass
import logging
import secrets

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


log = logging.getLogger(__name__)


@dataclass
class _AlertState:
    user_id: int
    hidden: asyncio.Event
    task: asyncio.Task | None = None


class TelegramAlertRepeater:
    """Short-lived bot alerts kept separate from the external notification provider."""

    def __init__(self, bot: Bot, interval: float = 10, daytime_total: int = 6):
        self.bot = bot
        self.interval = interval
        self.daytime_total = daytime_total
        self._states: dict[str, _AlertState] = {}

    @staticmethod
    def _markup(token: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Скрыть", callback_data=f"hide_alert:{token}")
        ]])

    async def notify(self, user_id: int, title: str, night: bool):
        token = secrets.token_urlsafe(9)
        state = _AlertState(user_id, asyncio.Event())
        self._states[token] = state
        try:
            message = await self.bot.send_message(
                user_id, f"{title} - новое сообщение", reply_markup=self._markup(token)
            )
        except Exception:
            self._states.pop(token, None)
            raise
        if state.hidden.is_set():
            await self._delete(user_id, message.message_id)
            self._states.pop(token, None)
            return
        if night or self.daytime_total <= 1:
            self._states.pop(token, None)
            return
        state.task = asyncio.create_task(
            self._repeat(token, state, title, message.message_id),
            name=f"telegram-alert-{token}",
        )

    async def _repeat(self, token: str, state: _AlertState, title: str, message_id: int):
        try:
            for _ in range(1, self.daytime_total):
                try:
                    await asyncio.wait_for(state.hidden.wait(), timeout=self.interval)
                    return
                except asyncio.TimeoutError:
                    pass
                await self._delete(state.user_id, message_id)
                if state.hidden.is_set():
                    return
                message = await self.bot.send_message(
                    state.user_id, f"{title} - новое сообщение", reply_markup=self._markup(token)
                )
                message_id = message.message_id
                if state.hidden.is_set():
                    await self._delete(state.user_id, message_id)
                    return
        except Exception as error:
            log.warning("Повтор Telegram-уведомления остановлен: получатель=%s; %s",
                        state.user_id, type(error).__name__)
        finally:
            if self._states.get(token) is state:
                self._states.pop(token, None)

    async def hide(self, user_id: int, message_id: int, token: str):
        state = self._states.get(token)
        if state and state.user_id == user_id:
            state.hidden.set()
        await self._delete(user_id, message_id)

    async def _delete(self, user_id: int, message_id: int):
        try:
            await self.bot.delete_message(user_id, message_id)
        except TelegramAPIError as error:
            log.warning("Не удалось удалить Telegram-уведомление: получатель=%s; %s",
                        user_id, type(error).__name__)

    async def close(self):
        tasks = []
        for state in self._states.values():
            state.hidden.set()
            if state.task:
                state.task.cancel()
                tasks.append(state.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._states.clear()
