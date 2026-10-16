"""Provider پیامک برای محیط Development (چاپ در Console)."""

import logging

from django.conf import settings

from .base import SMSProvider, SMSProviderError

logger = logging.getLogger("shop.sms.development")


class ConsoleSMSProvider(SMSProvider):
    """
    پیامک‌ها را فقط در محیط Development در Console نمایش می‌دهد.

    در Production عمداً خطا می‌دهد تا هرگز به‌صورت ناخواسته فعال نشود
    و پیامک واقعی ارسال نشود.
    """

    name = "console"

    def send(self, phone_number: str, message: str) -> None:
        if not settings.DEBUG:
            raise SMSProviderError(
                "Provider پیامک 'console' فقط برای Development است. "
                "در Production یک Provider واقعی تنظیم کنید."
            )
        # این چاپ تنها کانال نمایش OTP در Development است.
        # در لاگ‌های امنیتی اصلی هیچ کد OTP ثبت نمی‌شود.
        logger.info("[Development SMS] → %s | %s", phone_number, message)
