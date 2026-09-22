from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from telethon import functions, types
from telethon.errors import FloodWaitError, InviteRequestSentError, UserNotParticipantError

from ndabudilka.sources import SourceCatalog, channel_reference

CHAT = -1001234567890


def channel(*, group=False, forum=False, title="Signals", ident=1234567890):
    return types.Channel(id=ident, title=title, photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc),
                         broadcast=not group, megagroup=group, forum=forum)


def topic(ident, title):
    return types.ForumTopic(
        id=ident, date=datetime.now(timezone.utc), peer=types.PeerChannel(1234567890), title=title,
        icon_color=0, top_message=ident + 100, read_inbox_max_id=0, read_outbox_max_id=0,
        unread_count=0, unread_mentions_count=0, unread_reactions_count=0, unread_poll_votes_count=0,
        from_id=types.PeerUser(1), notify_settings=types.PeerNotifySettings())


class ReferenceTests(unittest.TestCase):
    def test_supported_links(self):
        for value in ("https://t.me/channel", "t.me/channel", "https://telegram.me/channel/", "@channel"):
            self.assertEqual(channel_reference(value), ("public", "channel"))
        for value in ("https://t.me/+abcdef", "https://t.me/joinchat/abcdef"):
            self.assertEqual(channel_reference(value), ("invite", "abcdef"))
        self.assertEqual(channel_reference(str(CHAT)), ("id", CHAT))

    def test_not_a_channel_reference(self):
        for value in ("https://evil.example/channel", "https://t.me/channel/123", "https://t.me/c/123/45",
                      "https://t.me/channel?x=1", "https://t.me@evil.example/channel", str(CHAT) + " 42"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                channel_reference(value)


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.entity = channel()
        self.joined = False
        self.dialogs = []
        self.requests = []
        self.client = AsyncMock()
        self.client.is_connected = Mock(return_value=True)
        self.client.get_entity.return_value = self.entity
        self.invite = types.ChatInvite(title="Private", photo=types.PhotoEmpty(id=1), participants_count=1,
                                      color=0, broadcast=True, channel=True)
        self.pages = []

        async def dialogs():
            for entity in self.dialogs:
                yield SimpleNamespace(id=-(1000000000000 + entity.id), entity=entity)

        async def call(request):
            self.requests.append(request)
            if isinstance(request, functions.channels.GetParticipantRequest):
                if not self.joined:
                    raise UserNotParticipantError(None)
                return SimpleNamespace(participant=SimpleNamespace())
            if isinstance(request, functions.channels.JoinChannelRequest):
                self.joined = True
                self.dialogs = [self.entity]
                return SimpleNamespace(chats=[self.entity])
            if isinstance(request, functions.messages.CheckChatInviteRequest):
                return types.ChatInviteAlready(self.entity) if self.joined else self.invite
            if isinstance(request, functions.messages.ImportChatInviteRequest):
                self.joined = True
                self.dialogs = [self.entity]
                return SimpleNamespace(chats=[self.entity])
            if isinstance(request, functions.messages.GetForumTopicsRequest):
                return self.pages.pop(0)
            raise AssertionError(type(request))

        self.client.iter_dialogs = dialogs
        self.client.side_effect = call
        self.catalog = SourceCatalog(self.client)

    def mutations(self):
        return [r for r in self.requests if isinstance(r, (functions.channels.JoinChannelRequest,
                                                          functions.messages.ImportChatInviteRequest))]

    async def test_public_preview_does_not_join_confirmation_does(self):
        draft = await self.catalog.prepare_channel("https://t.me/signals")
        self.assertTrue(draft["join_required"])
        self.assertEqual(self.mutations(), [])
        result = await self.catalog.confirm_channel(draft)
        self.assertEqual(result, dict(chat_id=CHAT, topic_id=0, title="Signals", forum=False))
        self.assertEqual(len(self.mutations()), 1)

    async def test_existing_channel_not_joined_again(self):
        self.joined = True
        self.dialogs = [self.entity]
        for reference in ("https://t.me/signals", str(CHAT)):
            draft = await self.catalog.prepare_channel(reference)
            self.assertFalse(draft["join_required"])
            await self.catalog.confirm_channel(draft)
        self.assertEqual(self.mutations(), [])

    async def test_private_invite_checked_before_join(self):
        draft = await self.catalog.prepare_channel("https://t.me/+abcdef")
        self.assertIsNone(draft["chat_id"])
        self.assertEqual(self.mutations(), [])
        result = await self.catalog.confirm_channel(draft)
        self.assertEqual(result["chat_id"], CHAT)
        self.assertIsInstance(self.mutations()[0], functions.messages.ImportChatInviteRequest)

    async def test_private_existing_member_does_not_rejoin(self):
        self.joined = True
        self.dialogs = [self.entity]
        draft = await self.catalog.prepare_channel("https://t.me/+abcdef")
        self.assertFalse(draft["join_required"])
        await self.catalog.confirm_channel(draft)
        self.assertEqual(self.mutations(), [])

    async def test_private_join_short_updates_and_delayed_dialog_list(self):
        draft = await self.catalog.prepare_channel("https://t.me/+abcdef")
        original = self.client.side_effect

        async def short_updates(request):
            result = await original(request)
            if isinstance(request, functions.messages.ImportChatInviteRequest):
                self.dialogs = []  # Membership is confirmed before the dialog appears.
                return types.UpdatesTooLong()
            return result

        self.client.side_effect = short_updates
        result = await self.catalog.confirm_channel(draft)
        self.assertEqual(result["chat_id"], CHAT)
        self.assertEqual(len(self.mutations()), 1)

    async def test_private_existing_member_without_dialog_entry(self):
        self.joined = True
        draft = await self.catalog.prepare_channel("https://t.me/+abcdef")
        result = await self.catalog.confirm_channel(draft)
        self.assertEqual(result["chat_id"], CHAT)
        self.assertEqual(self.mutations(), [])

    async def test_group_link_rejected_without_join(self):
        self.client.get_entity.return_value = channel(group=True)
        with self.assertRaisesRegex(ValueError, "не канал"):
            await self.catalog.prepare_channel("https://t.me/signals")
        self.invite.broadcast = False
        self.invite.megagroup = True
        with self.assertRaisesRegex(ValueError, "ведёт в группу"):
            await self.catalog.prepare_channel("https://t.me/+abcdef")
        self.assertEqual(self.mutations(), [])

    async def test_paid_invite_rejected_without_join(self):
        self.invite.subscription_pricing = SimpleNamespace(amount=100)
        with self.assertRaisesRegex(ValueError, "Платные"):
            await self.catalog.prepare_channel("https://t.me/+abcdef")
        self.assertEqual(self.mutations(), [])

    async def test_changed_public_link_not_joined(self):
        draft = await self.catalog.prepare_channel("https://t.me/signals")
        self.client.get_entity.return_value = channel(ident=99999)
        with self.assertRaisesRegex(ValueError, "другой канал"):
            await self.catalog.confirm_channel(draft)
        self.assertEqual(self.mutations(), [])

    async def test_request_to_join_is_not_reported_as_subscription(self):
        draft = await self.catalog.prepare_channel("https://t.me/+abcdef")
        original = self.client.side_effect

        async def need_approval(request):
            if isinstance(request, functions.messages.ImportChatInviteRequest):
                raise InviteRequestSentError(None)
            return await original(request)

        self.client.side_effect = need_approval
        with self.assertRaisesRegex(ValueError, "После одобрения"):
            await self.catalog.confirm_channel(draft)

    async def test_rate_limit_shows_wait(self):
        self.client.get_entity.side_effect = FloodWaitError(None, capture=30)
        with self.assertRaisesRegex(ValueError, "30 сек"):
            await self.catalog.prepare_channel("https://t.me/signals")

    async def test_groups_exclude_channels_and_left_groups(self):
        left = channel(group=True, title="Left")
        left.left = True
        self.dialogs = [channel(), channel(group=True, title="Group"), left]
        groups = await self.catalog.groups()
        self.assertEqual([g["title"] for g in groups], ["Group"])
        self.assertEqual(self.mutations(), [])

    async def test_group_id_rejects_channel(self):
        self.dialogs = [self.entity]
        with self.assertRaisesRegex(ValueError, "не относится к супергруппе"):
            await self.catalog.group(CHAT)

    async def test_topic_pagination_uses_last_message_date(self):
        self.dialogs = [channel(group=True, forum=True)]
        first, general = topic(42, "Signals"), topic(1, "General")
        last_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.pages = [SimpleNamespace(topics=[first], count=2, order_by_create_date=False,
                                      messages=[SimpleNamespace(id=142, date=last_date)]),
                      SimpleNamespace(topics=[general], count=2)]
        result = await self.catalog.topics(CHAT)
        self.assertEqual([t["id"] for t in result], [1, 42])
        second_request = self.requests[1]
        self.assertEqual((second_request.offset_id, second_request.offset_topic, second_request.offset_date), (142, 42, last_date))

    async def test_no_topics_for_nonforum_group(self):
        self.dialogs = [channel(group=True)]
        self.assertEqual(await self.catalog.topics(CHAT), [])
        self.client.assert_not_awaited()
