#!/usr/bin/env bash
# =====================================================================
#  راه‌اندازی سرور MySQL/MariaDB برای محیط توسعه این پروژه
#
#  این اسکریپت برای محیط‌هایی است که MySQL به‌صورت سرویس سیستمی اجرا
#  نمی‌شود (مثلاً بدون دسترسی root). در سرور استاندارد، MySQL را با
#  سرویس خود بالا بیاورید:
#      sudo systemctl start mysql      (یا mariadb)
#
#  استفاده:
#      bash scripts/mysql_dev.sh
# =====================================================================
set -euo pipefail

# مسیر نصب سرور (قابل تغییر با متغیر محیطی)
MARIADB_PREFIX="${MARIADB_PREFIX:-$HOME/.local/share/mariadb-11.4}"
DATA_DIR="${MYSQL_DATA_DIR:-$HOME/.local/share/mysql11-data}"
PORT="${MYSQL_PORT:-3306}"
CONFIG="$DATA_DIR/my.cnf"

# ---------------------------------------------------------------------
# بررسی تداخل پورت — رایج‌ترین علت خطای `Access denied for user 'shop'`
# ---------------------------------------------------------------------
# اجرای `/opt/lampp/lampp start` سرویس MariaDB داخل XAMPP را روی همین پورت
# بالا می‌آورد و جای دیتابیس این پروژه را می‌گیرد. در آن حالت Django با
# خطای (1045) «Access denied for user 'shop'@'localhost'» متوقف می‌شود، چون
# کاربر `shop` داخل دیتابیس XAMPP وجود ندارد.
if ss -ltn 2>/dev/null | grep -qw "127.0.0.1:$PORT"; then
  OWNER="$(ss -ltnp 2>/dev/null | grep -w "127.0.0.1:$PORT" \
           | grep -oE 'users:\(\("[^"]+"' | head -1 | cut -d'"' -f2 || true)"
  case "$OWNER" in
    *lampp*|*xampp*)
      echo "✗ پورت $PORT توسط XAMPP/LAMPP اشغال است («$OWNER»)." >&2
      echo "" >&2
      echo "  دیتابیس این پروژه بالا نمی‌آید و Django با خطای زیر مواجه می‌شود:" >&2
      echo "      Access denied for user 'shop'@'localhost'" >&2
      echo "" >&2
      echo "  راه‌حل (یکی را انتخاب کنید):" >&2
      echo "    • /opt/lampp/lampp stopmysql      # دیتابیس XAMPP را خاموش کنید" >&2
      echo "    • پورت XAMPP را در /opt/lampp/etc/my.cnf به 3308 عوض کنید" >&2
      echo "    • MYSQL_PORT=3308 bash scripts/mysql_dev.sh" >&2
      echo "" >&2
      echo "  برای مشاهده‌ی مالک پورت:  ss -ltnp | grep $PORT" >&2
      exit 1
      ;;
    "") : ;;
    *)
      echo "⚠  پورت $PORT توسط «$OWNER» اشغال است؛ اگر سرور دیگری است،" >&2
      echo "   با MYSQL_PORT=<پورت دیگر> این اسکریپت را اجرا کنید." >&2
      ;;
  esac
fi

if [ ! -x "$MARIADB_PREFIX/bin/mariadbd" ]; then
  echo "✗ سرور در $MARIADB_PREFIX پیدا نشد." >&2
  echo "  ابتدا MariaDB/MySQL را نصب کنید یا MARIADB_PREFIX را تنظیم کنید." >&2
  exit 1
fi

mkdir -p "$DATA_DIR"

if [ ! -d "$DATA_DIR/mysql" ]; then
  echo "→ مقداردهی اولیه دیتابیس در $DATA_DIR ..."
  "$MARIADB_PREFIX/scripts/mariadb-install-db" \
    --no-defaults \
    --user="$(id -un)" \
    --datadir="$DATA_DIR" \
    --auth-root-authentication-method=normal \
    --skip-test-db \
    --basedir="$MARIADB_PREFIX" > /dev/null
fi

if [ ! -f "$CONFIG" ]; then
  cat > "$CONFIG" <<EOF
[mysqld]
datadir=$DATA_DIR
socket=$DATA_DIR/mysql.sock
pid-file=$DATA_DIR/mysqld.pid
port=$PORT
bind-address=127.0.0.1
character-set-server=utf8mb4
collation-server=utf8mb4_unicode_ci
default-storage-engine=InnoDB
skip-symbolic-links=1
local-infile=0
innodb-buffer-pool-size=256M

[client]
socket=$DATA_DIR/mysql.sock
port=$PORT
user=root
EOF
fi

if "$MARIADB_PREFIX/bin/mariadb-admin" --defaults-file="$CONFIG" ping > /dev/null 2>&1; then
  echo "✓ سرور در حال اجراست (پورت $PORT)."
else
  echo "→ در حال اجرای سرور ..."
  nohup "$MARIADB_PREFIX/bin/mariadbd" \
    --defaults-file="$CONFIG" --console >> "$DATA_DIR/server.log" 2>&1 &
  for _ in $(seq 1 40); do
    if "$MARIADB_PREFIX/bin/mariadb-admin" --defaults-file="$CONFIG" ping > /dev/null 2>&1; then
      echo "✓ سرور با موفقیت اجرا شد (پورت $PORT)."
      break
    fi
    sleep 1
  done
  if ! "$MARIADB_PREFIX/bin/mariadb-admin" --defaults-file="$CONFIG" ping > /dev/null 2>&1; then
    echo "✗ اجرای سرور ناموفق بود. لاگ: $DATA_DIR/server.log" >&2
    exit 1
  fi
fi

# ساخت دیتابیس و کاربر پروژه (Idempotent)
DB_NAME="${DATABASE_NAME:-clothing_shop}"
DB_USER="${DATABASE_USER:-shop}"
DB_PASS="${DATABASE_PASSWORD:-shop_secret_2026}"

"$MARIADB_PREFIX/bin/mariadb" --defaults-file="$CONFIG" <<SQL
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$DB_USER'@'127.0.0.1' IDENTIFIED BY '$DB_PASS';
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'127.0.0.1';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';
-- دسترسی‌های لازم برای ساخت دیتابیس آزمایشی تست‌ها.
-- در Production فقط به دیتابیس همین پروژه محدود کنید.
GRANT ALL PRIVILEGES ON *.* TO '$DB_USER'@'127.0.0.1';
GRANT ALL PRIVILEGES ON *.* TO '$DB_USER'@'localhost';
FLUSH PRIVILEGES;
SQL

echo "✓ دیتابیس «$DB_NAME» و کاربر «$DB_USER» آماده است."

# بارگذاری جدول‌های Timezone (لازم برای CONVERT_TZ / date_hierarchy ادمین و Trunc*)
TZ_COUNT=$("$MARIADB_PREFIX/bin/mariadb" --defaults-file="$CONFIG" \
  -N -B -e "SELECT COUNT(*) FROM mysql.time_zone_name;" 2>/dev/null || echo 0)
if [ "${TZ_COUNT:-0}" -eq 0 ]; then
  echo "→ بارگذاری جدول‌های Timezone ..."
  TZINFO_BIN="$MARIADB_PREFIX/bin/mysql_tzinfo_to_sql"
  if [ -x "$TZINFO_BIN" ] && [ -d /usr/share/zoneinfo ]; then
    "$TZINFO_BIN" /usr/share/zoneinfo 2>/dev/null \
      | "$MARIADB_PREFIX/bin/mariadb" --defaults-file="$CONFIG" mysql || true
    TZ_COUNT=$("$MARIADB_PREFIX/bin/mariadb" --defaults-file="$CONFIG" \
      -N -B -e "SELECT COUNT(*) FROM mysql.time_zone_name;" 2>/dev/null || echo 0)
  fi
fi
echo "✓ جدول‌های Timezone: ${TZ_COUNT:-0} منطقه زمانی."
