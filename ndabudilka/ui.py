import asyncio
from html import escape
import json
import logging
import secrets

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Config
from .notifications import DeliveryError, Notification
from .notifications.ntfy import NtfySender, parse_destination, validate_token
from .rules import format_time, parse_schedule, parse_source, parse_words, priority_for
from .storage import Database
from .sources import SourceCatalog
from .watcher import Watcher

log = logging.getLogger(__name__)
PRIORITIES = {1: "1 — минимальный", 2: "2 — низкий", 3: "3 — обычный", 4: "4 — высокий", 5: "5 — максимальный"}
PRIORITY_HELP = (
    "1 — минимальный: без звука и вибрации, в разделе тихих/прочих уведомлений.\n"
    "2 — низкий: без звука и вибрации, виден при открытии шторки.\n"
    "3 — обычный: стандартный звук и короткая вибрация.\n"
    "4 — высокий: звук, длинная вибрация и всплывающее уведомление.\n"
    "5 — максимальный: звук, более длительная вибрация и всплывающее уведомление.\n\n"
    "Это стандартное поведение ntfy на Android. На вашем телефоне оно зависит от ОС, "
    "настроек приложения и режима «Не беспокоить»."
)
ADMIN_INPUTS = {"source", "channel", "group", "words_add", "words_remove", "grant", "revoke"}
PICKER_ACTIONS = {"gpage": "groups", "gselect": "groups", "tpage": "topics", "tselect": "topics"}
PROMPTS = {
    "destination": "Пришлите название темы ntfy, например your-random-topic. Будет использован ntfy.sh. Можно также прислать полную ссылку на свой сервер.\nПри смене подключения старый токен будет сброшен.",
    "token": "Пришлите токен доступа ntfy. Он будет скрыт в интерфейсе; сообщение с токеном будет удалено.",
    "schedule": "Пришлите ночной период по Москве: 01:00-07:00. Можно пересекать полночь, например 23:00-08:00.",
    "words_add": "Пришлите слова или фразы через запятую, которые нужно добавить в общий список. Например: SHORT, LONG, шорт, лонг.",
    "words_remove": "Пришлите слова или фразы через запятую, которые нужно удалить из общего списка.",
    "source": "Пришлите ID канала/супергруппы и ID ветки через пробел.\nПример: -1001234567890 42\nДля канала или всех веток: -1001234567890 0\nАккаунт чтения уже должен состоять в источнике.",
    "channel": "Пришлите ссылку на канал (https://t.me/channel), @username или приглашение https://t.me/+… . После подтверждения аккаунт подпишется, если ещё не подписан. Для существующей подписки можно указать ID -100… .",
    "group": "Пришлите только ID супергруппы: -1001234567890. Затем выберите ветку кнопкой. Аккаунт чтения уже должен состоять в группе.",
    "grant": "Пришлите числовой Telegram ID пользователя, которому выдать доступ. Пользователь видит свой ID после /start.",
    "revoke": "Пришлите числовой Telegram ID пользователя, у которого отозвать доступ.",
}


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in rows
    ])


def toggle(value) -> str:
    return "✅" if value else "❌"


class SerialPerUser(BaseMiddleware):
    """Bounded locks prevent racing /start, text inputs and rapid button clicks."""
    def __init__(self):
        self.locks = [asyncio.Lock() for _ in range(128)]

    async def __call__(self, handler, event, data):
        async with self.locks[event.from_user.id % len(self.locks)]:
            return await handler(event, data)


class BotUI:
    def __init__(self, bot: Bot, db: Database, watcher: Watcher, sender: NtfySender, config: Config):
        self.bot, self.db, self.watcher, self.sender, self.config = bot, db, watcher, sender, config
        self.catalog = SourceCatalog(watcher.client)
        self.router = Router()
        self.router.message.filter(F.chat.type == "private")
        self.router.callback_query.filter(F.message.chat.type == "private")
        isolation = SerialPerUser()
        self.router.message.middleware(isolation)
        self.router.callback_query.middleware(isolation)
        self.router.message.register(self.start, CommandStart())
        self.router.message.register(self.input_message)
        self.router.callback_query.register(self.callback)

    def is_admin(self, uid: int) -> bool:
        return uid == self.config.admin_id

    async def delete(self, uid: int, message_id: int | None):
        if message_id:
            try:
                await self.bot.delete_message(uid, message_id)
            except TelegramAPIError as error:
                log.warning("Could not delete temporary message: %s", type(error).__name__)

    async def clear_input(self, uid: int):
        user = self.db.user(uid)
        await self.delete(uid, user["prompt_message_id"])
        self.db.update_user(uid, prompt_message_id=None, pending=None, draft=None)

    async def render(self, uid: int, text: str, rows):
        message_id = self.db.user(uid)["main_message_id"]
        if not message_id:
            return
        try:
            await self.bot.edit_message_text(text, chat_id=uid, message_id=message_id,
                                             reply_markup=keyboard(rows), parse_mode="HTML",
                                             disable_web_page_preview=True)
        except TelegramBadRequest as error:
            if "message is not modified" in error.message.lower():
                return
            # Never create a replacement main message implicitly.
            raise

    async def start(self, message: Message):
        uid = message.from_user.id
        user = self.db.ensure_user(uid)
        await self.clear_input(uid)
        if user["main_message_id"]:
            try:
                await self.bot.edit_message_reply_markup(chat_id=uid, message_id=user["main_message_id"], reply_markup=None)
            except TelegramAPIError:
                pass
        main = await self.bot.send_message(uid, "Открываю меню…")
        self.db.update_user(uid, main_message_id=main.message_id)
        await self.delete(uid, message.message_id)
        await self.home(uid)

    async def home(self, uid: int, notice: str = ""):
        user = self.db.user(uid)
        if not user["granted"]:
            await self.render(uid, f"<b>Доступ закрыт</b>\nВаш Telegram ID: <code>{uid}</code>\n"
                              "Передайте ID администратору. После выдачи доступа нажмите «Проверить доступ».",
                              [[("Проверить доступ", "home")]])
            return
        destination = json.loads(user["destination"])
        text = ("<b>НДА Будилка</b>\n\n"
                f"{toggle(user['enabled'])} Уведомления\n"
                f"{toggle(bool(destination))} ntfy подключён\n"
                f"{toggle(user['night_enabled'])} Ночной режим\n"
                f"Источников: {len(self.db.sources())} · Слов: {len(self.db.words())}\n"
                f"Текущий приоритет: {priority_for(user)} из 5\n\n"
                "Все пользователи получают сигналы из общего списка источников.")
        if notice:
            text += "\n\n" + escape(notice)
        rows = [[(f"{toggle(user['enabled'])} Уведомления", "toggle:enabled")],
                [("Подключение ntfy", "ntfy"), ("Ночной режим", "night")],
                [("Источники", "sources:0"), ("Кодовые слова", "words")],
                [("Состояние", "status")]]
        if destination:
            rows.append([("Тест уведомления", "test")])
        if self.is_admin(uid):
            rows.append([("Администрирование", "admin")])
        await self.render(uid, text, rows)

    async def test_menu(self, uid: int, notice: str = ""):
        if not json.loads(self.db.user(uid)["destination"]):
            raise ValueError("Сначала настройте подключение ntfy.")
        text = ("<b>Тест уведомления — выберите приоритет</b>\n\n" + PRIORITY_HELP
                + "\n\nНажатие кнопки отправит один тест с выбранным приоритетом, "
                "даже при включённом ночном режиме или паузе уведомлений. Ваши настройки не изменятся.")
        if notice:
            text += "\n\n" + escape(notice)
        await self.render(uid, text, [[(label, f"test:{n}")] for n, label in PRIORITIES.items()]
                          + [[("‹ Подключение ntfy", "ntfy"), ("Главное меню", "home")]])

    async def ntfy(self, uid: int, notice: str = ""):
        destination = json.loads(self.db.user(uid)["destination"])
        link = destination.get("server", "") + "/" + destination.get("topic", "") if destination else "не настроено"
        text = (f"<b>Подключение ntfy</b>\nТема: <code>{escape(link)}</code>\n"
                f"Токен: {'задан (скрыт)' if destination.get('token') else 'не задан'}\n\n"
                "Подпишитесь на эту же тему в приложении ntfy. Для открытого сервера токен необязателен. "
                "Используйте непредсказуемое имя темы или тему с ограничением доступа.")
        if notice:
            text += "\n\n" + escape(notice)
        rows = [[("Указать тему / ссылку", "input:destination")]]
        if destination:
            rows.extend([[("Указать токен", "input:token"), ("Убрать токен", "clear_token")],
                         [("Тест уведомления", "test")], [("Отключить ntfy", "disconnect")]])
        rows.append([("‹ Главное меню", "home")])
        await self.render(uid, text, rows)

    async def night(self, uid: int):
        user = self.db.user(uid)
        await self.render(uid, "<b>Ночной режим</b>\n\n"
                          f"{toggle(user['night_enabled'])} {'Включён' if user['night_enabled'] else 'Выключен'}\n"
                          f"Период: {format_time(user['night_start'])}–{format_time(user['night_end'])} МСК\n"
                          f"Приоритет ночью: {PRIORITIES[user['night_priority']]}\n\n"
                          "Вне периода — 5 (максимальный). Приоритеты 1–2 предназначены для тихих уведомлений. "
                          "Настройки звука зависят также от телефона.",
                          [[(f"{toggle(user['night_enabled'])} Ночной режим", "toggle:night_enabled")],
                           [("Изменить время", "input:schedule")],
                           [("Приоритет ночью", "priorities")], [("‹ Главное меню", "home")]])

    async def sources(self, uid: int, page: int):
        sources = self.db.sources()
        page = max(0, min(page, max(0, (len(sources) - 1) // 6)))
        visible = sources[page * 6:(page + 1) * 6]
        text = "<b>Общие источники</b>\n\n"
        rows = []
        for source in visible:
            text += (f"{escape(source['title'])}\n<code>{source['chat_id']}</code> · "
                     f"ветка: {source['topic_id'] or 'все'} · "
                     f"{'все сообщения' if source['trigger_mode'] == 'all' else 'кодовые слова'}\n\n")
            if self.is_admin(uid):
                rows.append([(f"⚙️ {source['title'][:40]}", f"source_settings:{source['id']}")])
        if not sources:
            text += "Источников пока нет. Администратор может добавить канал или ветку по ID.\n"
        nav = []
        if page:
            nav.append(("‹", f"sources:{page - 1}"))
        if (page + 1) * 6 < len(sources):
            nav.append(("›", f"sources:{page + 1}"))
        if nav:
            rows.append(nav)
        if self.is_admin(uid):
            rows.append([("Добавить канал", "input:channel")])
            rows.append([("Добавить супергруппу", "groups")])
        rows.append([("‹ Главное меню", "home")])
        await self.render(uid, text, rows)

    async def source_settings(self, uid: int, source_id: int, notice: str = ""):
        source = self.db.source(source_id)
        if not source:
            raise ValueError("Источник уже удалён.")
        mode = source["trigger_mode"]
        text = ("<b>Настройки источника</b>\n\n"
                f"{escape(source['title'])}\n"
                f"ID: <code>{source['chat_id']}</code>\n"
                f"Ветка: {source['topic_id'] or 'все'}\n\n"
                "Какие сообщения создают уведомление:\n"
                f"{'✅' if mode == 'keywords' else '❌'} По кодовым словам\n"
                f"{'✅' if mode == 'all' else '❌'} Все новые сообщения")
        if notice:
            text += "\n\n" + escape(notice)
        rows = [[(f"{'✅' if mode == 'keywords' else '❌'} Кодовые слова",
                  f"source_mode:{source_id}:keywords")],
                [(f"{'✅' if mode == 'all' else '❌'} Все сообщения",
                  f"source_mode:{source_id}:all")],
                [("Удалить источник", f"remove:{source_id}")],
                [("‹ Источники", "sources:0")]]
        await self.render(uid, text, rows)

    async def show_groups(self, uid: int):
        groups = await self.catalog.groups()
        state = dict(kind="groups", nonce=secrets.token_hex(6), items=groups)
        await self.picker(uid, state, 0)

    async def show_topics(self, uid: int, chat_id: int):
        group = await self.catalog.group(chat_id)
        topics = await self.catalog.topics(chat_id)
        state = dict(kind="topics", nonce=secrets.token_hex(6), group=group, items=topics)
        await self.picker(uid, state, 0)

    async def picker(self, uid: int, state: dict, page: int):
        items = state["items"]
        page = max(0, min(page, max(0, (len(items) - 1) // 8)))
        is_groups = state["kind"] == "groups"
        prefix = "g" if is_groups else "t"
        nonce = state["nonce"]
        text = ("<b>Выберите супергруппу</b>\nПоказаны группы аккаунта чтения." if is_groups else
                "<b>Выберите ветку</b>\n" + escape(state["group"]["title"]))
        rows = []
        for index in range(page * 8, min(len(items), (page + 1) * 8)):
            item = items[index]
            suffix = f" · {item['chat_id']}" if is_groups else f" · {item['id']}"
            rows.append([(item["title"][:45] + suffix, f"{prefix}select:{nonce}:{index}")])
        if not items:
            text += "\nСупергруппы не найдены." if is_groups else "\nДоступных веток нет или в группе отключены ветки."
        nav = []
        if page:
            nav.append(("‹", f"{prefix}page:{nonce}:{page - 1}"))
        if (page + 1) * 8 < len(items):
            nav.append(("›", f"{prefix}page:{nonce}:{page + 1}"))
        if nav:
            rows.append(nav)
        if is_groups:
            rows.append([("Указать ID группы", "input:group")])
        else:
            rows.append([("Вся группа / все ветки", f"tselect:{nonce}:-1")])
            rows.append([("‹ К выбору группы", "groups")])
        rows.append([("‹ Источники", "sources:0")])
        self.db.update_user(uid, draft=json.dumps(state, ensure_ascii=False))
        await self.render(uid, text, rows)

    async def words(self, uid: int):
        rows = [[("Добавить слова", "input:words_add"), ("Удалить слова", "input:words_remove")]] if self.is_admin(uid) else []
        rows.append([("‹ Главное меню", "home")])
        words = self.db.words()
        await self.render(uid, "<b>Общие кодовые слова</b>\n\n" + escape(", ".join(words) if words else "список пуст")
                          + "\n\nПоиск целых слов/фраз без учёта регистра, в тексте и подписях к медиа. "
                          "Правки сообщений не проверяются. Одно уведомление на сообщение.", rows)

    async def admin(self, uid: int, page: int = 0):
        users = self.db.users()
        page = max(0, min(page, max(0, (len(users) - 1) // 12)))
        text = "<b>Администрирование</b>\n\nПользователи с доступом:\n" + "\n".join(
            f"<code>{u['id']}</code>" + (" — администратор" if self.is_admin(u["id"]) else "")
            for u in users[page * 12:(page + 1) * 12])
        rows = [[("Выдать доступ", "input:grant"), ("Отозвать доступ", "input:revoke")],
                [("Источники", "sources:0"), ("Кодовые слова", "words")]]
        nav = []
        if page:
            nav.append(("‹", f"admin:{page - 1}"))
        if (page + 1) * 12 < len(users):
            nav.append(("›", f"admin:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("‹ Главное меню", "home")])
        await self.render(uid, text, rows)

    async def status(self, uid: int):
        counts = self.db.counts(uid)
        user = self.db.user(uid)
        await self.render(uid, "<b>Состояние</b>\n\n"
                          f"Аккаунт чтения: {'подключён' if self.watcher.client.is_connected() else 'нет соединения'}\n"
                          f"В очереди: {counts.get('pending', 0)}\nДоставлено: {counts.get('sent', 0)}\n"
                          f"Не доставлено: {counts.get('failed', 0)}\n"
                          f"Последняя ошибка доставки: {escape(user['last_error'] or 'нет')}\n"
                          f"Чтение: {escape(self.watcher.last_error or 'ошибок нет')}",
                          [[("Обновить", "status")], [("‹ Главное меню", "home")]])

    async def prompt(self, uid: int, kind: str):
        if kind not in PROMPTS or (kind in ADMIN_INPUTS and not self.is_admin(uid)):
            raise ValueError("Это действие доступно только администратору.")
        if kind == "token" and not json.loads(self.db.user(uid)["destination"]):
            raise ValueError("Сначала укажите тему или ссылку ntfy.")
        await self.render(uid, "<b>Ожидаю ввод</b>\n\n" + escape(PROMPTS[kind]), [[("Отмена", "home")]])
        prompt = await self.bot.send_message(uid, PROMPTS[kind])
        self.db.update_user(uid, pending=kind, prompt_message_id=prompt.message_id)

    async def preview(self, uid: int, kind: str, value, description: str):
        nonce = secrets.token_hex(6)
        self.db.update_user(uid, draft=json.dumps({"kind": kind, "value": value, "nonce": nonce}, ensure_ascii=False))
        await self.render(uid, "<b>Подтвердите изменение</b>\n\n" + escape(description),
                          [[("Сохранить", f"save:{nonce}"), ("Отмена", "home")]])

    async def input_message(self, message: Message):
        uid = message.from_user.id
        user = self.db.user(uid)
        await self.delete(uid, message.message_id)
        if not user or not user["main_message_id"]:
            return  # /start is the only operation that creates a main message.
        kind = user["pending"]
        await self.clear_input(uid)
        if not self.db.user(uid)["granted"]:
            await self.home(uid)
            return
        if not kind:
            await self.home(uid, "Выберите действие кнопкой в меню.")
            return
        try:
            if kind in ADMIN_INPUTS and not self.is_admin(uid):
                raise ValueError("Это действие доступно только администратору.")
            if not message.text:
                raise ValueError("Нужен текстовый ответ. Нажмите «Повторить ввод».")
            if kind == "group":
                chat_id, topic_id = parse_source(message.text)
                if topic_id or len(message.text.split()) != 1:
                    raise ValueError("Сначала укажите только ID группы. Ветка выбирается на следующем шаге.")
                await self.show_topics(uid, chat_id)
                return
            value, description = await self.parse_input(uid, kind, message.text)
            await self.preview(uid, kind, value, description)
        except ValueError as error:
            await self.render(uid, "<b>Не удалось сохранить</b>\n\n" + escape(str(error)),
                              [[("Повторить ввод", f"input:{kind}")], [("Отмена", "home")]])

    async def parse_input(self, uid: int, kind: str, text: str):
        if kind == "destination":
            value = parse_destination(text, self.config.allow_http, self.config.allow_private_hosts)
            return value, "Подключить ntfy: " + value["server"] + "/" + value["topic"] + "\nСтарый токен будет сброшен."
        if kind == "token":
            return validate_token(text), "Сохранить новый токен ntfy? Значение скрыто."
        if kind == "schedule":
            value = parse_schedule(text)
            return value, f"Ночной период: {format_time(value[0])}–{format_time(value[1])} МСК."
        if kind in {"words_add", "words_remove"}:
            value = parse_words(text)
            if kind == "words_add":
                existing = {word.casefold() for word in self.db.words()}
                changed = [word for word in value if word.casefold() not in existing]
                if not changed:
                    raise ValueError("Все указанные слова уже есть в списке.")
                return value, "Добавить в общий список:\n" + ", ".join(changed)
            existing = {word.casefold() for word in self.db.words()}
            changed = [word for word in value if word.casefold() in existing]
            if not changed:
                raise ValueError("Указанных слов нет в общем списке.")
            return value, "Удалить из общего списка:\n" + ", ".join(changed)
        if kind == "channel":
            value = await self.catalog.prepare_channel(text)
            action = "Аккаунт подпишется на канал после сохранения." if value["join_required"] else "Аккаунт уже подписан."
            return value, f"Добавить канал для всех пользователей:\n{value['title']}\n\n{action}"
        if kind == "source":
            chat_id, topic_id = parse_source(text)
            value = await self.watcher.validate_source(chat_id, topic_id)
            return value, f"Добавить для всех пользователей:\n{value['title']}\nID: {chat_id}\nВетка: {topic_id or 'все'}"
        if kind in {"grant", "revoke"}:
            if not text.strip().isdecimal() or not 0 < int(text.strip()) < 2 ** 63:
                raise ValueError("Введите положительный числовой Telegram ID.")
            value = int(text.strip())
            if kind == "revoke" and value == self.config.admin_id:
                raise ValueError("Нельзя отозвать доступ администратора.")
            return value, f"{'Выдать' if kind == 'grant' else 'Отозвать'} доступ пользователя {value}?"
        raise ValueError("Неизвестное действие. Откройте главное меню.")

    async def save(self, uid: int, draft: dict):
        kind, value = draft["kind"], draft["value"]
        if kind in ADMIN_INPUTS | {"remove"} and not self.is_admin(uid):
            raise ValueError("Это действие доступно только администратору.")
        if kind == "destination":
            self.db.cancel_user_deliveries(uid)
            self.db.update_user(uid, destination=json.dumps(value), last_error="")
        elif kind in {"token", "clear_token"}:
            destination = json.loads(self.db.user(uid)["destination"])
            if not destination:
                raise ValueError("Сначала укажите тему или ссылку ntfy.")
            destination["token"] = value if kind == "token" else ""
            self.db.update_user(uid, destination=json.dumps(destination), last_error="")
        elif kind == "disconnect":
            self.db.cancel_user_deliveries(uid)
            self.db.update_user(uid, destination="{}", last_error="")
        elif kind == "schedule":
            self.db.update_user(uid, night_start=value[0], night_end=value[1])
        elif kind == "words_add":
            self.db.add_words(value)
        elif kind == "words_remove":
            self.db.remove_words(value)
        elif kind == "source":
            self.db.add_source(**value)
        elif kind == "channel":
            try:
                source = await self.catalog.confirm_channel(value)
            except Exception as error:
                if isinstance(error, ValueError):
                    log.warning("Канал не сохранён: источник=%r; %s", value["title"], error)
                    reason = str(error)
                else:
                    log.error("Ошибка подтверждения канала", exc_info=True)
                    reason = "Не удалось завершить добавление. Подписка могла выполниться; повторное сохранение проверит её."
                await self.preview(uid, "channel", value, value["title"] + "\n\n" + reason + "\n\nМожно повторить сохранение этой же кнопкой.")
                return
            self.db.add_source(**source)
            log.info("Источник сохранён: источник=%r chat_id=%s администратор=%s", source["title"], source["chat_id"], uid)
        elif kind == "remove":
            self.db.remove_source(value)
        elif kind in {"grant", "revoke"}:
            if kind == "revoke" and self.db.user(value):
                await self.clear_input(value)
            self.db.set_access(value, kind == "grant")
        else:
            raise ValueError("Неизвестное изменение.")
        if kind in {"destination", "token", "clear_token", "disconnect"}:
            await self.ntfy(uid, "Настройки сохранены.")
        elif kind == "schedule":
            await self.night(uid)
        elif kind in {"words_add", "words_remove"}:
            await self.words(uid)
        elif kind in {"source", "channel", "remove"}:
            await self.sources(uid, 0)
        else:
            await self.admin(uid)

    async def callback(self, query: CallbackQuery):
        uid = query.from_user.id
        user = self.db.user(uid)
        if not user or query.message.message_id != user["main_message_id"]:
            await query.answer("Это старое меню. Используйте актуальное или отправьте /start.", show_alert=True)
            return
        action, _, arg = (query.data or "").partition(":")
        if not user["granted"] and action != "home":
            await query.answer("Доступ закрыт.", show_alert=True)
            return
        admin_action = action in {"admin", "remove", "groups", "source_settings", "source_mode",
                                  *PICKER_ACTIONS} or (action == "input" and arg in ADMIN_INPUTS)
        if admin_action and not self.is_admin(uid):
            await query.answer("Доступно только администратору.", show_alert=True)
            return
        draft = json.loads(user["draft"]) if user["draft"] else None
        if action in PICKER_ACTIONS:
            nonce, _, number = arg.partition(":")
            if (not draft or draft["kind"] != PICKER_ACTIONS[action] or draft["nonce"] != nonce
                    or not number.lstrip("-").isdecimal()):
                await query.answer("Этот список устарел. Откройте добавление супергруппы заново.", show_alert=True)
                return
        if action == "save" and (not draft or draft["nonce"] != arg or draft["kind"] in {"groups", "topics"}):
            await query.answer("Изменение уже обработано или отменено.", show_alert=True)
            return
        await query.answer()
        try:
            await self.clear_input(uid)
            user = self.db.user(uid)
            if not user["granted"]:
                await self.home(uid)
                return
            if action == "home":
                await self.home(uid)
            elif action == "ntfy":
                await self.ntfy(uid)
            elif action == "night":
                await self.night(uid)
            elif action == "sources":
                await self.sources(uid, int(arg))
            elif action == "source_settings":
                if not arg.isdecimal():
                    raise ValueError("Некорректный источник.")
                await self.source_settings(uid, int(arg))
            elif action == "source_mode":
                source_id, separator, mode = arg.partition(":")
                if not separator or not source_id.isdecimal() or mode not in {"keywords", "all"}:
                    raise ValueError("Некорректный режим источника.")
                self.db.set_source_mode(int(source_id), mode)
                source = self.db.source(int(source_id))
                log.info("Режим источника изменён: источник=%r режим=%s администратор=%s",
                         source["title"], mode, uid)
                await self.source_settings(uid, int(source_id), "Режим сохранён.")
            elif action == "groups":
                await self.show_groups(uid)
            elif action in PICKER_ACTIONS:
                index = int(arg.partition(":")[2])
                if action in {"gpage", "tpage"}:
                    await self.picker(uid, draft, index)
                elif action == "gselect":
                    if not 0 <= index < len(draft["items"]):
                        raise ValueError("Выберите группу из списка.")
                    await self.show_topics(uid, draft["items"][index]["chat_id"])
                else:
                    if not -1 <= index < len(draft["items"]):
                        raise ValueError("Выберите ветку из списка.")
                    topic_id = 0 if index == -1 else draft["items"][index]["id"]
                    source = await self.watcher.validate_source(draft["group"]["chat_id"], topic_id)
                    await self.preview(uid, "source", source, "Добавить для всех пользователей:\n" + source["title"])
            elif action == "words":
                await self.words(uid)
            elif action == "admin":
                await self.admin(uid, int(arg or "0"))
            elif action == "status":
                await self.status(uid)
            elif action == "input":
                await self.prompt(uid, arg)
            elif action == "save":
                await self.save(uid, draft)
            elif action == "toggle" and arg in {"enabled", "night_enabled"}:
                new_value = int(not user[arg])
                self.db.update_user(uid, **{arg: new_value})
                if arg == "enabled":
                    if not new_value:
                        self.db.cancel_user_deliveries(uid)
                    await self.home(uid)
                else:
                    await self.night(uid)
            elif action == "priorities":
                await self.render(uid, "<b>Выберите приоритет ночью</b>\n\n" + PRIORITY_HELP,
                                  [[(f"{toggle(user['night_priority'] == n)} {label}", f"priority:{n}")]
                                   for n, label in PRIORITIES.items()] + [[("‹ Назад", "night")]])
            elif action == "priority" and arg in {"1", "2", "3", "4", "5"}:
                self.db.update_user(uid, night_priority=int(arg))
                await self.night(uid)
            elif action == "remove":
                if not arg.isdecimal():
                    raise ValueError("Некорректный источник.")
                source = self.db.source(int(arg))
                if not source:
                    raise ValueError("Источник уже удалён.")
                await self.preview(uid, "remove", source["id"], f"Удалить источник для всех пользователей?\n{source['title']}")
            elif action in {"clear_token", "disconnect"}:
                await self.preview(uid, action, None, "Убрать токен ntfy?" if action == "clear_token" else "Отключить ntfy и отменить ожидающие уведомления?")
            elif action == "test":
                destination = json.loads(user["destination"])
                if not destination:
                    raise ValueError("Сначала настройте подключение ntfy.")
                if not arg:
                    await self.test_menu(uid)
                    return
                if arg not in {"1", "2", "3", "4", "5"}:
                    raise ValueError("Выберите приоритет теста от 1 до 5.")
                priority = int(arg)
                log.info("Отправка тестового уведомления: получатель=%s приоритет=%s", uid, priority)
                try:
                    await self.sender.send(destination, Notification("НДА Будилка — тест", f"Проверочное уведомление. Приоритет: {PRIORITIES[priority]}.", priority))
                except DeliveryError as error:
                    log.warning("Тестовое уведомление не доставлено: получатель=%s; %s", uid, error)
                    self.db.update_user(uid, last_error=str(error))
                    await self.test_menu(uid, str(error))
                else:
                    log.info("Тестовое уведомление доставлено: получатель=%s приоритет=%s", uid, priority)
                    self.db.update_user(uid, last_error="")
                    await self.test_menu(uid, f"Тест отправлен: {PRIORITIES[priority]}.")
            else:
                await self.home(uid)
        except ValueError as error:
            await self.home(uid, str(error))
        except TelegramAPIError as error:
            log.warning("Menu update failed: %s; user should send /start", type(error).__name__)
