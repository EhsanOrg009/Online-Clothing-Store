"""Private support inputs and the existing verified SQL-backup policy."""

from contextlib import contextmanager
import os
import stat

from django.core.management.base import CommandError
from django.db import connection

from payments.management.commands.reconcile_zibal_payments import validate_backup
from scripts.db_backup import BackupError, protect_recovery_backup


@contextmanager
def protected_backup(filename):
    if not filename:
        raise CommandError("عملیات دستی به بکاپ تازهٔ SQL و فایل SHA256 معتبر نیاز دارد.")
    try:
        with protect_recovery_backup(filename):
            report = validate_backup(filename, connection.settings_dict["NAME"])
            yield report
    except BackupError as error:
        raise CommandError("بکاپ برای عملیات دستی قابل تأیید نیست.") from error


def read_private_token(filename):
    """Never accept tokens on the command line, logs, or a browser callback."""
    descriptor = None
    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not 0 < info.st_size <= 16385:
            raise ValueError
        return os.read(descriptor, 16385).decode("utf-8").removesuffix("\n")
    except (OSError, ValueError, UnicodeError) as error:
        raise CommandError("توکن باید در فایل خصوصی معمولی با دسترسی ۶۰۰ و اندازهٔ محدود باشد.") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
