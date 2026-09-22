"""Telegram source discovery and explicit channel subscription."""

import asyncio
from functools import wraps
import logging
import re
from urllib.parse import urlsplit

from telethon import functions, types, utils
from telethon.errors import (FloodWaitError, InviteRequestSentError, RPCError,
                             UserAlreadyParticipantError, UserNotParticipantError)

from .rules import parse_source

log = logging.getLogger(__name__)


def telegram_action(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        if not self.client.is_connected():
            raise ValueError("Аккаунт чтения не подключён.")
        try:
            async with asyncio.timeout(40):
                return await method(self, *args, **kwargs)
        except FloodWaitError as error:
            raise ValueError(f"Telegram просит подождать {error.seconds} сек. Повторите позже.") from None
        except InviteRequestSentError:
            raise ValueError("Заявка на вступление отправлена. После одобрения добавьте канал по этой ссылке ещё раз.") from None
        except RPCError as error:
            raise ValueError(f"Telegram не выполнил действие ({type(error).__name__}). Проверьте ссылку и доступ.") from None
        except (OSError, asyncio.TimeoutError):
            raise ValueError("Не удалось связаться с Telegram. Повторите действие позже.") from None
    return wrapped


def channel_reference(value: str) -> tuple[str, str | int]:
    value = value.strip()
    if value.startswith("-100"):
        chat_id, topic_id = parse_source(value)
        if topic_id or len(value.split()) != 1:
            raise ValueError("Для канала нужен только его ID, без ID ветки.")
        return "id", chat_id
    if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", value):
        return "public", value[1:]
    if value.startswith(("t.me/", "telegram.me/")):
        value = "https://" + value
    try:
        url = urlsplit(value)
        valid = (url.scheme == "https" and url.hostname in {"t.me", "telegram.me"}
                 and not url.username and not url.password and not url.port
                 and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Пришлите ссылку https://t.me/channel, приглашение https://t.me/+… или ID канала.")
    path = url.path.strip("/")
    if path.startswith("+") or path.startswith("joinchat/"):
        invite_hash = path[1:] if path.startswith("+") else path[len("joinchat/"):]
        if re.fullmatch(r"[A-Za-z0-9_-]{5,256}", invite_hash):
            return "invite", invite_hash
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", path):
        return "public", path
    raise ValueError("Нужна ссылка на сам канал или приглашение, а не на сообщение или ветку.")


def require_channel(entity):
    if not isinstance(entity, types.Channel) or not entity.broadcast or entity.megagroup:
        raise ValueError("Это не канал. Для супергруппы используйте «Добавить супергруппу».")


def source_value(entity) -> dict:
    return dict(chat_id=utils.get_peer_id(entity), topic_id=0, title=entity.title[:250], forum=False)


class SourceCatalog:
    def __init__(self, client):
        self.client = client

    async def member_entity(self, chat_id: int):
        async for dialog in self.client.iter_dialogs():
            if dialog.id == chat_id and not getattr(dialog.entity, "left", False):
                return dialog.entity
        raise ValueError("Источник не найден среди подписок аккаунта. Для нового канала укажите ссылку.")

    async def public_entity(self, reference: str):
        try:
            entity = await self.client.get_entity(reference)
        except ValueError:
            raise ValueError("Канал по этой ссылке не найден.") from None
        require_channel(entity)
        return entity

    async def joined_invite_entity(self, invite_hash: str):
        invite = await self.client(functions.messages.CheckChatInviteRequest(invite_hash))
        if not isinstance(invite, types.ChatInviteAlready):
            raise ValueError("Подписка ещё не подтверждена. После одобрения заявки повторите сохранение.")
        require_channel(invite.chat)
        return invite.chat

    async def is_member(self, entity) -> bool:
        try:
            result = await self.client(functions.channels.GetParticipantRequest(entity, types.InputPeerSelf()))
            return (not isinstance(result.participant, types.ChannelParticipantLeft)
                    and not getattr(result.participant, "left", False))
        except UserNotParticipantError:
            return False

    async def inspect_channel(self, reference: str) -> dict:
        kind, target = channel_reference(reference)
        if kind == "invite":
            invite = await self.client(functions.messages.CheckChatInviteRequest(target))
            if isinstance(invite, (types.ChatInviteAlready, types.ChatInvitePeek)):
                entity = invite.chat
                require_channel(entity)
                chat_id, title = utils.get_peer_id(entity), entity.title
                joined = isinstance(invite, types.ChatInviteAlready)
            elif isinstance(invite, types.ChatInvite):
                if not invite.broadcast or invite.megagroup:
                    raise ValueError("Приглашение ведёт в группу. Используйте «Добавить супергруппу».")
                if invite.subscription_pricing:
                    raise ValueError("Платные приглашения не поддерживаются. Подпишитесь самостоятельно и добавьте канал по ID.")
                chat_id, title, joined = None, invite.title, False
            else:
                raise ValueError("Не удалось проверить приглашение.")
        else:
            entity = await self.member_entity(target) if kind == "id" else await self.public_entity(target)
            require_channel(entity)
            chat_id, title = utils.get_peer_id(entity), entity.title
            joined = kind == "id" or await self.is_member(entity)
        return dict(reference=reference, chat_id=chat_id, title=title[:250], join_required=not joined)

    @telegram_action
    async def prepare_channel(self, reference: str) -> dict:
        return await self.inspect_channel(reference.strip())

    @telegram_action
    async def confirm_channel(self, draft: dict) -> dict:
        current = await self.inspect_channel(draft["reference"])
        if draft["chat_id"] is not None and current["chat_id"] != draft["chat_id"]:
            raise ValueError("Ссылка теперь ведёт в другой канал. Добавьте его заново для проверки.")
        kind, target = channel_reference(draft["reference"])
        if current["join_required"]:
            log.info("Подписка на канал: источник=%r", current["title"])
            try:
                if kind == "invite":
                    result = await self.client(functions.messages.ImportChatInviteRequest(target))
                    channels = [c for c in getattr(result, "chats", []) if isinstance(c, types.Channel) and c.broadcast and not c.megagroup]
                    # UpdatesTooLong / UpdateShort have no chats. Resolve the joined
                    # invite directly, without waiting for the dialog list to catch up.
                    entity = channels[0] if len(channels) == 1 else await self.joined_invite_entity(target)
                else:
                    entity = await self.public_entity(target)
                    if utils.get_peer_id(entity) != current["chat_id"]:
                        raise ValueError("Канал изменился. Добавьте его заново.")
                    await self.client(functions.channels.JoinChannelRequest(entity))
            except UserAlreadyParticipantError:
                entity = await self.joined_invite_entity(target) if kind == "invite" else await self.public_entity(target)
        else:
            if kind == "invite":
                entity = await self.joined_invite_entity(target)
            elif kind == "public":
                entity = await self.public_entity(target)
            else:
                entity = await self.member_entity(current["chat_id"])
        require_channel(entity)
        if current["chat_id"] is not None and utils.get_peer_id(entity) != current["chat_id"]:
            raise ValueError("Канал изменился. Добавьте его заново.")
        if not await self.is_member(entity):
            raise ValueError("Подписка ещё не подтверждена. После одобрения заявки добавьте канал повторно.")
        log.info("Подписка подтверждена: источник=%r chat_id=%s", entity.title, utils.get_peer_id(entity))
        return source_value(entity)

    @telegram_action
    async def groups(self) -> list[dict]:
        groups = []
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, types.Channel) and entity.megagroup and not entity.left:
                groups.append(dict(chat_id=dialog.id, title=entity.title[:250], forum=bool(entity.forum)))
        return sorted(groups, key=lambda item: (item["title"].casefold(), item["chat_id"]))

    @telegram_action
    async def group(self, chat_id: int) -> dict:
        entity = await self.member_entity(chat_id)
        if not isinstance(entity, types.Channel) or not entity.megagroup:
            raise ValueError("Этот ID не относится к супергруппе аккаунта.")
        return dict(chat_id=chat_id, title=entity.title[:250], forum=bool(entity.forum))

    @telegram_action
    async def topics(self, chat_id: int) -> list[dict]:
        entity = await self.member_entity(chat_id)
        if not isinstance(entity, types.Channel) or not entity.megagroup:
            raise ValueError("Выберите супергруппу.")
        if not entity.forum:
            return []
        offset_date, offset_id, offset_topic = None, 0, 0
        topics = {}
        while True:
            result = await self.client(functions.messages.GetForumTopicsRequest(
                entity, offset_date=offset_date, offset_id=offset_id, offset_topic=offset_topic, limit=100))
            page = [topic for topic in result.topics if isinstance(topic, types.ForumTopic)]
            if not page:
                break
            previous_count = len(topics)
            for topic in page:
                topics[topic.id] = dict(id=topic.id, title=topic.title[:100])
            if len(topics) == previous_count:
                raise ValueError("Telegram повторил страницу веток. Обновите список позже.")
            if len(topics) >= result.count:
                break
            last = page[-1]
            if result.order_by_create_date:
                offset_date = last.date
            else:
                message = next((m for m in result.messages if m.id == last.top_message), None)
                if message is None:
                    message = await self.client.get_messages(entity, ids=last.top_message)
                if message is None or message.date is None:
                    raise ValueError("Не удалось получить следующую страницу веток. Повторите позже.")
                offset_date = message.date
            offset_id, offset_topic = last.top_message, last.id
        return sorted(topics.values(), key=lambda item: (item["id"] != 1, item["title"].casefold(), item["id"]))
