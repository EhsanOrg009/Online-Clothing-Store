"""Provider درون‌حافظه‌ای برای تست‌ها (هیچ پیامک واقعی ارسال نمی‌شود)."""

from .base import SMSProvider


class InMemorySMSProvider(SMSProvider):
    """پیامک‌ها را فقط در حافظه نگه می‌دارد تا تست‌ها بتوانند آن را بخوانند."""

    name = "memory"

    def __init__(self):
        self.outbox: dict[str, list[str]] = {}

    def send(self, phone_number: str, message: str) -> None:
        self.outbox.setdefault(phone_number, []).append(message)

    @property
    def last_message(self) -> str | None:
        if not self.outbox:
            return None
        messages = list(self.outbox.values())[-1]
        return messages[-1] if messages else None

    def clear(self) -> None:
        self.outbox.clear()
