#!/usr/bin/env bash
# =====================================================================
#  دادن مجوز ساخت دیتابیس‌های آزمایشی به کاربر اپلیکیشن
#
#  Django هنگام اجرای تست، دیتابیسی به نام `test_<NAME>` می‌سازد و در
#  پایان حذف می‌کند. تصویر رسمی MariaDB وقتی `MARIADB_USER` را می‌سازد،
#  فقط روی `MARIADB_DATABASE` مجوز می‌دهد؛ در نتیجه بدون این اسکریپت:
#
#      Access denied for user 'shop'@'%' to database 'test_clothing_shop'
#
#  ⚠️ دامنه‌ی مجوز عمداً بسیار محدود نگه داشته شده: فقط دیتابیس‌هایی که
#     با `test_` شروع می‌شوند. کاربر اپلیکیشن همچنان به هیچ دیتابیس
#     دیگری (از جمله `mysql`) دسترسی ندارد و اجرای تست در Production
#     همچنان توصیه نمی‌شود.
#
#  فقط هنگام ساخت اولیه‌ی Volume اجرا می‌شود (`/docker-entrypoint-initdb.d`).
#  اگر Volume از قبل ساخته شده، یا باید بازسازی شود یا دستور زیر را
#  یک‌بار اجرا کنید:
#      docker compose exec -T db mariadb -u root -p"..." < این دستورها
# =====================================================================

set -euo pipefail

: "${MARIADB_DATABASE:?MARIADB_DATABASE تنظیم نشده است}"
: "${MARIADB_USER:?MARIADB_USER تنظیم نشده است}"

echo "[init] دادن مجوز ساخت دیتابیس‌های test_* به کاربر ${MARIADB_USER}"

mariadb -u root <<SQL
GRANT CREATE, DROP, INDEX, ALTER, CREATE VIEW, CREATE ROUTINE, ALTER ROUTINE, EXECUTE, REFERENCES
  ON \`${MARIADB_DATABASE}\`.* TO '${MARIADB_USER}'@'%';

GRANT ALL PRIVILEGES ON \`test\_%\`.* TO '${MARIADB_USER}'@'%';

FLUSH PRIVILEGES;
SQL

echo "[init] مجوزها با موفقیت اعمال شد."
