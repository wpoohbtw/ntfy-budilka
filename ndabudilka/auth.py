"""Interactive account sign-in during startup, before the bot begins polling."""

import asyncio
import getpass
import re
import sys
import warnings

from telethon import TelegramClient
from telethon.errors import (FloodWaitError, PasswordHashInvalidError, PhoneCodeEmptyError,
                             PhoneCodeExpiredError, PhoneCodeInvalidError, PhoneNumberBannedError,
                             PhoneNumberInvalidError, PhoneNumberUnoccupiedError, RPCError,
                             SessionPasswordNeededError)


def read_secret(prompt: str) -> str:
    # Do not allow getpass to fall back to echoing passwords in a redirected console.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ValueError("Недоступен скрытый ввод. Запустите start.bat в обычном окне терминала.") from None


async def sign_in_password(client: TelegramClient):
    for _ in range(3):
        password = read_secret("Пароль 2FA (двухэтапной аутентификации; ввод скрыт): ")
        if not password:
            raise ValueError("Авторизация отменена: пароль не введён.")
        try:
            await asyncio.wait_for(client.sign_in(password=password), timeout=60)
            return
        except PasswordHashInvalidError:
            print("Неверный пароль 2FA. Попробуйте ещё раз.")
        finally:
            password = None
    raise ValueError("Не удалось войти: три неверных пароля 2FA. Запустите приложение заново.")


async def interactive_sign_in(client: TelegramClient):
    print("Нужна авторизация аккаунта чтения Telegram. Пустой ответ или Ctrl+C — отмена.")
    for _ in range(3):
        phone = input("Номер телефона с кодом страны (например, +79991234567): ").strip()
        if not phone:
            raise ValueError("Авторизация отменена: номер не введён.")
        phone = re.sub(r"[\s()-]", "", phone)
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
            print("Введите номер в международном формате, начиная с +.")
            continue
        try:
            sent = await asyncio.wait_for(client.send_code_request(phone), timeout=30)
            break
        except PhoneNumberInvalidError:
            print("Telegram не принял номер. Проверьте его и повторите ввод.")
    else:
        raise ValueError("Не удалось отправить код: три неверных номера.")

    print("Код отправлен способом, выбранным Telegram. Проверьте приложение Telegram или SMS.")
    for _ in range(3):
        code = read_secret("Код входа Telegram (ввод скрыт): ").replace(" ", "")
        if not code:
            raise ValueError("Авторизация отменена: код не введён.")
        if not re.fullmatch(r"[0-9]{4,10}", code):
            print("Код должен содержать только цифры.")
            continue
        try:
            await asyncio.wait_for(client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash), timeout=30)
            return
        except SessionPasswordNeededError:
            await sign_in_password(client)
            return
        except (PhoneCodeInvalidError, PhoneCodeEmptyError):
            print("Неверный код. Попробуйте ещё раз.")
        except PhoneCodeExpiredError:
            raise ValueError("Код истёк. Запустите приложение заново, чтобы получить новый код.") from None
        finally:
            code = None
    raise ValueError("Не удалось войти: три неверных кода. Запустите приложение заново.")


async def ensure_authorized(client: TelegramClient, *, string_session: bool = False):
    if await asyncio.wait_for(client.is_user_authorized(), timeout=15):
        return
    if string_session:
        raise ValueError("TELETHON_STRING_SESSION не авторизована. Очистите её в .env для входа с сохранением в TELETHON_SESSION.")
    if sys.stdin is None or not sys.stdin.isatty():
        raise ValueError("Для первой авторизации откройте start.bat в интерактивном терминале.")
    try:
        await interactive_sign_in(client)
    except EOFError:
        raise ValueError("Ввод прерван. Повторите запуск start.bat в интерактивном терминале.") from None
    except FloodWaitError as error:
        raise ValueError(f"Telegram ограничил вход. Повторите через {error.seconds} сек.") from None
    except PhoneNumberBannedError:
        raise ValueError("Telegram заблокировал вход для этого номера.") from None
    except PhoneNumberUnoccupiedError:
        raise ValueError("Аккаунт с этим номером не найден. Сначала зарегистрируйтесь в Telegram.") from None
    except RPCError as error:
        raise ValueError(f"Telegram не завершил авторизацию ({type(error).__name__}). Повторите запуск позже.") from None
    except (OSError, asyncio.TimeoutError):
        raise ValueError("Соединение прервано во время авторизации. Проверьте сеть и повторите запуск.") from None
    print("Авторизация завершена. Сессия сохранена; повторный ввод при следующем запуске не потребуется.")
