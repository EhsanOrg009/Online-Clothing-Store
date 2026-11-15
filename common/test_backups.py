"""Backup worker tests use private temporary directories and synthetic clients."""

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.test import SimpleTestCase

from scripts import db_backup as backup


ROOT = Path(__file__).resolve().parents[1]


class BackupWorkerTests(SimpleTestCase):
    now = datetime(2026, 11, 15, 8, tzinfo=timezone.utc)

    def setUp(self):
        self.temporary = Path(self.enterContext(TemporaryDirectory(prefix="minikandoo-backup-unit-")))
        self.directory = self.temporary / "backups"
        self.directory.mkdir(mode=0o700)
        self.bin_directory = self.temporary / "bin"
        self.bin_directory.mkdir()
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.enterContext(patch.dict(os.environ, {"PATH": str(self.bin_directory) + os.pathsep + os.environ["PATH"]}))
        self.config = backup.Config(
            self.directory, database="synthetic_test", user="test_client", password="test_only_private_value",
            retention_mode="apply", command_timeout=10,
        )
        self.sql = b"-- MariaDB dump\n-- Host: db    Database: synthetic_test\nSELECT 1;\n-- Dump completed on test\n"
        self.stub_client("mariadb", "printf 'InnoDB\\t3\\t1000\\n'\n")
        self.stub_client("mariadb-dump", "printf '%s\\n' '-- MariaDB dump' '-- Host: db    Database: synthetic_test' 'SELECT 1;' '-- Dump completed on test'\n")

    def stub_client(self, name, body):
        path = self.bin_directory / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o700)

    def point(self, stamp=None, *, name=None, contents=None, checksum=True):
        stamp = stamp or self.now
        if name is None:
            name = f"db-{stamp.strftime('%Y%m%dT%H%M%SZ')}-{os.urandom(6).hex()}.sql.gz"
        path = self.directory / name
        path.write_bytes(gzip.compress(self.sql if contents is None else contents, mtime=0))
        if checksum:
            self.checksum(path)
        return path

    @staticmethod
    def checksum(path, *, recorded_name=None):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        Path(str(path) + ".sha256").write_text(f"{digest}  {recorded_name or path.name}\n")
        return digest

    def perform(self, **kwargs):
        with patch.object(backup, "utc_now", return_value=self.now):
            return backup.perform_backup(self.config, now=self.now, **kwargs)

    def test_successful_backup_is_private_nonempty_and_verified(self):
        point = self.perform()
        self.assertEqual(point.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(str(point.path) + ".sha256").stat().st_mode & 0o777, 0o600)
        self.assertGreater(point.size, 0)
        self.assertGreater(backup.verify_gzip(point.path), 0)
        self.assertEqual(backup.verify_backup_checksum(point.path), point.digest)
        self.assertEqual(list(self.directory.glob("*.partial")), [])
        self.assertIn('"checksum_verified": true', self.output.getvalue())

    def test_unique_names_on_repeated_manual_attempts(self):
        first, second = self.perform(), self.perform()
        self.assertNotEqual(first.path, second.path)
        self.assertTrue(first.path.is_file())
        self.assertTrue(second.path.is_file())

    def test_timestamp_naming_is_utc(self):
        local = self.now.astimezone(timezone(timedelta(hours=3, minutes=30)))
        point = backup.perform_backup(self.config, now=local)
        self.assertTrue(point.path.name.startswith("db-20261115T080000Z-"))
        self.assertTrue(backup.NAME_PATTERN.fullmatch(point.path.name))

    def test_naive_backup_clock_is_rejected(self):
        with self.assertRaises(backup.BackupError):
            backup.perform_backup(self.config, now=self.now.replace(tzinfo=None))

    def test_name_collision_does_not_overwrite_either_file(self):
        path = self.point(name="db-20261115T080000Z-abcdef123456.sql.gz")
        original = path.read_bytes()
        with patch.object(backup.secrets, "token_hex", return_value="abcdef123456"):
            with self.assertRaises(backup.BackupError):
                self.perform()
        self.assertEqual(path.read_bytes(), original)
        backup.verify_backup_checksum(path)

    def test_sidecar_is_generated_in_portable_sha256sum_format(self):
        point = self.perform()
        self.assertEqual(Path(str(point.path) + ".sha256").read_text(), f"{point.digest}  {point.path.name}\n")

    def test_missing_sidecar_is_never_a_valid_recovery_point(self):
        path = self.point(checksum=False)
        with self.assertRaises(backup.BackupError):
            backup.verify_backup_checksum(path)
        points, invalid = backup.scan_recovery_points(self.directory)
        self.assertEqual(points, [])
        self.assertEqual(invalid, [path.name])

    def test_wrong_checksum_and_wrong_filename_are_rejected(self):
        path = self.point()
        for value in ("0" * 64 + "  " + path.name + "\n", "not a checksum", "f" * 64 + "  different.sql.gz\n"):
            Path(str(path) + ".sha256").write_text(value)
            with self.assertRaises(backup.BackupError):
                backup.verify_backup_checksum(path)

    def test_legacy_absolute_sidecar_matches_actual_mounted_file(self):
        path = self.point()
        digest = self.checksum(path, recorded_name="/root/minikandoo-backups/" + path.name)
        self.assertEqual(backup.verify_backup_checksum(path), digest)

    def test_corruption_is_detected_even_if_gzip_header_remains(self):
        path = self.point()
        path.write_bytes(path.read_bytes()[:-8])
        with self.assertRaises(backup.BackupError):
            backup.verify_backup_checksum(path)
        self.checksum(path)
        with self.assertRaises(backup.BackupError):
            backup.verify_gzip(path)

    def test_symlink_backup_or_sidecar_is_not_trusted(self):
        target = self.point()
        link = self.directory / "db-20261115T080000Z-000000000001.sql.gz"
        link.symlink_to(target)
        with self.assertRaises(backup.BackupError):
            backup.verify_backup_checksum(link)
        Path(str(target) + ".sha256").unlink()
        Path(str(target) + ".sha256").symlink_to(link)
        with self.assertRaises(backup.BackupError):
            backup.verify_backup_checksum(target)

    def test_failed_dump_is_partial_and_preserves_prior_points(self):
        first, second = self.point(self.now - timedelta(hours=2)), self.point(self.now - timedelta(hours=1))
        self.stub_client("mariadb-dump", "printf '%s' 'incomplete synthetic SQL'\nexit 1\n")
        with self.assertRaises(backup.BackupError):
            self.perform()
        self.assertEqual(len(list(self.directory.glob("*.sql.gz"))), 2)
        self.assertEqual(len(list(self.directory.glob("*.partial"))), 1)
        for path in (first, second):
            backup.verify_backup_checksum(path)
        self.assertNotIn('"event": "retention_plan"', self.output.getvalue())

    def test_empty_successful_dump_is_rejected_before_publication(self):
        self.stub_client("mariadb-dump", "exit 0\n")
        with self.assertRaisesMessage(backup.BackupError, "empty"):
            self.perform()
        self.assertEqual(list(self.directory.glob("*.sql.gz")), [])

    def test_gzip_t_failure_is_not_published(self):
        original_run = subprocess.run

        def fail_gzip(args, **kwargs):
            if args[:2] == ["gzip", "-t"]:
                return subprocess.CompletedProcess(args, 1)
            return original_run(args, **kwargs)

        with patch.object(backup.subprocess, "run", side_effect=fail_gzip):
            with self.assertRaisesMessage(backup.BackupError, "gzip -t failed"):
                self.perform()
        self.assertEqual(list(self.directory.glob("*.sql.gz")), [])

    def test_checksum_generation_failure_is_not_published(self):
        with patch.object(backup.hashlib, "file_digest", side_effect=OSError("synthetic failure")):
            with self.assertRaises(backup.BackupError):
                self.perform()
        self.assertEqual(list(self.directory.glob("*.sql.gz")), [])

    def test_publication_is_atomic_no_clobber_and_dump_appears_last(self):
        original_link = os.link
        destinations = []

        def observe_link(source, destination):
            if str(destination).endswith(".sha256"):
                self.assertFalse(Path(str(destination)[:-7]).exists())
            else:
                self.assertTrue(Path(str(destination) + ".sha256").exists())
                self.assertGreater(backup.verify_gzip(source), 0)
            self.assertFalse(Path(destination).exists())
            destinations.append(Path(destination))
            return original_link(source, destination)

        with patch.object(backup.os, "link", side_effect=observe_link):
            point = self.perform()
        self.assertEqual(destinations, [Path(str(point.path) + ".sha256"), point.path])

    def test_failed_final_checksum_unpublishes_only_this_attempt(self):
        prior = self.point(self.now - timedelta(hours=1))
        original = backup.verify_backup_checksum

        def fail_final(path, **kwargs):
            if Path(path).name.startswith("db-20261115T080000Z-"):
                raise backup.BackupError("synthetic final check failure")
            return original(path, **kwargs)

        with patch.object(backup, "verify_backup_checksum", side_effect=fail_final):
            with self.assertRaises(backup.BackupError):
                self.perform()
        self.assertEqual(list(self.directory.glob("*.sql.gz")), [prior])
        original(prior)

    def test_credentials_are_private_escaped_and_not_in_child_arguments_or_env(self):
        secret = 'test-only-\\password"with\nnewline'
        config = replace(self.config, password=secret)
        with backup.client_options(config) as options:
            self.assertEqual(options.stat().st_mode & 0o777, 0o600)
            self.assertIn('password="test-only-\\\\password\\"with\\nnewline"', options.read_text())
            client_path = options
        self.assertFalse(client_path.exists())
        calls = []
        original_popen = subprocess.Popen

        def capture(args, **kwargs):
            calls.append((args, kwargs.get("env", {})))
            return original_popen(args, **kwargs)

        with patch.object(backup.subprocess, "Popen", side_effect=capture):
            self.perform()
        for args, env in calls:
            self.assertNotIn(self.config.password, " ".join(args))
            self.assertNotIn("DATABASE_PASSWORD", env)
            self.assertNotIn("ZIBAL_MERCHANT", env)
        self.assertNotIn(self.config.password, self.output.getvalue())

    def test_directory_lock_blocks_backup_and_retention(self):
        with backup.directory_lock(self.directory):
            with self.assertRaises(backup.BackupBusy):
                self.perform()
            with self.assertRaises(backup.BackupBusy):
                backup.run_retention(self.config)
        self.assertTrue((self.directory / ".db-backup.lock").exists())

    def test_read_only_recovery_lock_blocks_backup_and_retention(self):
        point = self.point()
        with backup.directory_lock(self.directory):
            pass
        with backup.protect_recovery_backup(point):
            with self.assertRaises(backup.BackupBusy):
                self.perform()
            with self.assertRaises(backup.BackupBusy):
                backup.run_retention(self.config)
        self.assertEqual(backup.verify_backup_checksum(point), self.checksum(point))

    def test_recovery_refuses_managed_point_without_lock_or_with_active_writer(self):
        point = self.point()
        with self.assertRaises(backup.BackupError):
            with backup.protect_recovery_backup(point):
                pass
        with backup.directory_lock(self.directory):
            with self.assertRaises(backup.BackupBusy):
                with backup.protect_recovery_backup(point):
                    pass

    def test_lock_also_blocks_a_separate_process(self):
        script = (
            "from pathlib import Path; from scripts.db_backup import directory_lock; import sys; "
            "lock = directory_lock(Path(sys.argv[1])); lock.__enter__(); "
            "print('held', flush=True); sys.stdin.read(); lock.__exit__(None, None, None)"
        )
        with subprocess.Popen(
            [sys.executable, "-c", script, str(self.directory)], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as holder:
            try:
                self.assertEqual(holder.stdout.readline().strip(), "held")
                with self.assertRaises(backup.BackupBusy):
                    self.perform()
                with self.assertRaises(backup.BackupBusy):
                    backup.run_retention(self.config, dry_run=True)
            finally:
                holder.communicate(input="", timeout=10)

    def test_recent_48_and_seven_daily_points_have_correct_overlap(self):
        recent = [self.point(self.now - timedelta(minutes=30 * index)) for index in range(48)]
        for day in range(2, 10):
            for hour in (1, 23):
                self.point((self.now - timedelta(days=day)).replace(hour=hour))
        plan = backup.run_retention(self.config, dry_run=True, now=self.now)
        self.assertEqual(plan["recent_count"], 48)
        self.assertEqual(plan["daily_count"], 7)
        self.assertEqual(plan["daily_overlap_count"], 1)
        self.assertEqual(plan["retained_count"], 54)
        self.assertTrue({path.name for path in recent} <= set(plan["retained"]))
        backup.run_retention(self.config, now=self.now)
        points, _ = backup.scan_recovery_points(self.directory)
        self.assertEqual(len(points), 54)
        for name in plan["delete"]:
            self.assertFalse((self.directory / name).exists())
            self.assertFalse((self.directory / (name + ".sha256")).exists())

    def test_retention_never_deletes_newest_or_previous_valid_point(self):
        points = [self.point(self.now - timedelta(hours=index)) for index in range(5)]
        config = replace(self.config, recent=1, daily_days=0)
        backup.run_retention(config, now=self.now)
        self.assertTrue(points[0].exists())
        self.assertTrue(points[1].exists())
        self.assertEqual(len(list(self.directory.glob("*.sql.gz"))), 2)

    def test_same_second_random_suffix_cannot_delete_the_newest_backup(self):
        self.config = replace(self.config, recent=1, daily_days=0)
        with patch.object(backup.secrets, "token_hex", side_effect=["ffffffffffff", "eeeeeeeeeeee", "000000000001"]):
            points = [self.perform() for _ in range(3)]
        self.assertFalse(points[0].path.exists())
        self.assertTrue(points[1].path.exists())
        self.assertTrue(points[2].path.exists())

    def test_retention_preserves_historical_and_unrelated_files(self):
        historical = self.directory / "db-20261114-190647.sql.gz"
        historical.write_bytes(b"historical recovery evidence")
        old_sidecar = Path(str(historical) + ".sha256")
        old_sidecar.write_bytes(b"historical sidecar")
        unrelated = self.directory / "private-incident-report.json"
        unrelated.write_bytes(b"unrelated synthetic data")
        malformed_name = self.point(name="db-20261115-080000-oldname.sql.gz")
        for index in range(5):
            self.point(self.now - timedelta(hours=index))
        backup.run_retention(replace(self.config, recent=1, daily_days=0), now=self.now)
        self.assertEqual(historical.read_bytes(), b"historical recovery evidence")
        self.assertEqual(old_sidecar.read_bytes(), b"historical sidecar")
        self.assertEqual(unrelated.read_bytes(), b"unrelated synthetic data")
        self.assertTrue(malformed_name.exists())

    def test_invalid_sidecars_are_preserved_and_excluded_from_retention_counts(self):
        invalid = self.point(self.now - timedelta(days=10), checksum=False)
        corrupt = self.point(self.now - timedelta(days=9))
        Path(str(corrupt) + ".sha256").write_text("invalid")
        valid = [self.point(self.now - timedelta(hours=index)) for index in range(3)]
        plan = backup.run_retention(replace(self.config, recent=1, daily_days=0), now=self.now)
        self.assertEqual(plan["retained_count"], 2)
        self.assertEqual(set(plan["invalid_preserved"]), {invalid.name, corrupt.name})
        self.assertTrue(invalid.exists())
        self.assertTrue(corrupt.exists())
        self.assertTrue(all(path.exists() for path in valid[:2]))

    def test_failed_new_backup_cannot_trigger_retention_of_old_points(self):
        points = [self.point(self.now - timedelta(hours=index)) for index in range(6)]
        self.config = replace(self.config, recent=1, daily_days=0)
        self.stub_client("mariadb-dump", "exit 1\n")
        with self.assertRaises(backup.BackupError):
            self.perform()
        self.assertTrue(all(path.exists() for path in points))

    def test_abandoned_partial_cleanup_recovers_space_even_when_next_dump_fails(self):
        prior = self.point(self.now - timedelta(hours=1))
        partial = self.directory / ".db-20261114T080000Z-abcdef123456.sql.gz.old123.partial"
        partial.write_bytes(b"x" * 4000)
        stamp = (self.now - timedelta(hours=25)).timestamp()
        os.utime(partial, (stamp, stamp))
        self.stub_client("mariadb-dump", "exit 1\n")
        with self.assertRaises(backup.BackupError):
            self.perform()
        self.assertFalse(partial.exists())
        self.assertTrue(prior.exists())
        backup.verify_backup_checksum(prior)

    def test_stale_partials_only_exact_owned_names_older_than_24_hours(self):
        old = self.directory / ".db-20261114T080000Z-abcdef123456.sql.gz.old123.partial"
        fresh = self.directory / ".db-20261115T080000Z-abcdef123456.sql.gz.new123.partial"
        unrelated = self.directory / "unrelated.partial"
        legacy = self.directory / "db-20261114-190647-OLD.sql.gz.partial"
        for path in (old, fresh, unrelated, legacy):
            path.write_bytes(b"partial")
        old_stamp = (self.now - timedelta(hours=25)).timestamp()
        for path in (old, unrelated, legacy):
            os.utime(path, (old_stamp, old_stamp))
        fresh_stamp = (self.now - timedelta(hours=23)).timestamp()
        os.utime(fresh, (fresh_stamp, fresh_stamp))
        plan = backup.run_retention(self.config, dry_run=True, now=self.now)
        self.assertEqual(plan["stale_partials"], [old.name])
        self.assertTrue(old.exists())
        backup.run_retention(self.config, now=self.now)
        self.assertFalse(old.exists())
        self.assertTrue(all(path.exists() for path in (fresh, unrelated, legacy)))

    def test_active_lock_prevents_stale_partial_cleanup(self):
        partial = self.directory / ".db-20261114T080000Z-abcdef123456.sql.gz.old123.partial"
        partial.write_bytes(b"active test data")
        os.utime(partial, (1, 1))
        with backup.directory_lock(self.directory):
            with self.assertRaises(backup.BackupBusy):
                backup.run_retention(self.config, now=self.now)
        self.assertTrue(partial.exists())

    def test_retention_dry_run_reports_capacity_and_deletes_nothing(self):
        for index in range(6):
            self.point(self.now - timedelta(hours=index))
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        plan = backup.run_retention(replace(self.config, recent=1, daily_days=0), dry_run=True, now=self.now)
        self.assertGreater(plan["bytes_used"], 0)
        self.assertGreater(plan["estimated_bytes_freed"], 0)
        self.assertGreater(plan["available_bytes"], 0)
        self.assertEqual(plan["delete_count"], 4)
        self.assertTrue(all((self.directory / name).read_bytes() == value for name, value in before.items()))

    def test_safe_default_retention_is_preview_until_reviewed(self):
        config = backup.Config.from_env({"BACKUP_DIRECTORY": str(self.directory), "BACKUP_DB_USER": "test_client", "BACKUP_DB_PASSWORD": self.config.password})
        self.assertEqual(config.retention_mode, "dry-run")
        self.assertEqual(config.recent, 48)
        self.assertEqual(config.daily_days, 7)
        self.assertIsNone(config.max_total_bytes)
        self.assertIsNone(config.min_free_bytes)

    def test_invalid_capacity_and_retention_configuration_is_rejected(self):
        for key, value in (("BACKUP_RETENTION_RECENT", "0"), ("BACKUP_RETENTION_DAILY_DAYS", "-1"),
                           ("BACKUP_MAX_TOTAL_BYTES", "wrong"), ("BACKUP_MIN_FREE_BYTES", "-1"),
                           ("BACKUP_RETENTION_MODE", "automatic"), ("DATABASE_PORT", "65536")):
            with self.subTest(key=key):
                with self.assertRaises(backup.BackupError):
                    backup.Config.from_env({"BACKUP_DB_USER": "test_client", "BACKUP_DB_PASSWORD": self.config.password, key: value})

    def test_insufficient_disk_space_fails_without_deleting_points(self):
        prior = self.point(self.now - timedelta(hours=1))
        disk = shutil._ntuple_diskusage(10000, 9500, 500)
        with patch.object(backup.shutil, "disk_usage", return_value=disk):
            with self.assertRaisesMessage(backup.BackupError, "Insufficient free disk space"):
                self.perform()
        self.assertTrue(prior.exists())
        self.assertEqual(list(self.directory.glob("*.partial")), [])

    def test_total_capacity_includes_historical_files_and_fails_safely(self):
        historical = self.directory / "db-20261114-190647.sql.gz"
        historical.write_bytes(b"x" * 2000)
        self.config = replace(self.config, max_total_bytes=3000, min_free_bytes=0)
        with self.assertRaisesMessage(backup.BackupError, "capacity limit"):
            self.perform()
        self.assertEqual(historical.stat().st_size, 2000)
        self.assertEqual(list(self.directory.glob("*.partial")), [])

    def test_capacity_is_checked_again_after_fast_dump_completion(self):
        original_dump = backup._dump_to_gzip
        disk = shutil._ntuple_diskusage(10000, 9500, 500)

        def shrinking_space(*args, **kwargs):
            with patch.object(backup.shutil, "disk_usage", return_value=disk):
                return original_dump(*args, **kwargs)

        with patch.object(backup, "_dump_to_gzip", side_effect=shrinking_space):
            with self.assertRaisesMessage(backup.BackupError, "reserve reached"):
                self.perform()
        self.assertEqual(list(self.directory.glob("*.sql.gz")), [])

    def test_capacity_limit_includes_the_completed_checksum_pair(self):
        previous = self.point(self.now - timedelta(hours=1))
        self.stub_client("mariadb", "printf 'InnoDB\\t3\\t1\\n'\n")
        # The observed SQL estimate and compressed dump fit this headroom, but
        # publishing the checksum pair would cross the configured ceiling.
        config = replace(self.config, max_total_bytes=backup.storage_bytes(self.directory) + len(self.sql) + 20)
        with self.assertRaisesMessage(backup.BackupError, "checksum sidecar"):
            backup.perform_backup(config, now=self.now)
        self.assertEqual(list(self.directory.glob("*.sql.gz")), [previous])
        backup.verify_backup_checksum(previous)
        self.assertNotIn('"event": "retention_plan"', self.output.getvalue())

    def test_mariadb_unavailable_preserves_existing_recovery_points_and_redacts_errors(self):
        prior = self.point()
        self.stub_client("mariadb", "printf '%s' 'test_only_private_value SQL customer data' >&2\nexit 1\n")
        with self.assertRaises(backup.BackupError) as context:
            self.perform()
        self.assertNotIn("test_only_private_value", str(context.exception))
        self.assertNotIn("customer data", self.output.getvalue())
        self.assertTrue(prior.exists())

    def test_non_transactional_engines_are_refused_by_default(self):
        self.stub_client("mariadb", "printf 'Aria\\t1\\t1000\\nInnoDB\\t2\\t2000\\n'\n")
        with self.assertRaisesMessage(backup.BackupError, "Non-InnoDB"):
            self.perform()
        self.assertEqual(list(self.directory.glob("*.sql.gz")), [])

    def test_explicit_non_transactional_policy_uses_lock_all_tables(self):
        self.stub_client("mariadb", "printf 'MyISAM\\t1\\t1000\\n'\n")
        self.config = replace(self.config, non_transactional_policy="lock-all")
        original = subprocess.Popen
        commands = []

        def capture(args, **kwargs):
            commands.append(args)
            return original(args, **kwargs)

        with patch.object(backup.subprocess, "Popen", side_effect=capture):
            self.perform()
        dump = next(args for args in commands if args[0] == "mariadb-dump")
        self.assertIn("--lock-all-tables", dump)
        self.assertNotIn("--single-transaction", dump)
        self.assertTrue({"--quick", "--routines", "--events", "--triggers"} <= set(dump))

    def test_manual_worker_cli_uses_same_implementation(self):
        env = {
            **os.environ, "BACKUP_DIRECTORY": str(self.directory), "DATABASE_NAME": "synthetic_test",
            "BACKUP_DB_USER": "test_client", "BACKUP_DB_PASSWORD": self.config.password,
            "BACKUP_RETENTION_MODE": "apply", "BACKUP_MIN_FREE_BYTES": "0",
        }
        result = subprocess.run([sys.executable, str(ROOT / "scripts/db_backup.py"), "backup"],
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout)
        paths = list(self.directory.glob("*.sql.gz"))
        self.assertEqual(len(paths), 1)
        backup.verify_backup_checksum(paths[0])
        self.assertNotIn(self.config.password, result.stdout + result.stderr)

    def test_shutdown_interrupts_dump_without_publishing_or_cleaning(self):
        from threading import Event
        stopped = Event()
        stopped.set()
        self.stub_client("mariadb-dump", "sleep 10\nprintf 'synthetic SQL'\n")
        with self.assertRaisesMessage(backup.BackupError, "interrupted"):
            self.perform(stop_event=stopped)
        self.assertEqual(list(self.directory.glob("*.sql.gz")), [])

    def capacity_fixture(self):
        for index in range(48):
            self.point(self.now - timedelta(minutes=30 * index))
        for day in range(2, 8):
            self.point((self.now - timedelta(days=day)).replace(hour=23, minute=30))
        old = self.point(self.now - timedelta(days=15), contents=os.urandom(16000))
        corrupt = self.point(self.now - timedelta(days=16), checksum=False)
        historical = self.directory / "db-20261114-190647.sql.gz"
        historical.write_bytes(b"historical synthetic fixture")
        plan = backup.make_retention_plan(self.config, now=self.now)
        protected = [self.directory / name for name in plan["retained"]]
        used = backup.storage_bytes(self.directory)
        old_pair_bytes = old.stat().st_size + Path(str(old) + ".sha256").stat().st_size
        self.config = replace(self.config, min_free_bytes=0, max_total_bytes=used - old_pair_bytes + 2100)
        return old, protected, corrupt, historical

    def test_sufficient_capacity_does_not_clean_retention_before_dump(self):
        self.config = replace(self.config, recent=2, daily_days=0)
        old = self.point(self.now - timedelta(days=15))
        self.point(self.now - timedelta(hours=1))
        self.point(self.now - timedelta(hours=2))
        original = backup._dump_to_gzip
        def dump(*args):
            self.assertTrue(old.exists())
            self.assertNotIn('"reason": "capacity_pressure"', self.output.getvalue())
            return original(*args)
        with patch.object(backup, "_dump_to_gzip", side_effect=dump):
            self.perform()
        self.assertFalse(old.exists())  # Ordinary post-success retention remains active.

    def test_capacity_cleanup_rechecks_limits_and_preserves_full_retention_set(self):
        old, protected, corrupt, historical = self.capacity_fixture()
        original = backup._dump_to_gzip
        def dump(*args):
            self.assertFalse(old.exists())
            self.assertFalse(Path(str(old) + ".sha256").exists())
            self.assertTrue(all(path.exists() for path in protected))
            return original(*args)
        with patch.object(backup, "capacity_check", wraps=backup.capacity_check) as capacity, patch.object(
            backup, "_dump_to_gzip", side_effect=dump,
        ):
            result = self.perform()
        self.assertEqual(capacity.call_count, 2)
        self.assertTrue(result.path.exists())
        self.assertTrue(corrupt.exists())
        self.assertTrue(historical.exists())
        # New success may rotate the old recent boundary; daily and latest two stay protected.
        points, invalid = backup.scan_recovery_points(self.directory)
        self.assertGreaterEqual(len(points), 48)
        self.assertIn(corrupt.name, invalid)
        daily = {p.timestamp.date() for p in points}
        self.assertTrue(all((self.now - timedelta(days=day)).date() in daily for day in range(1, 8)))

    def test_capacity_dry_run_logs_reclaimed_bytes_without_deletion(self):
        old, protected, corrupt, historical = self.capacity_fixture()
        self.config = replace(self.config, retention_mode="dry-run")
        with patch.object(backup, "_dump_to_gzip") as dump:
            with self.assertRaises(backup.BackupCapacityError):
                self.perform()
        dump.assert_not_called()
        self.assertTrue(all(path.exists() for path in [old, *protected, corrupt, historical]))
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        plan = next(e for e in events if e["event"] == "retention_plan")
        self.assertEqual(plan["mode"], "dry-run")
        self.assertGreater(plan["estimated_bytes_freed"], 0)

    def test_capacity_failure_after_cleanup_never_sacrifices_protected_points(self):
        old, protected, corrupt, historical = self.capacity_fixture()
        self.config = replace(self.config, max_total_bytes=100)
        with patch.object(backup, "_dump_to_gzip") as dump:
            with self.assertRaisesMessage(backup.BackupCapacityError, "remains insufficient"):
                self.perform()
        dump.assert_not_called()
        self.assertFalse(old.exists())
        self.assertTrue(all(path.exists() for path in [*protected, corrupt, historical]))
        for path in protected:
            backup.verify_backup_integrity(path)

    def test_failed_dump_after_capacity_cleanup_preserves_every_protected_pair(self):
        old, protected, corrupt, historical = self.capacity_fixture()
        self.stub_client("mariadb-dump", "exit 2\n")
        with self.assertRaises(backup.BackupError):
            self.perform()
        self.assertFalse(old.exists())
        self.assertTrue(all(path.exists() for path in [*protected, corrupt, historical]))
        for path in protected:
            backup.verify_backup_integrity(path)
        self.assertNotIn('"event": "backup_completed"', self.output.getvalue())

    def test_capacity_cleanup_can_recover_required_free_space(self):
        old, protected, _, _ = self.capacity_fixture()
        self.config = replace(self.config, max_total_bytes=None, min_free_bytes=1000)
        def disk_usage(directory):
            return shutil._ntuple_diskusage(100000, 99500, 500) if old.exists() else shutil._ntuple_diskusage(100000, 50000, 50000)
        with patch.object(backup.shutil, "disk_usage", side_effect=disk_usage):
            self.assertTrue(self.perform().path.exists())
        self.assertFalse(old.exists())

    def test_combined_integrity_scan_reads_original_compressed_bytes_once(self):
        contents = os.urandom(2 * 1024 * 1024)
        path = self.point(contents=contents)
        count = 0
        original = backup._HashingReader.read
        def read(reader, size=-1):
            nonlocal count
            data = original(reader, size)
            count += len(data)
            return data
        with patch.object(backup._HashingReader, "read", new=read):
            digest, size = backup.verify_backup_integrity(path)
        self.assertEqual(size, len(contents))
        self.assertEqual(count, path.stat().st_size)
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_combined_integrity_rejects_corruption_truncation_and_bad_sidecars(self):
        for failure in ("corrupt", "truncate", "missing", "mismatch", "malformed"):
            with self.subTest(failure=failure):
                path = self.point(contents=os.urandom(8192))
                sidecar = Path(str(path) + ".sha256")
                if failure == "corrupt":
                    data = bytearray(path.read_bytes()); data[30] ^= 1
                    path.write_bytes(data); self.checksum(path)
                elif failure == "truncate":
                    path.write_bytes(path.read_bytes()[:-5]); self.checksum(path)
                elif failure == "missing":
                    sidecar.unlink()
                elif failure == "mismatch":
                    sidecar.write_text("0" * 64 + "  " + path.name + "\n")
                else:
                    sidecar.write_text("invalid sidecar")
                with self.assertRaises(backup.BackupError):
                    backup.verify_backup_integrity(path)

    def test_combined_integrity_rejects_backup_and_sidecar_symlinks(self):
        source = self.point()
        target = self.directory / "foreign.sql.gz"
        target.symlink_to(source)
        with self.assertRaises(backup.BackupError):
            backup.verify_backup_integrity(target)
        sidecar = Path(str(source) + ".sha256")
        original = self.directory / "original-sidecar"
        sidecar.rename(original); sidecar.symlink_to(original)
        with self.assertRaises(backup.BackupError):
            backup.verify_backup_integrity(source)

    def test_combined_integrity_detects_file_changes_during_stream(self):
        for change in ("backup", "sidecar"):
            with self.subTest(change=change):
                path = self.point(contents=os.urandom(65536))
                original = backup._HashingReader.read
                changed = False
                def read(reader, size=-1):
                    nonlocal changed
                    data = original(reader, size)
                    if data and not changed:
                        changed = True
                        selected = path if change == "backup" else Path(str(path) + ".sha256")
                        with selected.open("ab") as stream:
                            stream.write(b"x")
                    return data
                with patch.object(backup._HashingReader, "read", new=read):
                    with self.assertRaises(backup.BackupError):
                        backup.verify_backup_integrity(path)

    def test_worker_requires_dedicated_credentials_without_root_fallback(self):
        for env in ({}, {"DATABASE_USER": "root", "DATABASE_PASSWORD": "synthetic-root-only"},
                    {"BACKUP_DB_USER": "root", "BACKUP_DB_PASSWORD": "synthetic-backup-only"},
                    {"BACKUP_DB_USER": "backup_user", "BACKUP_DB_PASSWORD": "REPLACE_EXAMPLE"}):
            with self.assertRaises(backup.BackupError):
                backup.Config.from_env(env)
        config = backup.Config.from_env({"BACKUP_DB_USER": "backup_user", "BACKUP_DB_PASSWORD": "synthetic-backup-only",
                                         "DATABASE_USER": "root", "DATABASE_PASSWORD": "ignored-root-value"})
        self.assertEqual(config.user, "backup_user")



class BackupSchedulerTests(SimpleTestCase):
    def setUp(self):
        self.temporary = Path(self.enterContext(TemporaryDirectory(prefix="minikandoo-scheduler-test-")))
        self.config = backup.Config(self.temporary)
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.enterContext(patch.object(backup, "Thread"))
        self.enterContext(patch.object(backup, "_write_heartbeat"))

    def run_schedule(self, *, start, failure=None, count=3):
        current = [start]
        calls = []

        class Stop:
            stopped = False

            def is_set(self):
                return self.stopped

            def set(self):
                self.stopped = True

            def wait(self, seconds):
                current[0] += timedelta(seconds=seconds)
                return self.stopped

        stop = Stop()

        def attempt(config, *, stop_event):
            calls.append(current[0])
            if len(calls) >= count:
                stop_event.set()
            if failure and len(calls) == 1:
                raise failure

        backup.schedule(self.config, stop_event=stop, backup_function=attempt, clock=lambda: current[0])
        return calls

    def test_initial_backup_then_exact_utc_half_hour_boundaries(self):
        calls = self.run_schedule(start=datetime(2026, 11, 15, 8, 7, tzinfo=timezone.utc))
        self.assertEqual([(value.hour, value.minute) for value in calls], [(8, 7), (8, 30), (9, 0)])

    def test_failure_retries_at_next_schedule_without_permanent_exit(self):
        calls = self.run_schedule(start=datetime(2026, 11, 15, 8, 7, tzinfo=timezone.utc),
                                  failure=backup.BackupError("MariaDB unavailable"))
        self.assertEqual(len(calls), 3)
        self.assertIn('"event": "backup_failed"', self.output.getvalue())
        self.assertIn('"last_result": "success"', self.output.getvalue())

    def test_unexpected_failure_is_redacted_and_does_not_stop_future_attempts(self):
        calls = self.run_schedule(start=datetime(2026, 11, 15, 8, 7, tzinfo=timezone.utc),
                                  failure=RuntimeError("synthetic-private-password"))
        self.assertEqual(len(calls), 3)
        self.assertNotIn("synthetic-private-password", self.output.getvalue())

    def test_restart_performs_a_new_initial_backup(self):
        first = self.run_schedule(start=datetime(2026, 11, 15, 8, 7, tzinfo=timezone.utc), count=1)
        second = self.run_schedule(start=datetime(2026, 11, 15, 8, 20, tzinfo=timezone.utc), count=1)
        self.assertEqual(first[0].minute, 7)
        self.assertEqual(second[0].minute, 20)

    def test_missed_boundaries_are_skipped_after_long_attempt(self):
        self.assertEqual(backup.next_half_hour(datetime(2026, 11, 15, 9, 43, tzinfo=timezone.utc)).minute, 0)

    def test_healthcheck_requires_recent_success_and_live_scheduler(self):
        now = backup.utc_now()
        path = self.temporary / "heartbeat.json"
        status = {"pid": os.getpid(), "state": "waiting", "last_result": "success",
                  "heartbeat_at": now.isoformat(), "last_success_at": now.isoformat()}
        with patch.object(backup, "HEARTBEAT_PATH", path):
            self.assertEqual(backup.healthcheck(), 1)
            path.write_text(json.dumps(status))
            self.assertEqual(backup.healthcheck(), 0)
            for changes in ({"last_result": "failed"}, {"state": "stopped"},
                            {"heartbeat_at": (now - timedelta(minutes=2)).isoformat()},
                            {"last_success_at": (now - timedelta(hours=2)).isoformat()}):
                path.write_text(json.dumps({**status, **changes}))
                self.assertEqual(backup.healthcheck(), 1)


@skipUnless(shutil.which("docker"), "Docker Compose CLI is required for configuration validation")
class BackupComposeTests(SimpleTestCase):
    def setUp(self):
        self.temporary = Path(self.enterContext(TemporaryDirectory(prefix="minikandoo-compose-unit-")))
        self.environment_file = self.temporary / "test-only.env"
        self.environment_file.write_text("BACKUP_HOST_DIR=" + str(self.temporary / "backups") + "\n")
        self.env = {
            **os.environ, "ENV_FILE": str(self.environment_file), "COMPOSE_PROJECT_NAME": "minikandoo-config-test",
            "SECRET_KEY": "configuration-only-test-key-never-production-" + "x" * 50,
            "DATABASE_PASSWORD": "configuration-test-only", "MARIADB_ROOT_PASSWORD": "configuration-root-test-only",
            "BACKUP_DB_USER": "test_backup", "BACKUP_DB_PASSWORD": "configuration-backup-test-only",
            "ZIBAL_MERCHANT": "configuration-test-only", "BACKUP_HOST_DIR": str(self.temporary / "backups"),
        }

    def compose(self, *args):
        return subprocess.run([str(ROOT / "scripts/compose-production"), *args], cwd=self.temporary,
                              env=self.env, capture_output=True, text=True, timeout=30)

    def test_production_and_bootstrap_configuration_are_valid(self):
        for args in (("config", "--quiet"), ("--bootstrap", "config", "--quiet")):
            result = self.compose(*args)
            self.assertEqual(result.returncode, 0, "Compose syntax validation failed")

    def test_backup_service_has_internal_health_dependency_persistent_mount_and_security(self):
        result = self.compose("config", "--format", "json")
        self.assertEqual(result.returncode, 0, "Compose configuration failed")
        config = json.loads(result.stdout)
        service = config["services"]["db-backup"]
        self.assertEqual(service["depends_on"]["db"]["condition"], "service_healthy")
        self.assertEqual(service["restart"], "unless-stopped")
        self.assertTrue(service["read_only"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertIn("/var/lib/mysql:ro,noexec,nosuid,nodev", service["tmpfs"])
        self.assertFalse(service.get("privileged", False))
        self.assertFalse(service.get("ports"))
        self.assertFalse(service.get("profiles"))
        self.assertEqual(set(service["networks"]), {"shop-net"})
        self.assertNotIn("web", service["depends_on"])
        self.assertEqual(service["environment"]["DATABASE_HOST"], "db")
        self.assertNotIn("ZIBAL_MERCHANT", service["environment"])
        self.assertNotIn("SECRET_KEY", service["environment"])
        mount = service["volumes"][0]
        self.assertEqual((mount["type"], mount["target"]), ("bind", "/backups"))
        self.assertEqual(mount["source"], str(self.temporary / "backups"))
        self.assertFalse(mount["bind"]["create_host_path"])
        self.assertTrue(all("docker.sock" not in item.get("source", "") for item in service["volumes"]))
        self.assertNotIn("db-backup", config["services"]["web"]["depends_on"])

    def test_worker_does_not_receive_root_credentials_and_provisioning_is_profile_scoped(self):
        result = self.compose("--profile", "backup-admin", "config", "--format", "json")
        self.assertEqual(result.returncode, 0, "Provisioning Compose configuration failed")
        services = json.loads(result.stdout)["services"]
        worker = services["db-backup"]
        self.assertEqual(worker["environment"]["BACKUP_DB_USER"], "test_backup")
        self.assertNotIn("MARIADB_ROOT_PASSWORD", worker["environment"])
        self.assertNotIn("DATABASE_PASSWORD", worker["environment"])
        provision = services["db-backup-provision"]
        self.assertEqual(provision["profiles"], ["backup-admin"])
        self.assertEqual(provision["restart"], "no")
        self.assertEqual(provision["depends_on"]["db"]["condition"], "service_healthy")
        self.assertFalse(provision.get("ports"))
        self.assertFalse(provision.get("volumes"))

    def test_manual_wrapper_uses_same_compose_project_and_worker(self):
        bin_directory = self.temporary / "bin"
        bin_directory.mkdir()
        stub = bin_directory / "docker"
        stub.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
        stub.chmod(0o700)
        env = {**self.env, "PATH": str(bin_directory) + os.pathsep + os.environ["PATH"]}
        result = subprocess.run([str(ROOT / "scripts/backup-production-db")], cwd=self.temporary,
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        args = result.stdout.splitlines()
        self.assertEqual(args[-6:], ["run", "--rm", "--no-deps", "-T", "db-backup", "backup"])
        self.assertIn("docker-compose.production.yml", args)
        self.assertNotIn("docker-compose.override.yml", args)
        self.assertIn(str(self.environment_file), args)
        rejected = subprocess.run([str(ROOT / "scripts/backup-production-db"), "/different/directory"],
                                  env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(rejected.returncode, 2)


class BackupProvisionTests(SimpleTestCase):
    def setUp(self):
        from scripts import db_backup_provision
        self.provision = db_backup_provision
        self.env = {"DATABASE_NAME": "synthetic_database", "BACKUP_DB_USER": "backup_test",
                    "BACKUP_DB_PASSWORD": "synthetic-private-'\\-value", "MARIADB_ROOT_PASSWORD": "synthetic-root-only",
                    "APPLICATION_DB_USER": "application_test"}

    def test_provisioning_is_explicit_scoped_and_password_rotation_is_optional(self):
        output = io.StringIO()
        with patch.object(self.provision, "execute", side_effect=[b"0\t0", b"", b"0\t0", b""]) as execute, redirect_stdout(output):
            self.provision.provision(env=self.env)
            self.provision.provision(env=self.env, rotate_password=True, allow_lock_all=True)
        first, rotated = execute.call_args_list[1].args[1], execute.call_args_list[3].args[1]
        self.assertIn("CREATE USER IF NOT EXISTS", first)
        self.assertNotIn("ALTER USER", first)
        self.assertIn("ALTER USER", rotated)
        self.assertIn("GRANT SELECT, SHOW VIEW, TRIGGER, EVENT", first)
        self.assertIn("GRANT SELECT ON mysql.proc", first)
        self.assertNotIn("GRANT RELOAD", first)
        self.assertIn("GRANT RELOAD ON *.*", rotated)
        self.assertNotIn("GRANT ALL", first)
        self.assertNotIn(self.env["BACKUP_DB_PASSWORD"], output.getvalue())
        self.assertNotIn(self.env["MARIADB_ROOT_PASSWORD"], output.getvalue())

    def test_invalid_or_shared_account_fails_before_sql(self):
        for user in ("root", "bad'username", "application_test"):
            with patch.object(self.provision, "execute") as execute:
                with self.assertRaises(backup.BackupError):
                    self.provision.provision(env={**self.env, "BACKUP_DB_USER": user})
                execute.assert_not_called()

    def test_existing_roles_or_host_accounts_fail_closed(self):
        with patch.object(self.provision, "execute", return_value=b"1\t0") as execute:
            with self.assertRaises(backup.BackupError):
                self.provision.provision(env=self.env)
        self.assertEqual(execute.call_count, 1)

    def test_provisioning_sql_escaping_preserves_literal_password(self):
        value = "quote'and\\backslash"
        self.assertEqual(self.provision.sql_string(value), "'quote''and\\backslash'")
        with self.assertRaises(backup.BackupError):
            self.provision.sql_string("invalid\x00value")

    def test_provisioning_grants_only_the_literal_database_name(self):
        self.assertEqual(self.provision.sql_database_pattern("shop_data%`\\"), "`shop\\_data\\%``\\\\`")
        with patch.object(self.provision, "execute", side_effect=[b"0\t0", b""]) as execute, redirect_stdout(io.StringIO()):
            self.provision.provision(env=self.env)
        self.assertIn("ON `synthetic\\_database`.*", execute.call_args_list[1].args[1])
        for value in ("", "invalid\x00name", "invalid\nname"):
            with self.assertRaises(backup.BackupError):
                self.provision.sql_database_pattern(value)
