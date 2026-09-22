from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from telethon import events, types

from ndabudilka.delivery import DeliveryWorker
from ndabudilka.notifications import DeliveryError
from ndabudilka.storage import Database
from ndabudilka.watcher import Watcher

CHAT = -1001234567890


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:", 1)
        self.db.set_access(2, True)
        for uid in (1, 2):
            self.db.update_user(uid, destination=json.dumps({"server": "https://ntfy.sh", "topic": f"test-{uid}"}))
        self.sender = SimpleNamespace(send=AsyncMock())
        self.worker = DeliveryWorker(self.db, self.sender)
        self.client = Mock()
        self.watcher = Watcher(self.client, self.db, self.worker)
        self.db.add_source(CHAT, 42, "Signals", True)

    async def asyncTearDown(self):
        self.db.close()

    def message(self, mid=10, topic=42, text="SHORT and long"):
        return types.Message(id=mid, peer_id=types.PeerChannel(1234567890),
                             date=datetime.now(timezone.utc), message=text,
                             reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=topic))

    def test_subscribes_only_to_new_messages(self):
        handler = self.client.add_event_handler.call_args.args[1]
        self.assertIs(type(handler), events.NewMessage)

    def test_only_selected_chat_and_topic(self):
        self.assertEqual(self.watcher.process_message(CHAT - 1, self.message()), 0)
        self.assertEqual(self.watcher.process_message(CHAT, self.message(topic=43)), 0)
        self.assertEqual(self.watcher.process_message(CHAT, self.message()), 2)

    def test_notification_body_is_source_then_original_text(self):
        text = "LONG 🚀\nВторая строка"
        self.watcher.process_message(CHAT, self.message(text=text))
        self.assertEqual(self.db.due()[0]["body"], "Signals: " + text)
        self.assertNotIn("Совпадения", self.db.due()[0]["body"])


    def test_one_notification_per_message_per_user_and_overlapping_sources(self):
        self.db.add_source(CHAT, 0, "Entire forum", True)
        message = self.message()
        self.assertEqual(self.watcher.process_message(CHAT, message), 2)
        self.assertEqual(self.watcher.process_message(CHAT, message), 0)
        self.assertEqual(len(self.db.due()), 2)
        self.assertIn("/42/10", self.db.due()[0]["link"])

    def test_ungranted_paused_and_unconfigured_users(self):
        self.db.ensure_user(3)
        self.db.update_user(3, destination='{"topic":"hidden"}')
        self.db.update_user(2, enabled=0)
        self.assertEqual(self.watcher.process_message(CHAT, self.message()), 1)
        self.db.update_user(1, destination="{}")
        self.assertEqual(self.watcher.process_message(CHAT, self.message(mid=11)), 0)

    def test_whole_forum_includes_general_and_future_topics(self):
        self.db.add_source(CHAT, 0, "Forum", True)
        self.assertEqual(self.watcher.process_message(CHAT, self.message(topic=1024)), 2)
        general = self.message(mid=11)
        general.reply_to = None
        self.assertEqual(self.watcher.process_message(CHAT, general), 2)

    def test_old_history_and_nonmatching_text_ignored(self):
        old = self.message()
        old.date = self.watcher.started_at - timedelta(days=1)
        self.assertEqual(self.watcher.process_message(CHAT, old), 0)
        self.assertEqual(self.watcher.process_message(CHAT, self.message(text="longer")), 0)

    def test_all_messages_mode_includes_nonmatching_and_captionless_posts(self):
        source_id = self.db.sources()[0]["id"]
        self.db.set_source_mode(source_id, "all")
        self.assertEqual(self.watcher.process_message(CHAT, self.message(text="No signal here")), 2)
        self.assertEqual(self.watcher.process_message(CHAT, self.message(mid=11, text=None)), 2)
        bodies = [item["body"] for item in self.db.due()]
        self.assertIn("Signals: No signal here", bodies)
        self.assertIn("Signals: Новое сообщение без текста", bodies)

    def test_specific_topic_mode_overrides_whole_group_mode(self):
        self.db.add_source(CHAT, 0, "Whole forum", True, trigger_mode="all")
        self.assertEqual(self.watcher.process_message(CHAT, self.message(text="ordinary")), 0)
        self.assertEqual(self.watcher.process_message(CHAT, self.message(topic=99, text="ordinary")), 2)

    async def test_delivery_uses_current_night_settings_and_destination(self):
        self.watcher.process_message(CHAT, self.message())
        self.db.update_user(1, night_enabled=1, night_priority=1)
        item = self.db.due()[0]
        await self.worker.deliver(item, datetime(2026, 9, 21, 22, tzinfo=timezone.utc))
        destination, notification = self.sender.send.call_args.args
        self.assertEqual(notification.priority, 1)
        self.assertEqual(destination["topic"], "test-1")
        self.assertEqual(self.db.counts(1), {"sent": 1})
        self.assertEqual(self.db.connection.execute("SELECT body FROM deliveries WHERE id=?", (item["id"],)).fetchone()[0], "")

    async def test_signal_and_delivery_logs_include_source_but_not_post_text(self):
        with self.assertLogs("ndabudilka.watcher", level="INFO") as watcher_logs:
            self.watcher.process_message(CHAT, self.message(text="SHORT private payload"))
        watcher_text = "\n".join(watcher_logs.output)
        self.assertIn("источник='Signals'", watcher_text)
        self.assertIn("получателей=2", watcher_text)
        self.assertNotIn("private payload", watcher_text)

        with self.assertLogs("ndabudilka.delivery", level="INFO") as delivery_logs:
            await self.worker.deliver(self.db.due()[0])
        delivery_text = "\n".join(delivery_logs.output)
        self.assertIn("Отправка уведомления", delivery_text)
        self.assertIn("Уведомление доставлено", delivery_text)
        self.assertIn("источник='Signals'", delivery_text)
        self.assertIn("приоритет=5", delivery_text)
        self.assertNotIn("private payload", delivery_text)

    async def test_revocation_blocks_already_queued_delivery(self):
        self.watcher.process_message(CHAT, self.message())
        item = self.db.due()[1]
        self.db.set_access(2, False)
        await self.worker.deliver(item)
        self.sender.send.assert_not_awaited()

    async def test_source_removal_cancels_delivery(self):
        self.watcher.process_message(CHAT, self.message())
        item = self.db.due()[0]
        self.db.remove_source(self.db.sources()[0]["id"])
        await self.worker.deliver(item)
        self.sender.send.assert_not_awaited()
        self.assertEqual(self.db.counts(1), {"cancelled": 1})

    async def test_transient_failure_retried_then_delivered(self):
        self.watcher.process_message(CHAT, self.message())
        item = self.db.due()[0]
        self.sender.send.side_effect = DeliveryError("Temporary", True, 30)
        await self.worker.deliver(item)
        row = self.db.connection.execute("SELECT * FROM deliveries WHERE id=?", (item["id"],)).fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["state"], "pending")
        self.assertGreater(row["next_attempt"], item["created_at"])
        self.sender.send.side_effect = None
        await self.worker.deliver(row)
        self.assertEqual(self.db.counts(1), {"sent": 1})

    async def test_permanent_failure_not_retried(self):
        self.watcher.process_message(CHAT, self.message())
        self.sender.send.side_effect = DeliveryError("No access")
        await self.worker.deliver(self.db.due()[0])
        self.assertEqual(self.db.counts(1), {"failed": 1})

    async def test_retry_limit(self):
        self.watcher.process_message(CHAT, self.message())
        item = dict(self.db.due()[0])
        item["attempts"] = 5
        self.sender.send.side_effect = DeliveryError("Temporary", True)
        await self.worker.deliver(item)
        self.assertEqual(self.db.counts(1), {"failed": 1})

    async def test_expired_delivery_is_not_sent(self):
        self.watcher.process_message(CHAT, self.message())
        item = dict(self.db.due()[0])
        item["created_at"] -= 3601
        await self.worker.deliver(item)
        self.sender.send.assert_not_awaited()

    def test_admin_cannot_be_revoked(self):
        with self.assertRaises(ValueError):
            self.db.set_access(1, False)


class PersistenceTests(unittest.TestCase):
    def test_settings_and_dedup_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.sqlite3"
            db = Database(path, 1)
            db.set_access(2, True)
            db.set_words(["LONG"])
            db.update_user(2, night_enabled=1, main_message_id=123, destination='{"topic":"a"}')
            db.enqueue(2, CHAT, 10, 42, "title", "body", "link")
            db.close()
            db = Database(path, 1)
            try:
                self.assertEqual(db.words(), ["LONG"])
                self.assertEqual(db.user(2)["main_message_id"], 123)
                self.assertEqual(db.user(2)["night_enabled"], 1)
                self.assertFalse(db.enqueue(2, CHAT, 10, 42, "title", "body", "link"))
                self.assertEqual(len(db.due()), 1)
            finally:
                db.close()

    def test_old_sources_are_migrated_to_keyword_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
                               "topic_id INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL, forum INTEGER NOT NULL DEFAULT 0, "
                               "UNIQUE(chat_id, topic_id))")
            connection.execute("INSERT INTO sources(chat_id,topic_id,title,forum) VALUES (?,?,?,?)",
                               (CHAT, 42, "Existing", 1))
            connection.commit()
            connection.close()
            db = Database(path, 1)
            try:
                self.assertEqual(db.sources()[0]["trigger_mode"], "keywords")
                db.set_source_mode(1, "all")
                db.add_source(CHAT, 42, "Renamed", True)
                self.assertEqual(db.source(1)["trigger_mode"], "all")
            finally:
                db.close()

    def test_words_are_added_and_removed_case_insensitively(self):
        db = Database(":memory:", 1)
        try:
            self.assertEqual(db.add_words(["short", "ENTRY"]), ["ENTRY"])
            self.assertEqual(db.words()[-1], "ENTRY")
            self.assertEqual(db.remove_words(["entry", "ЛОНГ"]), ["лонг", "ENTRY"])
            self.assertNotIn("ENTRY", db.words())
            self.assertNotIn("лонг", db.words())
        finally:
            db.close()


class SourceValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:", 1)
        self.channel = types.Channel(id=1234567890, title="Forum", photo=types.ChatPhotoEmpty(),
                                     date=datetime.now(timezone.utc), forum=True, megagroup=True)
        self.dialogs = [SimpleNamespace(id=CHAT, entity=self.channel)]

        async def dialogs():
            for dialog in self.dialogs:
                yield dialog

        self.client = AsyncMock()
        self.client.is_connected = Mock(return_value=True)
        self.client.add_event_handler = Mock()
        self.client.iter_dialogs = dialogs
        self.watcher = Watcher(self.client, self.db, SimpleNamespace(wakeup=Mock()))

    async def asyncTearDown(self):
        self.db.close()

    async def test_existing_topic_is_resolved_with_current_telethon_api(self):
        topic = types.ForumTopic(
            id=42, date=datetime.now(timezone.utc), peer=types.PeerChannel(1234567890),
            title="Signals", icon_color=0, top_message=100, read_inbox_max_id=0,
            read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
            unread_reactions_count=0, unread_poll_votes_count=0, from_id=types.PeerUser(1),
            notify_settings=types.PeerNotifySettings(),
        )
        self.client.return_value = SimpleNamespace(topics=[topic])
        result = await self.watcher.validate_source(CHAT, 42)
        self.assertEqual(result, dict(chat_id=CHAT, topic_id=42, title="Forum / Signals", forum=True))
        self.assertEqual(self.client.call_args.args[0].topics, [42])

    async def test_not_subscribed_is_rejected(self):
        self.dialogs.clear()
        with self.assertRaises(ValueError):
            await self.watcher.validate_source(CHAT, 0)
        self.client.assert_not_awaited()

    async def test_deleted_topic_is_rejected(self):
        self.client.return_value = SimpleNamespace(topics=[types.ForumTopicDeleted(id=42)])
        with self.assertRaises(ValueError):
            await self.watcher.validate_source(CHAT, 42)

    async def test_topic_on_non_forum_rejected(self):
        self.channel.forum = False
        with self.assertRaises(ValueError):
            await self.watcher.validate_source(CHAT, 42)
        result = await self.watcher.validate_source(CHAT, 0)
        self.assertFalse(result["forum"])
