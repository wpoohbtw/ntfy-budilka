import argparse
import asyncio
from contextlib import contextmanager
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

from aiogram import Bot, Dispatcher
from telethon import TelegramClient
from telethon.sessions import StringSession

from .auth import ensure_authorized
from .config import Config, ROOT
from .delivery import DeliveryWorker
from .notifications.ntfy import NtfySender
from .storage import Database
from .ui import BotUI
from .watcher import Watcher

log = logging.getLogger("ndabudilka")


class SafeLibraryLogs(logging.Filter):
    def filter(self, record):
        if getattr(record, "_ndabudilka_sanitized", False):
            return True
        library = record.name.startswith(("telethon", "aiogram", "aiohttp"))
        if library or record.exc_info:
            # Keep code locations, never exception values, source lines or local variables.
            error_type = type(record.exc_info[1]).__name__ if record.exc_info else "see connection status"
            locations = []
            tb = record.exc_info[2] if record.exc_info else None
            while tb:
                code = tb.tb_frame.f_code
                locations.append(f"{Path(code.co_filename).name}:{tb.tb_lineno} ({code.co_name})")
                tb = tb.tb_next
            location = " at " + " -> ".join(locations[-10:]) if locations else ""
            message = "Library event" if library else record.getMessage()
            record.msg = "%s: %s%s"
            record.args = (message, error_type, location)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        record._ndabudilka_sanitized = True
        return True


def configure_logging(directory: Path = ROOT / "logs"):
    directory.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), RotatingFileHandler(
        directory / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")]
    for handler in handlers:
        handler.addFilter(SafeLibraryLogs())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers, force=True)
    for name in ("telethon", "aiogram", "aiohttp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    log.info("Журнал включён: logs/bot.log; до 5 МиБ на файл, 3 архивных файла.")


@contextmanager
def instance_lock(path: Path):
    """OS lock is released even after a crash; never run two delivery workers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        file.seek(0, 2)
        if file.tell() == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Приложение уже запущено для этой базы данных.") from None
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def make_client(config: Config) -> TelegramClient:
    if config.string_session:
        try:
            session = StringSession(config.string_session)
        except Exception:
            raise ValueError("Некорректная TELETHON_STRING_SESSION.") from None
    else:
        path = config.session_path
        if path.suffix != ".session":
            path = Path(str(path) + ".session")
        path.parent.mkdir(parents=True, exist_ok=True)
        session = str(path)
    return TelegramClient(session, config.api_id, config.api_hash, catch_up=False,
                          flood_sleep_threshold=0, request_retries=2)


async def run(config: Config):
    client = make_client(config)
    db = Database(config.database_path, config.admin_id)
    bot = Bot(config.bot_token)
    sender = NtfySender(config.allow_private_hosts, config.allow_http)
    tasks = []
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        await ensure_authorized(client, string_session=bool(config.string_session))
        me = await client.get_me()
        if me.bot:
            raise ValueError("Нужна сессия пользовательского аккаунта Telethon, не бота.")
        worker = DeliveryWorker(db, sender)
        watcher = Watcher(client, db, worker)
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise ValueError("У бота настроен webhook. Отключите его перед запуском локального polling.")
        dispatcher = Dispatcher()
        dispatcher.include_router(BotUI(bot, db, watcher, sender, config).router)

        async def disconnected():
            await client.disconnected
            raise RuntimeError("Watcher disconnected permanently")

        tasks = [asyncio.create_task(worker.run(), name="delivery"),
                 asyncio.create_task(disconnected(), name="watcher"),
                 asyncio.create_task(dispatcher.start_polling(bot, close_bot_session=False,
                                                            allowed_updates=["message", "callback_query"]), name="bot")]
        log.info("Бот и аккаунт чтения запущены. Остановить: Ctrl+C.")
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.disconnect()
        await sender.close()
        await bot.session.close()
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram / ntfy: уведомления по кодовым словам")
    parser.add_argument("--check", action="store_true", help="Проверить .env и наличие сессии без соединения с Telegram")
    args = parser.parse_args()
    configure_logging()
    try:
        config = Config.load()
        if args.check:
            if not config.string_session:
                path = config.session_path
                if path.suffix != ".session":
                    path = Path(str(path) + ".session")
                if not path.is_file():
                    print("Файла сессии ещё нет. При обычном запуске start.bat будет предложена авторизация.")
            else:
                try:
                    StringSession(config.string_session)
                except Exception:
                    raise ValueError("Некорректная TELETHON_STRING_SESSION.") from None
            print("Конфигурация корректна. Авторизация и сеть не проверялись.")
            return 0
        with instance_lock(config.database_path.with_suffix(".lock")):
            asyncio.run(run(config))
    except ValueError as error:
        log.error("%s", error)
        return 1
    except KeyboardInterrupt:
        log.info("Приложение остановлено.")
    except Exception as error:
        log.error("Приложение остановлено: %s. Проверьте сеть, токены и сессию.", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
