from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from aiogram import Dispatcher
from aiogram.types import Chat, Message as TelegramMessage, Update, User

from ndabudilka.config import Config
from ndabudilka.notifications import DeliveryError
from ndabudilka.storage import Database
from ndabudilka.ui import BotUI


class UiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:", 1)
        self.db.set_access(2, True)
        self.config = Config("1:dummy", 1, 123, "a" * 32, Path("unused.session"))
        self.next_id = 100

        async def send(*args, **kwargs):
            self.next_id += 1
            return SimpleNamespace(message_id=self.next_id)

        self.bot = SimpleNamespace(send_message=AsyncMock(side_effect=send),
                                   edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
                                   delete_message=AsyncMock(), id=123)
        self.sender = SimpleNamespace(send=AsyncMock())
        self.telegram_alerts = SimpleNamespace(hide=AsyncMock())
        self.watcher = SimpleNamespace(client=Mock(), last_error="", validate_source=AsyncMock())
        self.ui = BotUI(self.bot, self.db, self.watcher, self.sender, self.config, self.telegram_alerts)

    async def asyncTearDown(self):
        self.db.close()

    def message(self, uid=1, text="/start", mid=10):
        return SimpleNamespace(from_user=SimpleNamespace(id=uid), message_id=mid, text=text)

    def query(self, data, uid=1, mid=None):
        return SimpleNamespace(from_user=SimpleNamespace(id=uid), data=data, answer=AsyncMock(),
                               message=SimpleNamespace(message_id=mid or self.db.user(uid)["main_message_id"]))

    async def test_repeated_start_creates_main_and_disables_old_menu(self):
        await self.ui.start(self.message())
        first = self.db.user(1)["main_message_id"]
        await self.ui.start(self.message(mid=11))
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertNotEqual(self.db.user(1)["main_message_id"], first)
        self.bot.edit_message_reply_markup.assert_awaited_with(chat_id=1, message_id=first, reply_markup=None)
        self.bot.delete_message.assert_any_await(1, 10)
        self.bot.delete_message.assert_any_await(1, 11)

    async def test_toggles_edit_only_main(self):
        await self.ui.start(self.message())
        await self.ui.callback(self.query("toggle:night_enabled"))
        self.assertEqual(self.db.user(1)["night_enabled"], 1)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertIn("✅", self.bot.edit_message_text.call_args.args[0])

    async def test_hide_alert_works_outside_main_message(self):
        query = self.query("hide_alert:abc", mid=999)
        await self.ui.callback(query)
        query.answer.assert_awaited_once()
        self.telegram_alerts.hide.assert_awaited_once_with(1, 999, "abc")

    async def test_input_cleanup_and_confirmation_before_save(self):
        await self.ui.start(self.message())
        await self.ui.callback(self.query("input:words_add"))
        prompt = self.db.user(1)["prompt_message_id"]
        await self.ui.input_message(self.message(text="ENTRY, EXIT", mid=20))
        self.bot.delete_message.assert_any_await(1, prompt)
        self.bot.delete_message.assert_any_await(1, 20)
        self.assertIn("SHORT", self.db.words())
        draft = json.loads(self.db.user(1)["draft"])
        query = self.query("save:" + draft["nonce"])
        await self.ui.callback(query)
        self.assertEqual(self.db.words(), ["SHORT", "LONG", "шорт", "лонг", "ENTRY", "EXIT"])
        await self.ui.callback(query)
        self.assertTrue(query.answer.call_args.kwargs["show_alert"])
        self.assertEqual(self.bot.send_message.await_count, 2)  # main + temporary prompt

    async def test_words_can_be_removed_without_replacing_the_list(self):
        await self.ui.start(self.message())
        await self.ui.callback(self.query("input:words_remove"))
        await self.ui.input_message(self.message(text="short, ЛОНГ", mid=20))
        draft = json.loads(self.db.user(1)["draft"])
        await self.ui.callback(self.query("save:" + draft["nonce"]))
        self.assertEqual(self.db.words(), ["LONG", "шорт"])
        self.assertIn("LONG, шорт", self.bot.edit_message_text.call_args.args[0])

    async def test_invalid_input_deleted_without_extra_messages(self):
        await self.ui.start(self.message())
        await self.ui.callback(self.query("input:schedule"))
        await self.ui.input_message(self.message(text="25:00-07:00", mid=20))
        self.bot.delete_message.assert_any_await(1, 20)
        self.assertIsNone(self.db.user(1)["draft"])
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertIn("Не удалось", self.bot.edit_message_text.call_args.args[0])

    async def test_cancel_removes_prompt_and_draft(self):
        await self.ui.start(self.message())
        await self.ui.callback(self.query("input:source"))
        prompt = self.db.user(1)["prompt_message_id"]
        await self.ui.callback(self.query("home"))
        self.bot.delete_message.assert_any_await(1, prompt)
        self.assertIsNone(self.db.user(1)["pending"])

    async def test_input_instruction_is_only_in_temporary_message(self):
        await self.ui.start(self.message())
        await self.ui.callback(self.query("input:words_add"))
        main_text = self.bot.edit_message_text.call_args.args[0]
        temporary_text = self.bot.send_message.call_args.args[1]
        self.assertIn("Ожидаю ввод", main_text)
        self.assertNotIn("Пришлите слова или фразы", main_text)
        self.assertIn("Пришлите слова или фразы", temporary_text)

    async def test_old_menu_cannot_change_settings(self):
        await self.ui.start(self.message())
        old = self.db.user(1)["main_message_id"]
        await self.ui.start(self.message(mid=11))
        query = self.query("toggle:enabled", mid=old)
        await self.ui.callback(query)
        self.assertTrue(self.db.user(1)["enabled"])
        self.assertTrue(query.answer.call_args.kwargs["show_alert"])

    async def test_admin_actions_denied_even_with_forged_callback(self):
        await self.ui.start(self.message(uid=2))
        for data in ("admin", "input:words_add", "input:words_remove", "input:source", "input:grant",
                     "input:revoke", "remove:1", "source_settings:1", "source_mode:1:all"):
            query = self.query(data, uid=2)
            await self.ui.callback(query)
            self.assertTrue(query.answer.call_args.kwargs["show_alert"])
        self.assertIsNone(self.db.user(2)["pending"])

    async def test_unknown_user_has_only_access_screen(self):
        await self.ui.start(self.message(uid=3))
        self.assertIn("Доступ закрыт", self.bot.edit_message_text.call_args.args[0])
        query = self.query("input:destination", uid=3)
        await self.ui.callback(query)
        self.assertFalse(self.db.user(3)["granted"])
        self.assertTrue(query.answer.call_args.kwargs["show_alert"])

    async def test_grant_can_precede_first_start(self):
        await self.ui.start(self.message())
        await self.ui.callback(self.query("input:grant"))
        await self.ui.input_message(self.message(text="99", mid=20))
        nonce = json.loads(self.db.user(1)["draft"])["nonce"]
        await self.ui.callback(self.query("save:" + nonce))
        self.assertTrue(self.db.user(99)["granted"])
        await self.ui.start(self.message(uid=99))
        self.assertIn("НДА Будилка", self.bot.edit_message_text.call_args.args[0])

    async def test_token_is_not_echoed_and_cancel_discards_it(self):
        await self.ui.start(self.message())
        self.db.update_user(1, destination='{"server":"https://ntfy.sh","topic":"test"}')
        await self.ui.callback(self.query("input:token"))
        await self.ui.input_message(self.message(text="tk_secretvalue", mid=20))
        self.assertNotIn("tk_secretvalue", str(self.bot.edit_message_text.call_args_list))
        self.assertNotIn("tk_secretvalue", str(self.bot.send_message.call_args_list))
        self.bot.delete_message.assert_any_await(1, 20)
        await self.ui.callback(self.query("home"))
        self.assertIsNone(self.db.user(1)["draft"])
        self.assertNotIn("token", json.loads(self.db.user(1)["destination"]))

    async def test_revoked_user_cannot_confirm_pending_setting(self):
        await self.ui.start(self.message(uid=2))
        await self.ui.callback(self.query("input:destination", uid=2))
        await self.ui.input_message(self.message(uid=2, text="https://ntfy.sh/test", mid=20))
        nonce = json.loads(self.db.user(2)["draft"])["nonce"]
        self.db.set_access(2, False)
        await self.ui.callback(self.query("save:" + nonce, uid=2))
        self.assertEqual(json.loads(self.db.user(2)["destination"]), {})

    async def test_source_input_validated_before_confirmation(self):
        await self.ui.start(self.message())
        self.watcher.validate_source.return_value = dict(chat_id=-1001234567890, topic_id=42, title="Forum / signals", forum=True)
        await self.ui.callback(self.query("input:source"))
        await self.ui.input_message(self.message(text="-1001234567890 42", mid=20))
        self.watcher.validate_source.assert_awaited_once_with(-1001234567890, 42)
        self.assertEqual(len(self.db.sources()), 0)
        nonce = json.loads(self.db.user(1)["draft"])["nonce"]
        await self.ui.callback(self.query("save:" + nonce))
        self.assertEqual(self.db.sources()[0]["topic_id"], 42)

    async def test_group_start_ignored_by_router(self):
        dp = Dispatcher()
        dp.include_router(self.ui.router)
        message = TelegramMessage(message_id=1, date=datetime.now(timezone.utc), text="/start",
                                  chat=Chat(id=-1001234567890, type="supergroup"),
                                  from_user=User(id=1, is_bot=False, first_name="Test"))
        await dp.feed_update(self.bot, Update(update_id=1, message=message))
        self.bot.send_message.assert_not_awaited()

    async def test_private_start_dispatched_by_aiogram(self):
        dp = Dispatcher()
        dp.include_router(self.ui.router)
        message = TelegramMessage(message_id=1, date=datetime.now(timezone.utc), text="/start",
                                  chat=Chat(id=1, type="private"),
                                  from_user=User(id=1, is_bot=False, first_name="Test"))
        await dp.feed_update(self.bot, Update(update_id=1, message=message))
        self.bot.send_message.assert_awaited_once()
        self.assertIsNotNone(self.db.user(1)["main_message_id"])

    async def test_revocation_during_prompt_cleanup_blocks_setting(self):
        await self.ui.start(self.message(uid=2))
        self.db.update_user(2, prompt_message_id=55)

        async def revoke_while_deleting(*args, **kwargs):
            self.db.set_access(2, False)

        self.bot.delete_message.side_effect = revoke_while_deleting
        await self.ui.callback(self.query("toggle:night_enabled", uid=2))
        self.assertFalse(self.db.user(2)["night_enabled"])

    async def test_notification_menu_shows_all_priorities_without_sending(self):
        self.db.update_user(1, destination='{"server":"https://ntfy.sh","topic":"test"}')
        await self.ui.start(self.message())
        await self.ui.callback(self.query("test"))
        self.sender.send.assert_not_awaited()
        call = self.bot.edit_message_text.call_args
        text = call.args[0]
        buttons = [button for row in call.kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertEqual([b.callback_data for b in buttons if b.callback_data.startswith("test:")],
                         ["test:1", "test:2", "test:3", "test:4", "test:5"])
        for description in ("1 — минимальный", "2 — низкий", "3 — обычный", "4 — высокий", "5 — максимальный"):
            self.assertIn(description, text)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_explicit_test_priority_does_not_change_settings_or_follow_night_mode(self):
        self.db.update_user(1, destination='{"server":"https://ntfy.sh","topic":"test"}',
                            night_enabled=1, night_priority=2, enabled=0)
        await self.ui.start(self.message())
        before = dict(self.db.user(1))
        for priority in range(1, 6):
            await self.ui.callback(self.query(f"test:{priority}"))
            notification = self.sender.send.call_args.args[1]
            self.assertEqual(notification.priority, priority)
            self.assertIn(str(priority), notification.body)
        self.assertEqual(self.sender.send.await_count, 5)
        for key in ("night_enabled", "night_priority", "night_start", "night_end", "enabled", "destination"):
            self.assertEqual(self.db.user(1)[key], before[key])
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_invalid_test_priority_and_missing_destination_never_send(self):
        await self.ui.start(self.message())
        await self.ui.callback(self.query("test:5"))
        self.db.update_user(1, destination='{"server":"https://ntfy.sh","topic":"test"}')
        for value in ("0", "6", "abc"):
            await self.ui.callback(self.query("test:" + value))
        self.sender.send.assert_not_awaited()

    async def test_failed_notification_keeps_priority_menu_and_displays_error(self):
        self.db.update_user(1, destination='{"server":"https://ntfy.sh","topic":"test"}')
        await self.ui.start(self.message())
        self.sender.send.side_effect = DeliveryError("Нет соединения с ntfy.")
        await self.ui.callback(self.query("test:4"))
        self.assertEqual(self.db.user(1)["last_error"], "Нет соединения с ntfy.")
        self.assertIn("Нет соединения", self.bot.edit_message_text.call_args.args[0])
        self.assertIn("выберите приоритет", self.bot.edit_message_text.call_args.args[0])

    async def test_revoked_user_cannot_send_test(self):
        self.db.update_user(2, destination='{"server":"https://ntfy.sh","topic":"test"}')
        await self.ui.start(self.message(uid=2))
        self.db.set_access(2, False)
        await self.ui.callback(self.query("test:5", uid=2))
        self.sender.send.assert_not_awaited()

    def mock_catalog(self):
        group = dict(chat_id=-1001234567890, title="Forum", forum=True)
        self.ui.catalog = SimpleNamespace(
            groups=AsyncMock(return_value=[group]), group=AsyncMock(return_value=group),
            topics=AsyncMock(return_value=[dict(id=42, title="Signals")]),
            prepare_channel=AsyncMock(return_value=dict(reference="https://t.me/signals", chat_id=-1001234567890,
                                                       title="Channel", join_required=True)),
            confirm_channel=AsyncMock(return_value=dict(chat_id=-1001234567890, topic_id=0, title="Channel", forum=False)))

    async def test_destination_accepts_bare_topic(self):
        await self.ui.start(self.message())
        await self.ui.callback(self.query("input:destination"))
        await self.ui.input_message(self.message(text="my_random_topic", mid=20))
        draft = json.loads(self.db.user(1)["draft"])
        self.assertEqual(draft["value"]["server"], "https://ntfy.sh")
        self.assertEqual(draft["value"]["topic"], "my_random_topic")
        await self.ui.callback(self.query("save:" + draft["nonce"]))
        self.assertEqual(json.loads(self.db.user(1)["destination"])["topic"], "my_random_topic")

    async def test_sources_have_separate_add_buttons(self):
        await self.ui.start(self.message())
        await self.ui.callback(self.query("sources:0"))
        buttons = [b.callback_data for row in self.bot.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("input:channel", buttons)
        self.assertIn("groups", buttons)
        self.assertNotIn("input:source", buttons)

    async def test_source_list_opens_settings_and_changes_trigger_mode(self):
        self.db.add_source(-1001234567890, 42, "Signals", True)
        await self.ui.start(self.message())
        await self.ui.callback(self.query("sources:0"))
        markup = self.bot.edit_message_text.call_args.kwargs["reply_markup"]
        buttons = [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
        self.assertIn(("⚙️ Signals", "source_settings:1"), buttons)
        self.assertFalse(any(text.startswith("Удалить:") for text, _ in buttons))

        await self.ui.callback(self.query("source_settings:1"))
        self.assertIn("По кодовым словам", self.bot.edit_message_text.call_args.args[0])
        await self.ui.callback(self.query("source_mode:1:all"))
        self.assertEqual(self.db.source(1)["trigger_mode"], "all")
        self.assertIn("✅ Все новые сообщения", self.bot.edit_message_text.call_args.args[0])
        settings_buttons = [b.callback_data for row in self.bot.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("remove:1", settings_buttons)

        await self.ui.callback(self.query("remove:1"))
        draft = json.loads(self.db.user(1)["draft"])
        self.assertEqual(draft["kind"], "remove")
        self.assertEqual(len(self.db.sources()), 1)
        await self.ui.callback(self.query("save:" + draft["nonce"]))
        self.assertEqual(len(self.db.sources()), 0)

    async def test_regular_user_cannot_open_or_change_source_settings(self):
        self.db.add_source(-1001234567890, 0, "Channel", False)
        await self.ui.start(self.message(uid=2))
        await self.ui.callback(self.query("sources:0", uid=2))
        buttons = [b.callback_data for row in self.bot.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertNotIn("source_settings:1", buttons)
        query = self.query("source_mode:1:all", uid=2)
        await self.ui.callback(query)
        self.assertEqual(self.db.source(1)["trigger_mode"], "keywords")
        self.assertTrue(query.answer.call_args.kwargs["show_alert"])

    async def test_group_then_topic_then_confirmation(self):
        self.mock_catalog()
        self.watcher.validate_source.return_value = dict(chat_id=-1001234567890, topic_id=42, title="Forum / Signals", forum=True)
        await self.ui.start(self.message())
        await self.ui.callback(self.query("groups"))
        groups = json.loads(self.db.user(1)["draft"])
        self.assertEqual(groups["kind"], "groups")
        self.ui.catalog.topics.assert_not_awaited()
        await self.ui.callback(self.query(f"gselect:{groups['nonce']}:0"))
        topics = json.loads(self.db.user(1)["draft"])
        self.ui.catalog.topics.assert_awaited_once_with(-1001234567890)
        self.assertEqual(topics["kind"], "topics")
        await self.ui.callback(self.query(f"tselect:{topics['nonce']}:0"))
        draft = json.loads(self.db.user(1)["draft"])
        self.watcher.validate_source.assert_awaited_once_with(-1001234567890, 42)
        self.assertEqual(len(self.db.sources()), 0)
        await self.ui.callback(self.query("save:" + draft["nonce"]))
        self.assertEqual(self.db.sources()[0]["topic_id"], 42)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_manual_group_id_opens_topics_and_deletes_input(self):
        self.mock_catalog()
        await self.ui.start(self.message())
        await self.ui.callback(self.query("input:group"))
        prompt = self.db.user(1)["prompt_message_id"]
        await self.ui.input_message(self.message(text="-1001234567890", mid=20))
        self.assertEqual(json.loads(self.db.user(1)["draft"])["kind"], "topics")
        self.bot.delete_message.assert_any_await(1, prompt)
        self.bot.delete_message.assert_any_await(1, 20)

    async def test_group_pages_preserve_selection_and_expired_buttons_rejected(self):
        self.mock_catalog()
        group = self.ui.catalog.group.return_value
        self.ui.catalog.groups.return_value = [group | {"chat_id": -1001234567890 - n} for n in range(10)]
        await self.ui.start(self.message())
        await self.ui.callback(self.query("groups"))
        state = json.loads(self.db.user(1)["draft"])
        await self.ui.callback(self.query(f"gpage:{state['nonce']}:1"))
        self.assertEqual(json.loads(self.db.user(1)["draft"]), state)
        self.ui.catalog.groups.assert_awaited_once()
        await self.ui.callback(self.query("home"))
        stale = self.query(f"gselect:{state['nonce']}:9")
        await self.ui.callback(stale)
        self.assertTrue(stale.answer.call_args.kwargs["show_alert"])
        self.ui.catalog.topics.assert_not_awaited()

    async def test_channel_subscription_only_after_confirmation(self):
        self.mock_catalog()
        await self.ui.start(self.message())
        await self.ui.callback(self.query("input:channel"))
        await self.ui.input_message(self.message(text="https://t.me/signals", mid=20))
        draft = json.loads(self.db.user(1)["draft"])
        self.ui.catalog.confirm_channel.assert_not_awaited()
        self.assertEqual(len(self.db.sources()), 0)
        await self.ui.callback(self.query("save:" + draft["nonce"]))
        self.ui.catalog.confirm_channel.assert_awaited_once_with(draft["value"])
        self.assertEqual(self.db.sources()[0]["title"], "Channel")

    async def test_cancelled_channel_never_subscribed(self):
        self.mock_catalog()
        await self.ui.start(self.message())
        await self.ui.callback(self.query("input:channel"))
        await self.ui.input_message(self.message(text="https://t.me/signals", mid=20))
        await self.ui.callback(self.query("home"))
        self.ui.catalog.confirm_channel.assert_not_awaited()

    async def test_failed_subscription_not_saved_as_active_source(self):
        self.mock_catalog()
        self.ui.catalog.confirm_channel.side_effect = ValueError("Заявка отправлена. Дождитесь одобрения.")
        await self.ui.start(self.message())
        await self.ui.callback(self.query("input:channel"))
        await self.ui.input_message(self.message(text="https://t.me/signals", mid=20))
        draft = json.loads(self.db.user(1)["draft"])
        await self.ui.callback(self.query("save:" + draft["nonce"]))
        self.assertEqual(len(self.db.sources()), 0)
        self.assertIn("Дождитесь одобрения", self.bot.edit_message_text.call_args.args[0])

    async def test_unexpected_subscription_error_keeps_draft_for_retry(self):
        self.mock_catalog()
        source = dict(chat_id=-1001234567890, topic_id=0, title="Channel", forum=False)
        self.ui.catalog.confirm_channel.side_effect = [AttributeError("private-data"), source]
        await self.ui.start(self.message())
        await self.ui.callback(self.query("input:channel"))
        await self.ui.input_message(self.message(text="https://t.me/+invite", mid=20))
        first_draft = json.loads(self.db.user(1)["draft"])

        with self.assertLogs("ndabudilka.ui", level="ERROR") as captured:
            await self.ui.callback(self.query("save:" + first_draft["nonce"]))
        retry_draft = json.loads(self.db.user(1)["draft"])
        self.assertEqual(retry_draft["value"], first_draft["value"])
        self.assertNotEqual(retry_draft["nonce"], first_draft["nonce"])
        self.assertEqual(len(self.db.sources()), 0)
        self.assertIn("AttributeError", "\n".join(captured.output))
        self.assertNotIn("private-data", self.bot.edit_message_text.call_args.args[0])

        await self.ui.callback(self.query("save:" + retry_draft["nonce"]))
        self.assertEqual(len(self.db.sources()), 1)
        self.assertEqual(self.db.sources()[0]["title"], "Channel")
        self.assertEqual(self.ui.catalog.confirm_channel.await_count, 2)

    async def test_new_source_actions_are_admin_only(self):
        self.mock_catalog()
        await self.ui.start(self.message(uid=2))
        for data in ("groups", "input:channel", "input:group", "source_settings:1", "source_mode:1:all",
                     "gselect:fake:0", "tselect:fake:0", "gpage:fake:1", "tpage:fake:1"):
            query = self.query(data, uid=2)
            await self.ui.callback(query)
            self.assertTrue(query.answer.call_args.kwargs["show_alert"])
        self.ui.catalog.groups.assert_not_awaited()
        self.ui.catalog.topics.assert_not_awaited()
