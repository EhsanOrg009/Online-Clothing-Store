#!/bin/bash
# =====================================================================
#  بارگذاری جدول‌های Timezone در MariaDB
#
#  چرا لازم است؟
#  `date_hierarchy` در Django Admin از `CONVERT_TZ` استفاده می‌کند. اگر
#  جدول‌های Timezone خالی باشند، `CONVERT_TZ` مقدار NULL برمی‌گرداند و
#  ادمین با خطای `Database returned an invalid datetime value` مواجه
#  می‌شود.
#
#  این اسکریپت فقط یک بار، هنگام ساخت اولیه‌ی Volume دیتابیس اجرا
#  می‌شود (توسط docker-entrypoint-initdb.d).
# =====================================================================

set -euo pipefail

echo "[init] در حال بارگذاری جدول‌های Timezone..."

# MARIADB_ROOT_PASSWORD توسط ایمیج رسمی تعریف شده است
mysql --protocol=socket -uroot -p"${MARIADB_ROOT_PASSWORD}" mysql <<'SQL'
SELECT COUNT(*) AS timezone_rows_before FROM time_zone_name;
SQL

mysql_tzinfo_to_sql /usr/share/zoneinfo \
  | mysql --protocol=socket -uroot -p"${MARIADB_ROOT_PASSWORD}" mysql

mysql --protocol=socket -uroot -p"${MARIADB_ROOT_PASSWORD}" -e \
  "SELECT COUNT(*) AS timezone_rows_after FROM mysql.time_zone_name;"

echo "[init] جدول‌های Timezone بارگذاری شدند."
