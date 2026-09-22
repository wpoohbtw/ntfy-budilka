import asyncio
import ipaddress
import json
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from . import DeliveryError, Notification
from ..rules import clip_utf8


def check_host(host: str, allow_private: bool):
    if allow_private:
        return
    if host.lower() == "localhost" or host.lower().endswith((".localhost", ".local")):
        raise ValueError("Локальные адреса ntfy отключены в конфигурации приложения.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("Частные адреса ntfy отключены в конфигурации приложения.")


def parse_destination(value: str, allow_http: bool = False, allow_private: bool = False) -> dict:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        value = "https://ntfy.sh/" + value
    if len(value) > 1024:
        raise ValueError("Ссылка ntfy должна быть не длиннее 1024 символов.")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("Некорректная ссылка ntfy.") from None
    schemes = {"https", "http"} if allow_http else {"https"}
    if (url.scheme not in schemes or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment
            or any(char.isspace() for char in value) or "\\" in value):
        raise ValueError("Нужна HTTPS-ссылка на тему ntfy без логина, пароля и параметров.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Некорректный порт ntfy.")
    check_host(url.hostname, allow_private)
    path, _, topic = url.path.rstrip("/").rpartition("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", topic):
        raise ValueError("В конце ссылки укажите тему ntfy: буквы A–Z, цифры, _ или -.")
    if topic in {"docs", "static", "file", "app", "account", "settings", "v1", "config.js"}:
        raise ValueError("Это служебный путь ntfy. Укажите имя своей темы.")
    server = urlunsplit((url.scheme, url.netloc, path, "", "")).rstrip("/")
    return {"server": server, "topic": topic, "token": ""}


def validate_token(value: str) -> str:
    token = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,512}", token):
        raise ValueError("Некорректный токен доступа ntfy: без пробелов, до 512 символов.")
    return token


class PublicResolver(aiohttp.resolver.DefaultResolver):
    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        results = await super().resolve(host, port, family)
        for result in results:
            try:
                check_host(result["host"], False)
            except ValueError:
                raise OSError("Private ntfy destination is disabled") from None
        return results


class NtfySender:
    def __init__(self, allow_private: bool = False, allow_http: bool = False):
        self.allow_private = allow_private
        self.allow_http = allow_http
        resolver = aiohttp.resolver.DefaultResolver() if allow_private else PublicResolver()
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=resolver),
            timeout=aiohttp.ClientTimeout(total=15),
            trust_env=False,
        )

    async def close(self):
        await self.session.close()

    async def send(self, destination: dict, notification: Notification) -> None:
        try:
            checked = parse_destination(
                destination["server"] + "/" + destination["topic"],
                self.allow_http, self.allow_private,
            )
        except (KeyError, ValueError):
            raise DeliveryError("Проверьте ссылку подключения ntfy.") from None
        if not 1 <= notification.priority <= 5:
            raise DeliveryError("Некорректный приоритет уведомления.")
        headers = {}
        token = destination.get("token")
        if token:
            try:
                headers["Authorization"] = "Bearer " + validate_token(token)
            except ValueError:
                raise DeliveryError("Проверьте сохранённый токен ntfy.") from None
        payload = {
            "topic": checked["topic"],
            "title": clip_utf8(notification.title, 200),
            "message": clip_utf8(notification.body, 3000),
            "priority": notification.priority,
        }
        if notification.link:
            payload["click"] = notification.link
        try:
            # JSON publishing goes to the server root; redirects must not receive credentials.
            async with self.session.post(checked["server"] + "/", json=payload,
                                         headers=headers, allow_redirects=False) as response:
                if response.status in (401, 403):
                    raise DeliveryError("ntfy отклонил доступ. Проверьте токен и права на тему.")
                if response.status == 429 or response.status >= 500:
                    delay = response.headers.get("Retry-After", "0")
                    retry_after = min(float(delay), 3600) if delay.isdecimal() else 0
                    raise DeliveryError(f"ntfy временно недоступен (HTTP {response.status}).", True, retry_after)
                if not 200 <= response.status < 300:
                    raise DeliveryError(f"ntfy вернул HTTP {response.status}. Проверьте подключение.")
                raw = await response.content.read(16385)
                if len(raw) > 16384:
                    raise DeliveryError("Сервер вернул неожиданный ответ вместо подтверждения ntfy.")
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise DeliveryError("Сервер не подтвердил публикацию в формате ntfy.") from None
                if not isinstance(result, dict) or result.get("event") != "message" or not result.get("id"):
                    raise DeliveryError("Сервер не подтвердил публикацию уведомления.")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise DeliveryError("Нет соединения с ntfy. Проверьте адрес и доступность сервера.", True) from None
