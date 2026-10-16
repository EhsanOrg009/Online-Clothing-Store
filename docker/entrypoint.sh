#!/bin/sh
# =====================================================================
#  نقطه شروع کانتینر فروشگاه
#
#  کارها به ترتیب:
#    ۱) منتظر می‌ماند تا دیتابیس واقعاً آماده باشد (نه فقط پورت باز)
#    ۲) خطای اتصال را با پیام روشن فارسی گزارش می‌کند
#    ۳) فایل‌های static را جمع‌آوری می‌کند (اگر لازم باشد)
#    ۴) Migration را اجرا می‌کند
#    ۵) دستور درخواستی را اجرا می‌کند
#
#  برای جلوگیری از اجرای خودکار Migration در چند Worker همزمان، فقط
#  کانتینر اصلی این کار را می‌کند (MIGRATE_ON_START=0 برای غیرفعال‌کردن).
# =====================================================================

set -eu

cd /app

MIGRATE_ON_START="${MIGRATE_ON_START:-1}"
COLLECTSTATIC_ON_START="${COLLECTSTATIC_ON_START:-1}"
SEED_ON_START="${SEED_ON_START:-0}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-90}"

log() { printf '\n\033[1;36m[entrypoint]\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------
# ۱) انتظار برای دیتابیس
# ---------------------------------------------------------------------
wait_for_db() {
  log "در انتظار آماده‌شدن دیتابیس (حداکثر ${WAIT_TIMEOUT} ثانیه)..."
  python <<'PY'
import os
import sys
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
django.setup()

from django.db import connection  # noqa: E402

timeout = int(os.environ.get("WAIT_TIMEOUT", "90"))
deadline = time.monotonic() + timeout
attempt = 0

while True:
    attempt += 1
    try:
        connection.ensure_connection()
    except Exception as exc:  # noqa: BLE001 - گزارش روشن به اپراتور
        if time.monotonic() >= deadline:
            sys.exit(
                "\n\033[1;31m[entrypoint] اتصال به دیتابیس ناموفق بود "
                f"پس از {timeout} ثانیه.\n"
                f"  خطا: {exc}\n\n"
                "  نکته: نام کانتینر دیتابیس باید دقیقاً با DATABASE_HOST "
                "یکسان باشد (مثلاً `db`).\n"
                "  مقادیر فعلی:\n"
                f"    DATABASE_HOST={os.environ.get('DATABASE_HOST')}\n"
                f"    DATABASE_PORT={os.environ.get('DATABASE_PORT')}\n"
                f"    DATABASE_NAME={os.environ.get('DATABASE_NAME')}\n"
                f"    DATABASE_USER={os.environ.get('DATABASE_USER')}\n"
                "  برای بررسی لاگ دیتابیس: docker compose logs db\n"
                "\033[0m"
            )
        if attempt == 1 or attempt % 5 == 0:
            print(f"[entrypoint]   تلاش {attempt}: {exc}", flush=True)
        time.sleep(2)
    else:
        # نکته: cursor.execute() تعداد سطرها را برمی‌گرداند؛ برای خواندن
        # مقدار واقعی باید از fetchone() استفاده شود.
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            server_version = cursor.fetchone()[0]
        print(f"[entrypoint] دیتابیس متصل شد — نسخه: {server_version}", flush=True)
        break
PY
}

wait_for_db

# ---------------------------------------------------------------------
# ۲) جمع‌آوری فایل‌های static
# ---------------------------------------------------------------------
if [ "$COLLECTSTATIC_ON_START" = "1" ]; then
  log "جمع‌آوری فایل‌های static..."
  python manage.py collectstatic --noinput --clear >/dev/null
fi

# ---------------------------------------------------------------------
# ۳) اعمال Migration
# ---------------------------------------------------------------------
if [ "$MIGRATE_ON_START" = "1" ]; then
  log "اجرای Migration..."
  python manage.py migrate --noinput
fi

# ---------------------------------------------------------------------
# ۴) داده‌های نمونه (فقط در صورت درخواست صریح)
# ---------------------------------------------------------------------
if [ "$SEED_ON_START" = "1" ]; then
  log "ساخت داده‌های نمونه..."
  python manage.py seed_demo_data || log "داده‌های نمونه ساخته نشد (احتمالاً از قبل وجود دارند)."
fi

# ---------------------------------------------------------------------
# ۵) اجرای دستور درخواستی
# ---------------------------------------------------------------------
log "اجرای: $*"
exec "$@"
