#!/usr/bin/env python3
"""Explicit, short-lived Compose administration of the dedicated dump account."""

import argparse
import os
import subprocess

try:
    from scripts.db_backup import (
        BackupError, Config, client_environment, log, mariadb_client_options,
        validate_backup_credentials,
    )
except ModuleNotFoundError:
    from db_backup import (
        BackupError, Config, client_environment, log, mariadb_client_options, validate_backup_credentials,
    )


def sql_string(value):
    if not isinstance(value, str) or "\x00" in value:
        raise BackupError("Invalid backup provisioning configuration.")
    # Every session explicitly uses NO_BACKSLASH_ESCAPES before these literals.
    return "'" + value.replace("'", "''") + "'"


def sql_database_pattern(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\n\r"):
        raise BackupError("Invalid backup provisioning database.")
    # GRANT database identifiers are patterns even inside backticks. Escape
    # their wildcard syntax separately from SQL identifier quoting.
    value = value.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
    return "`" + value.replace("`", "``") + "`"


def execute(options, sql):
    try:
        result = subprocess.run(
            ["mariadb", "--defaults-file=" + str(options), "--connect-timeout=10",
             "--batch", "--skip-column-names"], input=sql.encode(),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=client_environment(), timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError("Backup provisioning could not connect to MariaDB.") from exc
    if result.returncode:
        raise BackupError("Backup provisioning failed; inspect configuration and administrator privileges privately.")
    return result.stdout


def provision(*, env=None, rotate_password=False, allow_lock_all=False):
    env = os.environ if env is None else env
    config = Config.from_env(env)
    if config.user.lower() == env.get("APPLICATION_DB_USER", "shop").lower():
        raise BackupError("Backup account must be separate from the application account.")
    validate_backup_credentials(config.user, config.password)
    root_password = env.get("MARIADB_ROOT_PASSWORD", "")
    if not root_password or root_password.startswith("REPLACE_"):
        raise BackupError("Administrator credential required only for explicit backup provisioning.")
    account = sql_string(config.user) + "@'%'"
    database = sql_database_pattern(config.database)
    session = "SET SESSION sql_mode='NO_BACKSLASH_ESCAPES';\n"
    with mariadb_client_options(config.database, "root", root_password, config.host, config.port) as options:
        counts = execute(options, session + (
            "SELECT (SELECT COUNT(*) FROM mysql.global_priv WHERE User=" + sql_string(config.user)
            + " AND Host<>'%'), (SELECT COUNT(*) FROM mysql.roles_mapping WHERE User="
            + sql_string(config.user) + " AND Host='%');\n"
        )).strip()
        if counts != b"0\t0":
            raise BackupError("Existing backup account has other host identities or roles; explicit administrative review required.")
        statements = [
            "CREATE USER IF NOT EXISTS " + account + " IDENTIFIED BY " + sql_string(config.password) + ";",
        ]
        if rotate_password:
            statements.append("ALTER USER " + account + " IDENTIFIED BY " + sql_string(config.password) + ";")
        # Only the explicitly named, separate backup account is changed. Reset
        # its grants to this exact set; no application/root account is touched.
        statements.extend([
            "REVOKE ALL PRIVILEGES, GRANT OPTION FROM " + account + ";",
            "GRANT SELECT, SHOW VIEW, TRIGGER, EVENT ON " + database + ".* TO " + account + ";",
            "GRANT SELECT ON mysql.proc TO " + account + ";",
        ])
        if allow_lock_all:
            statements.append("GRANT RELOAD ON *.* TO " + account + ";")
        execute(options, session + "\n".join(statements) + "\n")
    log("backup_account_provisioned", password_rotated=rotate_password, lock_all_authorized=allow_lock_all)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rotate-password", action="store_true")
    parser.add_argument("--allow-lock-all", action="store_true", help="Explicitly grant RELOAD for non-InnoDB global read locks.")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        provision(rotate_password=args.rotate_password, allow_lock_all=args.allow_lock_all)
        return 0
    except (BackupError, OSError):
        log("backup_account_provision_failed", reason="Provisioning failed; configuration and account require private review.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
