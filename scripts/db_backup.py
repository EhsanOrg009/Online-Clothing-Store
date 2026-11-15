#!/usr/bin/env python3
"""Container-owned MariaDB backups, integrity checks, retention and UTC scheduling.

Only the standard library and the official MariaDB/gzip clients are used. The
shared bind-mounted flock covers schedulers, manual one-off containers and
retention. Importing this module performs no operations (also used by recovery).
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
from threading import Event, Thread
import time
import zlib


INTERVAL_SECONDS = 1800
PARTIAL_MAX_AGE_SECONDS = 86400
NAME_PATTERN = re.compile(r"db-([0-9]{8}T[0-9]{6}Z)-([0-9a-f]{12})\.sql\.gz")
PARTIAL_PATTERN = re.compile(
    r"\.db-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\.sql\.gz(?:\.sha256)?\.[a-z0-9_]+\.partial"
)
HEARTBEAT_PATH = Path("/tmp/minikandoo-db-backup-heartbeat.json")


class BackupError(Exception):
    """Messages contain operational facts only, never client errors or SQL."""


class BackupBusy(BackupError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def log(event, **details):
    print(json.dumps({"time": utc_now().isoformat(), "event": event, **details}), flush=True)


def _integer(env, key, default, minimum=0, optional=False):
    value = env.get(key, str(default) if default is not None else "")
    if optional and value == "":
        return None
    if not re.fullmatch(r"[0-9]+", value or "") or int(value) < minimum:
        raise BackupError(f"Invalid {key}; use an integer >= {minimum}.")
    return int(value)


@dataclass(frozen=True)
class Config:
    directory: Path
    database: str = field(default="", repr=False)
    user: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    host: str = field(default="db", repr=False)
    port: int = 3306
    recent: int = 48
    daily_days: int = 7
    retention_mode: str = "dry-run"
    max_total_bytes: int | None = None
    min_free_bytes: int | None = None
    non_transactional_policy: str = "refuse"
    command_timeout: int = 1200

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        config = cls(
            directory=Path(env.get("BACKUP_DIRECTORY", "/backups")),
            database=env.get("DATABASE_NAME", ""), user=env.get("DATABASE_USER", ""),
            password=env.get("DATABASE_PASSWORD", ""), host=env.get("DATABASE_HOST", "db"),
            port=_integer(env, "DATABASE_PORT", 3306, 1),
            recent=_integer(env, "BACKUP_RETENTION_RECENT", 48, 1),
            daily_days=_integer(env, "BACKUP_RETENTION_DAILY_DAYS", 7),
            retention_mode=env.get("BACKUP_RETENTION_MODE", "dry-run"),
            max_total_bytes=_integer(env, "BACKUP_MAX_TOTAL_BYTES", None, 1, optional=True),
            min_free_bytes=_integer(env, "BACKUP_MIN_FREE_BYTES", None, optional=True),
            non_transactional_policy=env.get("BACKUP_NON_TRANSACTIONAL_POLICY", "refuse"),
            command_timeout=_integer(env, "BACKUP_COMMAND_TIMEOUT_SECONDS", 1200, 1),
        )
        if config.retention_mode not in ("dry-run", "apply"):
            raise BackupError("BACKUP_RETENTION_MODE must be dry-run or apply.")
        if config.non_transactional_policy not in ("refuse", "lock-all"):
            raise BackupError("BACKUP_NON_TRANSACTIONAL_POLICY must be refuse or lock-all.")
        if config.port > 65535:
            raise BackupError("Invalid DATABASE_PORT.")
        return config


@contextmanager
def directory_lock(directory):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Never unlink this lock: all container instances must lock the same inode.
    fd = os.open(directory / ".db-backup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BackupError("Backup lock is not a regular file.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupBusy("Another backup or retention process holds the directory lock.") from exc
        yield
    finally:
        os.close(fd)


@contextmanager
def protect_recovery_backup(path):
    """Keep a managed recovery point available for the entire apply operation.

    The app's backup mount can stay read-only: flock works on a read-only fd.
    Legacy filenames are never removed by this service's retention policy.
    """
    path = Path(path)
    if not NAME_PATTERN.fullmatch(path.name):
        yield
        return
    try:
        fd = os.open(path.parent / ".db-backup.lock", os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise BackupError("Managed backup directory lock is missing or unreadable.") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BackupError("Backup lock is not a regular file.")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupBusy("Backup or retention is active; retry recovery after it completes.") from exc
        yield
    finally:
        os.close(fd)


def _regular_file(path):
    try:
        info = Path(path).lstat()
    except OSError as exc:
        raise BackupError("File or checksum sidecar is missing or unreadable.") from exc
    if not stat.S_ISREG(info.st_mode):
        raise BackupError("Backups and checksum sidecars must be regular files, not symlinks.")
    return info


def verify_backup_checksum(path, *, sidecar=None, expected_filename=None):
    """Compare the actual digest to the recorded sidecar; never trust its path.

    Legacy sidecars can contain the original absolute host path. Match its
    basename, then hash the requested file (e.g. its read-only container mount).
    """
    path = Path(path)
    sidecar = Path(sidecar) if sidecar is not None else Path(str(path) + ".sha256")
    info = _regular_file(path)
    side_info = _regular_file(sidecar)
    try:
        if not info.st_size or side_info.st_size > 1024:
            raise BackupError("Empty backup or invalid checksum sidecar size.")
        match = re.fullmatch(r"([0-9a-fA-F]{64}) ([ *])([^\r\n]+)\n?", sidecar.read_text("ascii"))
        if not match or Path(match[3]).name != (expected_filename or path.name):
            raise BackupError("Malformed SHA256 sidecar or backup filename mismatch.")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if not hmac.compare_digest(match[1].lower(), digest):
            raise BackupError("Recorded SHA256 does not match the backup.")
        after = _regular_file(path)
        side_after = _regular_file(sidecar)
        for before, current in ((info, after), (side_info, side_after)):
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                current.st_ino, current.st_size, current.st_mtime_ns,
            ):
                raise BackupError("Backup or sidecar changed during verification.")
        return digest
    except (OSError, UnicodeError, ValueError) as exc:
        raise BackupError("Backup or checksum sidecar is unreadable.") from exc


def verify_gzip(path):
    """Read to EOF to check CRC/trailer and reject an empty uncompressed dump."""
    size = 0
    try:
        with gzip.open(path, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise BackupError("Backup gzip integrity verification failed.") from exc
    if size == 0:
        raise BackupError("Database dump is empty.")
    return size


@dataclass(frozen=True)
class RecoveryPoint:
    path: Path
    timestamp: datetime
    size: int
    uncompressed_size: int
    digest: str

    @property
    def pair_size(self):
        return self.size + Path(str(self.path) + ".sha256").stat().st_size


def scan_recovery_points(directory):
    valid, invalid = [], []
    for path in sorted(Path(directory).iterdir()):
        match = NAME_PATTERN.fullmatch(path.name)
        if not match:
            continue  # Legacy/unrelated names are never retention candidates.
        try:
            stamp = datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            digest = verify_backup_checksum(path)
            size = verify_gzip(path)
            valid.append(RecoveryPoint(path, stamp, path.stat().st_size, size, digest))
        except (BackupError, ValueError):
            invalid.append(path.name)
    # Manual backups can share a filename's UTC second. Use the actual write
    # order for ties, so a smaller random suffix cannot delete the newest dump.
    return sorted(valid, key=lambda point: (point.timestamp, point.path.stat().st_mtime_ns, point.path.name)), invalid


def storage_bytes(directory):
    # Include historical files, partials and unrelated files in capacity usage;
    # never follow symlinks to paths outside the backup directory.
    total = 0
    for root, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if not Path(root, name).is_symlink()]
        for name in files:
            info = Path(root, name).lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def make_retention_plan(config, *, now=None):
    now = now or utc_now()
    points, invalid = scan_recovery_points(config.directory)
    keep = {point.path for point in points[-max(2, config.recent):]}
    previous_days = {now.date() - timedelta(days=day) for day in range(1, config.daily_days + 1)}
    daily = {}
    for point in points:
        if point.timestamp.date() in previous_days:
            daily[point.timestamp.date()] = point.path
        # Protect future-dated files if the host clock has moved backwards.
        if point.timestamp > now:
            keep.add(point.path)
    keep.update(daily.values())
    delete = [point for point in points if point.path not in keep]
    stale = [
        path for path in config.directory.iterdir()
        if PARTIAL_PATTERN.fullmatch(path.name) and not path.is_symlink()
        and path.is_file() and now.timestamp() - path.stat().st_mtime > PARTIAL_MAX_AGE_SECONDS
    ]
    return {
        "retained": [point.path.name for point in points if point.path in keep],
        "delete": [point.path.name for point in delete],
        "invalid_preserved": invalid,
        "stale_partials": [path.name for path in sorted(stale)],
        "bytes_used": storage_bytes(config.directory),
        "estimated_bytes_freed": sum(point.pair_size for point in delete) + sum(path.stat().st_size for path in stale),
        "available_bytes": shutil.disk_usage(config.directory).free,
        "retained_count": len(keep), "delete_count": len(delete),
        "daily_count": len(daily), "recent_count": min(config.recent, len(points)),
        "daily_overlap_count": len(set(daily.values()) & {p.path for p in points[-config.recent:]}),
    }


def _retention_locked(config, *, dry_run=False, now=None):
    plan = make_retention_plan(config, now=now)
    preview = dry_run or config.retention_mode == "dry-run"
    log("retention_plan", mode="dry-run" if preview else "apply", **plan)
    deleted = 0
    if not preview:
        for name in plan["delete"]:
            path = config.directory / name
            verify_backup_checksum(path)  # Recheck immediately before deletion.
            # Both files are managed and removed under the same persistent lock.
            path.unlink()
            Path(str(path) + ".sha256").unlink()
            deleted += 1
        for name in plan["stale_partials"]:
            (config.directory / name).unlink()
    log(
        "retention_complete", mode="dry-run" if preview else "apply",
        retained=plan["retained_count"], deleted=deleted,
        stale_partials_deleted=0 if preview else len(plan["stale_partials"]),
        bytes_used=storage_bytes(config.directory), available_bytes=shutil.disk_usage(config.directory).free,
    )
    return plan


def _cleanup_stale_partials_locked(config, now):
    # Clear only service-owned, >24h abandoned partials before an attempt. This
    # also recovers space after repeated failures; valid backups are untouched.
    stale = [
        path for path in config.directory.iterdir()
        if PARTIAL_PATTERN.fullmatch(path.name) and not path.is_symlink()
        and path.is_file() and now.timestamp() - path.stat().st_mtime > PARTIAL_MAX_AGE_SECONDS
    ]
    if not stale:
        return
    freed = sum(path.stat().st_size for path in stale)
    apply = config.retention_mode == "apply"
    if apply:
        for path in stale:
            path.unlink()
    log("stale_partial_cleanup", mode=config.retention_mode, candidates=len(stale),
        deleted=len(stale) if apply else 0, bytes_freed=freed if apply else 0)


def run_retention(config, *, dry_run=False, now=None):
    with directory_lock(config.directory):
        return _retention_locked(config, dry_run=dry_run, now=now)


def _option_value(value):
    if "\x00" in value:
        raise BackupError("Invalid database client configuration.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace(
        "\n", "\\n"
    ).replace("\r", "\\r").replace("\t", "\\t") + '"'


@contextmanager
def client_options(config):
    if not all((config.database, config.user, config.password, config.host)):
        raise BackupError("Database client configuration is incomplete.")
    if config.database.startswith("-") or any(c in config.database for c in "\x00\n\r"):
        raise BackupError("Invalid database name.")
    with tempfile.TemporaryDirectory(prefix="minikandoo-mariadb-client-") as temporary:
        path = Path(temporary) / "client.cnf"
        with path.open("x", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            stream.write("[client]\nprotocol=tcp\ndefault-character-set=utf8mb4\n")
            for key, value in (("host", config.host), ("user", config.user), ("password", config.password)):
                stream.write(f"{key}={_option_value(value)}\n")
            stream.write(f"port={config.port}\n")
        yield path


def client_environment():
    # The child processes need no application/API secrets or database password
    # environment variables. Credentials exist only in the private option file.
    return {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C.UTF-8", "TZ": "UTC"}


def inspect_database(config, options):
    query = (
        "SELECT ENGINE, COUNT(*), COALESCE(SUM(DATA_LENGTH + INDEX_LENGTH), 0) "
        "FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_TYPE='BASE TABLE' GROUP BY ENGINE ORDER BY ENGINE"
    )
    try:
        result = subprocess.run(
            ["mariadb", f"--defaults-file={options}", "--connect-timeout=10", "--batch",
             "--skip-column-names", config.database, "--execute", query],
            capture_output=True, env=client_environment(), timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError("MariaDB client unavailable or database inquiry timed out.") from exc
    if result.returncode:
        raise BackupError("MariaDB is unavailable or backup metadata privileges are insufficient.")
    engines = {}
    try:
        for line in result.stdout.decode("ascii").splitlines():
            engine, count, size = line.split("\t")
            if not re.fullmatch(r"[A-Za-z0-9_]+", engine) or int(count) < 1 or int(size) < 0:
                raise ValueError
            engines[engine] = {"tables": int(count), "bytes": int(size)}
    except (UnicodeError, ValueError) as exc:
        raise BackupError("Invalid database table-engine/size inquiry.") from exc
    if not engines:
        raise BackupError("Database contains no base tables; refusing an empty recovery point.")
    log("database_inspected", engines=engines, database_bytes=sum(v["bytes"] for v in engines.values()))
    return engines


def capacity_check(config, engines, points):
    # Estimate from observed DB bytes (allow hex expansion of binary data) and
    # prior real SQL sizes. This is an estimate, never a claim about VPS capacity.
    estimate = max(1, 2 * sum(v["bytes"] for v in engines.values()),
                   max((point.uncompressed_size for point in points), default=0))
    reserve = estimate if config.min_free_bytes is None else config.min_free_bytes
    used = storage_bytes(config.directory)
    free = shutil.disk_usage(config.directory).free
    log("capacity_check", bytes_used=used, available_bytes=free,
        estimated_new_bytes=estimate, min_free_bytes=reserve, max_total_bytes=config.max_total_bytes)
    if free < estimate + reserve:
        raise BackupError("Insufficient free disk space for the estimated dump and free-space reserve.")
    if config.max_total_bytes is not None and used + estimate > config.max_total_bytes:
        raise BackupError("Backup storage capacity limit would be exceeded; previous recovery points preserved.")
    return used, reserve


def _fsync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _temporary(directory, name):
    fd, path = tempfile.mkstemp(prefix="." + name + ".", suffix=".partial", dir=directory)
    os.close(fd)
    return Path(path)


def _dump_to_gzip(config, options, partial, consistency_flag, used, reserve, stop_event):
    processes = []
    try:
        with partial.open("wb") as output:
            compressor = subprocess.Popen(
                ["gzip", "-n", "-c"], stdin=subprocess.PIPE, stdout=output,
                stderr=subprocess.DEVNULL, env=client_environment(),
            )
            processes.append(compressor)
            # mariadb-dump does not support the mariadb SQL client's
            # --connect-timeout option. The worker deadline covers connection,
            # dump and compression together, including an unreachable server.
            dump = subprocess.Popen(
                ["mariadb-dump", f"--defaults-file={options}",
                 consistency_flag, "--quick", "--routines", "--events", "--triggers", "--hex-blob",
                 config.database], stdout=compressor.stdin, stderr=subprocess.DEVNULL,
                env=client_environment(),
            )
            processes.append(dump)
            compressor.stdin.close()
            deadline = time.monotonic() + config.command_timeout
            while any(process.poll() is None for process in processes):
                if stop_event.is_set():
                    raise BackupError("Backup interrupted by container shutdown; no recovery point published.")
                if time.monotonic() > deadline:
                    raise BackupError("Database dump/compression timed out.")
                if shutil.disk_usage(config.directory).free < reserve:
                    raise BackupError("Free disk space reserve reached during dump.")
                if config.max_total_bytes is not None and used + partial.stat().st_size > config.max_total_bytes:
                    raise BackupError("Backup storage capacity limit reached during dump.")
                stop_event.wait(0.2)
            if dump.returncode:
                raise BackupError(f"Database dump failed (exit {dump.returncode}); partial is not a recovery point.")
            if compressor.returncode:
                raise BackupError("Database dump compression failed.")
            # A fast process may finish between polling intervals; enforce the
            # capacity limits on its final output too, before publishing it.
            if shutil.disk_usage(config.directory).free < reserve:
                raise BackupError("Free disk space reserve reached during dump.")
            if config.max_total_bytes is not None and used + partial.stat().st_size > config.max_total_bytes:
                raise BackupError("Backup storage capacity limit reached during dump.")
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        raise BackupError("Backup client or output file unavailable.") from exc
    finally:
        for process in reversed(processes):
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def perform_backup(config, *, now=None, stop_event=None):
    stop_event = stop_event or Event()
    now = now or utc_now()
    if now.tzinfo is None or now.utcoffset() is None:
        raise BackupError("Backup clock must have an explicit timezone.")
    now = now.astimezone(timezone.utc)
    with directory_lock(config.directory):
        _cleanup_stale_partials_locked(config, now)
        name = f"db-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(6)}.sql.gz"
        final = config.directory / name
        sidecar = Path(str(final) + ".sha256")
        if final.exists() or sidecar.exists():
            raise BackupError("Unique backup name collision; existing files were preserved.")
        log("backup_started", filename=name)
        with client_options(config) as options:
            engines = inspect_database(config, options)
            non_transactional = any(engine.lower() != "innodb" for engine in engines)
            if non_transactional and config.non_transactional_policy != "lock-all":
                raise BackupError("Non-InnoDB tables found; review consistency and explicitly approve lock-all or migrate engines.")
            consistency_flag = "--lock-all-tables" if non_transactional else "--single-transaction"
            log("dump_strategy", consistency=consistency_flag)
            points, invalid = scan_recovery_points(config.directory)
            used, reserve = capacity_check(config, engines, points)
            partial = _temporary(config.directory, name)
            _dump_to_gzip(config, options, partial, consistency_flag, used, reserve, stop_event)
        try:
            check = subprocess.run(["gzip", "-t", "--", str(partial)], stderr=subprocess.DEVNULL, timeout=config.command_timeout)
            if check.returncode:
                raise BackupError("gzip -t failed; partial is not a recovery point.")
            uncompressed_size = verify_gzip(partial)
            with partial.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            side_partial = _temporary(config.directory, name + ".sha256")
            with side_partial.open("w", encoding="ascii") as stream:
                stream.write(f"{digest}  {name}\n")
                stream.flush()
                os.fsync(stream.fileno())
            verify_backup_checksum(partial, sidecar=side_partial, expected_filename=name)
            # Enforce the final pair's capacity too: the sidecar is part of
            # backup storage, not free overhead outside the configured ceiling.
            if config.max_total_bytes is not None and storage_bytes(config.directory) > config.max_total_bytes:
                raise BackupError("Backup storage capacity limit reached by dump and checksum sidecar.")
            if shutil.disk_usage(config.directory).free < reserve:
                raise BackupError("Free disk space reserve reached during checksum validation.")
            # Publish sidecar first, completed dump last, using atomic no-clobber
            # hard links on the same filesystem. Never overwrite a destination.
            published = []
            try:
                os.link(side_partial, sidecar)
                published.append(sidecar)
                _fsync_directory(config.directory)
                os.link(partial, final)
                published.append(final)
                verify_backup_checksum(final)
                _fsync_directory(config.directory)
            except (OSError, BackupError) as exc:
                # Only remove links created by this attempt; partials remain.
                for path in reversed(published):
                    path.unlink()
                raise BackupError("Atomic backup publication or final checksum verification failed.") from exc
            partial.unlink()
            side_partial.unlink()
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackupError("Backup integrity validation failed; previous backups preserved.") from exc
        point = RecoveryPoint(final, now, final.stat().st_size, uncompressed_size, digest)
        log("backup_completed", filename=name, size_bytes=point.size, uncompressed_bytes=uncompressed_size,
            checksum_verified=True, bytes_used=storage_bytes(config.directory),
            available_bytes=shutil.disk_usage(config.directory).free, invalid_backups_preserved=len(invalid))
        # No cleanup on any failed dump/validation/publication attempt.
        _retention_locked(config)
        return point


def next_half_hour(now):
    return datetime.fromtimestamp(
        (int(now.timestamp()) // INTERVAL_SECONDS + 1) * INTERVAL_SECONDS, timezone.utc,
    )


def _write_heartbeat(status):
    payload = {**status, "heartbeat_at": utc_now().isoformat(), "pid": os.getpid()}
    fd, name = tempfile.mkstemp(prefix="minikandoo-backup-heartbeat-", dir=HEARTBEAT_PATH.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream)
        os.replace(name, HEARTBEAT_PATH)
    finally:
        Path(name).unlink(missing_ok=True)


def schedule(config, *, stop_event=None, backup_function=perform_backup, clock=utc_now):
    stop_event = stop_event or Event()
    status = {"state": "starting", "last_result": None, "last_success_at": None}

    def heartbeat():
        while not stop_event.is_set():
            _write_heartbeat(status)
            stop_event.wait(10)

    thread = Thread(target=heartbeat, daemon=True)
    thread.start()
    log("scheduler_started", interval_seconds=INTERVAL_SECONDS, initial_backup=True)
    try:
        while not stop_event.is_set():
            status["state"] = "backing_up"
            try:
                backup_function(config, stop_event=stop_event)
                status.update(last_result="success", last_success_at=clock().isoformat())
            except (BackupError, OSError) as exc:
                status["last_result"] = "failed"
                log("backup_failed", reason=str(exc) if isinstance(exc, BackupError) else "Filesystem operation failed.")
            except Exception:
                # An unexpected attempt failure must not end all future attempts;
                # do not print exception bodies, which could include client data.
                status["last_result"] = "failed"
                log("backup_failed", reason="Unexpected backup worker failure; next scheduled attempt will retry.")
            status["state"] = "waiting"
            deadline = next_half_hour(clock())
            log("scheduler_waiting", next_attempt_at=deadline.isoformat(), last_result=status["last_result"])
            _write_heartbeat(status)
            # Recompute against the UTC clock in short waits (clock adjustments).
            while not stop_event.is_set() and (delay := (deadline - clock()).total_seconds()) > 0:
                stop_event.wait(min(delay, 10))
    finally:
        stop_event.set()
        thread.join(timeout=2)
        status["state"] = "stopped"
        _write_heartbeat(status)
        log("scheduler_stopped")


def healthcheck():
    try:
        status = json.loads(HEARTBEAT_PATH.read_text())
        heartbeat_at = datetime.fromisoformat(status["heartbeat_at"])
        success_at = datetime.fromisoformat(status["last_success_at"])
        os.kill(status["pid"], 0)
        return 0 if (
            status["state"] != "stopped" and status["last_result"] == "success"
            and 0 <= (utc_now() - heartbeat_at).total_seconds() < 90
            and 0 <= (utc_now() - success_at).total_seconds() < 2 * INTERVAL_SECONDS
        ) else 1
    except (OSError, ValueError, TypeError, KeyError):
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("schedule", "backup", "retention", "inspect", "healthcheck"))
    parser.add_argument("--dry-run", action="store_true", help="Retention preview only; never deletes files.")
    args = parser.parse_args(argv)
    if args.command == "healthcheck":
        return healthcheck()
    os.umask(0o077)
    stop_event = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop_event.set())
    try:
        config = Config.from_env()
        if args.command == "schedule":
            schedule(config, stop_event=stop_event)
        elif args.command == "backup":
            if args.dry_run:
                raise BackupError("Use retention --dry-run or inspect for a read-only preview.")
            perform_backup(config, stop_event=stop_event)
        elif args.command == "retention":
            run_retention(config, dry_run=args.dry_run)
        else:
            with directory_lock(config.directory), client_options(config) as options:
                engines = inspect_database(config, options)
                points, _ = scan_recovery_points(config.directory)
                _retention_locked(config, dry_run=True)
                capacity_check(config, engines, points)
        return 0
    except BackupBusy as exc:
        log("operation_busy", reason=str(exc))
        return 75
    except (BackupError, OSError) as exc:
        log("operation_failed", reason=str(exc) if isinstance(exc, BackupError) else "Filesystem operation failed.")
        return 1
    except Exception as exc:
        log("operation_failed", reason=f"Unexpected backup worker failure ({type(exc).__name__}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
