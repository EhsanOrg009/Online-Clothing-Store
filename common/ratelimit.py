"""
سرویس محدودسازی نرخ درخواست (Rate Limiting) مبتنی بر Cache.

برای جلوگیری از Spam کردن سرویس OTP و سایر نقاط حساس استفاده می‌شود.
در Production چند-پردازشی، Cache را با Redis جایگزین کنید تا شمارنده‌ها مشترک شوند.

نکته پیاده‌سازی: از `cache.ttl()` استفاده نمی‌شود چون این متد اختصاصی
در همه Cache Backend ها (مثلاً LocMemCache) موجود نیست؛ به‌جای آن
مهلت پایان بازه در یک کلید همراه نگهداری می‌شود.
"""

import time
from ipaddress import ip_address

from django.conf import settings
from django.core.cache import cache


def _key(scope: str, identifier: str) -> str:
    return f"ratelimit:{scope}:{identifier}"


def _deadline_key(scope: str, identifier: str) -> str:
    return f"ratelimit:deadline:{scope}:{identifier}"


def allow_request(scope: str, identifier: str, limit: int, window_seconds: int) -> bool:
    """
    آیا درخواست جدید در محدوده مجاز است؟

    شمارنده به‌صورت اتمیک در Cache افزایش می‌یابد و پس از پایان بازه زمانی ریست می‌شود.
    """
    if limit <= 0:
        return False
    key = _key(scope, identifier)
    deadline = time.time() + window_seconds
    try:
        added = cache.add(key, 1, timeout=window_seconds)
        if added:
            cache.set(_deadline_key(scope, identifier), deadline, timeout=window_seconds)
            count = 1
        else:
            count = cache.incr(key)
    except ValueError:  # کلید منقضی شده بین add و incr
        cache.set(key, 1, timeout=window_seconds)
        cache.set(_deadline_key(scope, identifier), deadline, timeout=window_seconds)
        count = 1
    return count <= limit


def retry_after(scope: str, identifier: str) -> int:
    """باقی‌مانده زمان انتظار تا پایان بازه (ثانیه)."""
    deadline = cache.get(_deadline_key(scope, identifier))
    if not deadline:
        return 0
    return max(0, int(deadline - time.time()))


def reset(scope: str, identifier: str) -> None:
    cache.delete(_key(scope, identifier))
    cache.delete(_deadline_key(scope, identifier))


def _trusted_networks():
    """شبکه‌های پراکسی مورد اعتماد (IP یا CIDR) از تنظیمات."""
    from ipaddress import ip_network

    networks = []
    for entry in settings.TRUSTED_PROXY_IPS:
        try:
            networks.append(ip_network(str(entry).strip(), strict=False))
        except ValueError:
            # مقدار نامعتبر نادیده گرفته می‌شود؛ امن‌ترین حالت: اعتماد نکردن.
            continue
    return networks


def _is_trusted(candidate: str, networks) -> bool:
    from ipaddress import ip_address

    try:
        address = ip_address(candidate)
    except ValueError:
        return False
    return any(address in network for network in networks)


def client_ip(request) -> str:
    """Only accept forwarded IPs when the immediate peer is explicitly trusted.

    `TRUSTED_PROXY_IPS` هم IP دقیق و هم بازه‌ی CIDR را می‌پذیرد؛ در Docker
    آدرس کانتینر nginx داخل شبکه‌ی bridge است (مثلاً `172.18.0.0/16`) و IP
    آن از قبل معلوم نیست، بنابراین باید بازه ثبت شود.
    """
    peer = request.META.get("REMOTE_ADDR", "")
    networks = _trusted_networks()
    chain = [peer]
    if _is_trusted(peer, networks):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        chain = [part.strip() for part in forwarded.split(",") if part.strip()] + chain
    try:
        for candidate in reversed(chain):
            normalized = str(ip_address(candidate))
            if not _is_trusted(candidate, networks):
                return normalized
        return str(ip_address(peer))
    except ValueError:
        return "unknown"
