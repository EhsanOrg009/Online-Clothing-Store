"""Entrypoint rejects an incompatible schema before launching web processes."""

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[1]


class EntrypointSchemaTests(SimpleTestCase):
    def run_entrypoint(self, *, check_result=0, migrate_on_start="0", migrate_result=0):
        with TemporaryDirectory(prefix="minikandoo-startup-test-") as name:
            directory = Path(name)
            entrypoint = directory / "entrypoint.sh"
            source = (ROOT / "docker/entrypoint.sh").read_text().replace("cd /app\n", 'cd "$TEST_APP_DIRECTORY"\n')
            entrypoint.write_text(source)
            python = directory / "python"
            python.write_text(
                '#!/bin/sh\nif [ "$#" = 0 ]; then cat >/dev/null; exit 0; fi\n'
                'printf "%s\\n" "$*" >> "$TEST_APP_DIRECTORY/commands"\n'
                'if [ "$*" = "manage.py migrate --check --noinput" ]; then exit "$TEST_CHECK_RESULT"; fi\n'
                'if [ "$*" = "manage.py migrate --noinput" ]; then exit "$TEST_MIGRATE_RESULT"; fi\n'
                'exit 0\n'
            )
            python.chmod(0o700)
            launch = directory / "launch"
            launch.write_text('#!/bin/sh\nprintf "launched\\n"\n')
            launch.chmod(0o700)
            env = {**os.environ, "PATH": str(directory) + os.pathsep + os.environ.get("PATH", os.defpath),
                   "TEST_APP_DIRECTORY": name, "TEST_CHECK_RESULT": str(check_result),
                   "TEST_MIGRATE_RESULT": str(migrate_result), "MIGRATE_ON_START": migrate_on_start,
                   "COLLECTSTATIC_ON_START": "0", "SEED_ON_START": "0"}
            result = subprocess.run(["sh", str(entrypoint), str(launch)], env=env, capture_output=True, text=True, timeout=10)
            return result, (directory / "commands").read_text().splitlines()

    def test_pending_schema_stops_web_without_automatically_applying_migrations(self):
        result, commands = self.run_entrypoint(check_result=1)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("launched", result.stdout)
        self.assertEqual(commands, ["manage.py migrate --check --noinput"])

    def test_current_schema_allows_web_to_start_after_read_only_check(self):
        result, commands = self.run_entrypoint()
        self.assertEqual(result.returncode, 0)
        self.assertIn("launched", result.stdout)
        self.assertEqual(commands, ["manage.py migrate --check --noinput"])

    def test_failed_explicit_migration_also_stops_web_startup(self):
        result, commands = self.run_entrypoint(migrate_on_start="1", migrate_result=1)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("launched", result.stdout)
        self.assertEqual(commands, ["manage.py migrate --noinput"])
