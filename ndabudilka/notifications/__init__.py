"""Transport contract. Telegram and delivery policy do not depend on ntfy."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Notification:
    title: str
    body: str
    priority: int  # Internal urgency scale: 1 (quietest) ... 5 (most urgent).
    link: str = ""


class DeliveryError(Exception):
    """Safe user-facing error; never include credentials or HTTP response bodies."""

    def __init__(self, message: str, retryable: bool = False, retry_after: float = 0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class NotificationSender(Protocol):
    async def send(self, destination: dict, notification: Notification) -> None: ...
