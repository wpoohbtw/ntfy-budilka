import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from ndabudilka.telegram_alerts import TelegramAlertRepeater


class TelegramAlertRepeaterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.next_id = 100

        async def send(*args, **kwargs):
            self.next_id += 1
            return SimpleNamespace(message_id=self.next_id)

        self.bot = SimpleNamespace(send_message=AsyncMock(side_effect=send), delete_message=AsyncMock())

    async def test_daytime_alert_is_sent_six_times_and_first_five_are_deleted(self):
        alerts = TelegramAlertRepeater(self.bot, interval=0.001)
        await alerts.notify(1, "Signals", night=False)
        for _ in range(30):
            if not alerts._states:
                break
            await asyncio.sleep(0.002)
        self.assertEqual(self.bot.send_message.await_count, 6)
        self.assertEqual(self.bot.delete_message.await_count, 5)
        for call in self.bot.send_message.call_args_list:
            self.assertEqual(call.args[:2], (1, "Signals - новое сообщение"))
            self.assertEqual(call.kwargs["reply_markup"].inline_keyboard[0][0].text, "Скрыть")
        await alerts.close()

    async def test_night_alert_is_sent_once_and_left_visible(self):
        alerts = TelegramAlertRepeater(self.bot, interval=0.001)
        await alerts.notify(1, "Night signals", night=True)
        await asyncio.sleep(0.01)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.bot.delete_message.assert_not_awaited()
        await alerts.close()

    async def test_hide_deletes_current_alert_and_stops_repeats(self):
        alerts = TelegramAlertRepeater(self.bot, interval=60)
        await alerts.notify(1, "Signals", night=False)
        token = next(iter(alerts._states))
        await alerts.hide(1, 101, token)
        await asyncio.sleep(0)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.bot.delete_message.assert_awaited_once_with(1, 101)
        await alerts.close()
