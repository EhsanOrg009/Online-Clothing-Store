#!/usr/bin/env python3
"""Build/run production backup architecture against an isolated Compose QA DB.

All production-stack operations go through scripts/compose-production with a
unique project and synthetic credentials. No existing volume is removed. The
restore target is a separate temporary MariaDB with tmpfs data, never the store.
Private temporary fixtures/logs remain available for review after the run.
"""

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.db_backup import NAME_PATTERN, verify_backup_checksum, verify_gzip  # noqa: E402


class QAError(Exception):
    pass


class BackupStackQA:
    def __init__(self):
        self.token = secrets.token_hex(5)
        self.project = "minikandoo-backup-qa-" + self.token
        # Docker Desktop on Linux shares the user's home directory, while its
        # VM's /tmp is different from the host's /tmp. Native Docker works too.
        artifact_root = Path.home() / ".cache" / "minikandoo-backup-qa"
        artifact_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix=self.project + "-", dir=artifact_root))
        self.backups = self.directory / "backups"
        self.backups.mkdir(mode=0o700)
        self.override = self.directory / "qa.yml"
        self.override.write_text(
            "services:\n  db:\n    volumes: !reset []\n    tmpfs:\n"
            "      - /var/lib/mysql:rw,nosuid,nodev,size=512m\n"
            '    ports:\n      - "127.0.0.1::3306"\n'
            "    networks:\n      shop-net: {}\n      qa-test-access:\n        gw_priority: 1\n"
            "networks:\n  qa-test-access:\n    driver: bridge\n"
        )
        self.bad_override = self.directory / "unavailable-db.yml"
        self.bad_override.write_text(
            "services:\n  db-backup:\n    environment:\n      DATABASE_HOST: unavailable-qa-only.invalid\n"
        )
        app_subnet, proxy_subnet = self.choose_subnets()
        self.password = secrets.token_urlsafe(24)
        self.database = "minikandoo_qa_" + self.token
        values = {
            "COMPOSE_PROJECT_NAME": self.project, "SECRET_KEY": secrets.token_urlsafe(54),
            "DATABASE_NAME": self.database, "DATABASE_USER": "qa_app", "DATABASE_PASSWORD": secrets.token_urlsafe(24),
            "MARIADB_ROOT_PASSWORD": self.password, "ZIBAL_MERCHANT": "qa-only-no-api-calls",
            "SMS_API_KEY": "", "SMS_API_SECRET": "", "SMS_SENDER": "",
            "APP_SUBNET": app_subnet, "PROXY_SUBNET": proxy_subnet,
            "NGINX_PROXY_IP": str(ipaddress.ip_network(proxy_subnet).network_address + 2),
            "BACKUP_HOST_DIR": str(self.backups), "BACKUP_UID": str(os.getuid()), "BACKUP_GID": str(os.getgid()),
            "BACKUP_RETENTION_MODE": "apply", "BACKUP_MIN_FREE_BYTES": "0",
        }
        self.environment_file = self.directory / "synthetic-only.env"
        self.environment_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        self.environment_file.chmod(0o600)
        self.redacted_values = [values[key] for key in (
            "SECRET_KEY", "DATABASE_PASSWORD", "MARIADB_ROOT_PASSWORD",
        )]
        self.env = {**os.environ, **values, "ENV_FILE": str(self.environment_file)}
        self.restore_name = self.project + "-restore"
        self.report = {"environment": "isolated local QA", "project": self.project,
                       "artifacts": str(self.directory), "checks": []}
        self.command_index = 0
        self.historical = self.backups / "db-20261114-190647.sql.gz"
        self.historical.write_bytes(gzip.compress(b"-- synthetic historical backup fixture only\n", mtime=0))
        self.historical_hash = hashlib.sha256(self.historical.read_bytes()).hexdigest()

    @staticmethod
    def choose_subnets():
        ids = subprocess.run(["docker", "network", "ls", "-q"], capture_output=True, text=True, check=True).stdout.split()
        networks = json.loads(subprocess.run(["docker", "network", "inspect", *ids], capture_output=True, text=True,
                                            check=True).stdout) if ids else []
        used = [ipaddress.ip_network(entry["Subnet"]) for network in networks
                for entry in (network.get("IPAM", {}).get("Config") or []) if entry.get("Subnet")]
        selected = []
        for index in range(256):
            candidate = ipaddress.ip_network(f"172.29.{index}.0/24")
            if not any(candidate.overlaps(net) for net in used if net.version == 4):
                selected.append(str(candidate))
                if len(selected) == 2:
                    return selected
        raise QAError("No free QA network subnets found.")

    def run(self, arguments, *, phase, input=None, env=None, expected=0, timeout=180):
        self.command_index += 1
        print(f"QA: {phase}", flush=True)
        result = subprocess.run(arguments, cwd=ROOT, env=env or self.env, input=input,
                                capture_output=True, text=True, timeout=timeout)
        logfile = self.directory / f"{self.command_index:02d}.log"
        logged_output = result.stdout + result.stderr
        for secret in self.redacted_values:
            logged_output = logged_output.replace(secret, "[redacted synthetic credential]")
        logfile.write_text(logged_output)
        logfile.chmod(0o600)
        if result.returncode != expected:
            raise QAError(f"{phase} failed (exit {result.returncode}); inspect private QA log {logfile.name}.")
        return result

    def compose(self, *args, phase, bad=False, expected=0, timeout=180, input=None):
        command = [str(ROOT / "scripts/compose-production")]
        if args and args[0] == "--bootstrap":
            command.append("--bootstrap")
            args = args[1:]
        command += ["-f", str(self.override)]
        if bad:
            command += ["-f", str(self.bad_override)]
        return self.run([*command, *args], phase=phase, expected=expected, timeout=timeout, input=input)

    def check(self, name, **details):
        self.report["checks"].append({"name": name, "passed": True, **details})

    def wait(self, predicate, *, phase, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(1)
        raise QAError(phase + " timed out.")

    def container(self, service):
        result = self.compose("ps", "-q", service, phase=f"identify QA {service}")
        container_id = result.stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{12,64}", container_id):
            raise QAError("Invalid QA container identifier.")
        info = json.loads(self.run(["docker", "inspect", container_id], phase="check QA container label").stdout)[0]
        if info["Config"]["Labels"].get("com.docker.compose.project") != self.project:
            raise QAError("Container is outside the isolated QA project; refusing operation.")
        return container_id, info

    def worker_python(self, code, *, phase, input=None, extra_env=(), expected=0):
        return self.compose(
            "run", "--rm", "--no-deps", "-T", "--entrypoint", "python3",
            *extra_env, "db-backup", "-c",
            "import sys; sys.path.insert(0, '/opt/minikandoo'); import db_backup as b; " + code,
            phase=phase, input=input, expected=expected,
        )

    def sql(self, sql, *, phase, extra_env=(), database=True):
        code = (
            "import subprocess; config = b.Config.from_env();\n"
            "with b.client_options(config) as options:\n"
            " result = subprocess.run(['mariadb', '--defaults-file=' + str(options), '--batch', '--skip-column-names']"
            + (" + [config.database]" if database else "")
            + ", input=sys.stdin.buffer.read(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=b.client_environment());\n"
            " sys.stdout.buffer.write(result.stdout); sys.exit(result.returncode)\n"
        )
        return self.worker_python(code, phase=phase, input=sql, extra_env=extra_env)

    def run_checks(self, *, skip_django=False):
        self.compose("config", "--quiet", phase="production Compose syntax")
        self.compose("--bootstrap", "config", "--quiet", phase="bootstrap Compose syntax")
        config = json.loads(self.compose("config", "--format", "json", phase="sanitized Compose review").stdout)
        service = config["services"]["db-backup"]
        assert not service.get("ports") and not service.get("privileged")
        assert service["read_only"] and service["cap_drop"] == ["ALL"]
        assert service["volumes"][0]["source"] == str(self.backups)
        assert service["depends_on"]["db"]["condition"] == "service_healthy"
        self.check("Compose mount, healthy DB dependency, network and security")
        self.compose("build", "db-backup", phase="backup image build", timeout=900)
        self.check("backup image build")
        self.compose("up", "-d", "--wait", "--wait-timeout", "120", "db", phase="isolated MariaDB startup")
        port = self.compose("port", "db", "3306", phase="isolated loopback DB port").stdout.strip().rsplit(":", 1)[1]
        self.container("db")

        def reachable():
            try:
                with socket.create_connection(("127.0.0.1", int(port)), timeout=2):
                    return True
            except OSError:
                return False

        self.wait(reachable, phase="isolated loopback DB connectivity", timeout=15)
        test_env = {
            **self.env, "PYTHONPATH": str(ROOT / "scripts/qa"),
            "MINIKANDOO_TEST_DATABASE": self.database, "MINIKANDOO_TEST_DATABASE_PASSWORD": self.password,
            "MINIKANDOO_TEST_DATABASE_HOST": "127.0.0.1", "MINIKANDOO_TEST_DATABASE_PORT": port,
        }
        python = str(ROOT / ".venv/bin/python")
        if not skip_django:
            result = self.run([python, "manage.py", "test", "--settings=shop_mariadb_test_settings", "--noinput"],
                              env=test_env, phase="full Django suite on isolated MariaDB", timeout=600)
            summary = re.search(r"Ran ([0-9]+) tests in ([0-9.]+)s", result.stdout + result.stderr)
            self.check("full Django suite on MariaDB", tests=int(summary[1]) if summary else None)
        self.run([python, "manage.py", "migrate", "--settings=shop_mariadb_test_settings", "--noinput"],
                 env=test_env, phase="migrations on isolated backup source", timeout=180)
        self.sql(
            "CREATE TABLE backup_restore_probe (id int PRIMARY KEY, value varchar(64) UNIQUE) ENGINE=InnoDB;\n"
            "CREATE TABLE backup_trigger_probe (id int) ENGINE=InnoDB;\n"
            "CREATE TRIGGER qa_probe_trigger AFTER INSERT ON backup_restore_probe FOR EACH ROW INSERT INTO backup_trigger_probe VALUES (NEW.id);\n"
            "CREATE PROCEDURE qa_probe_routine() SELECT COUNT(*) FROM backup_restore_probe;\n"
            "CREATE EVENT qa_probe_event ON SCHEDULE AT CURRENT_TIMESTAMP + INTERVAL 10 YEAR DO INSERT INTO backup_trigger_probe VALUES (999);\n"
            "INSERT INTO backup_restore_probe VALUES (1, 'synthetic first'), (2, 'synthetic second');\n",
            phase="synthetic restore probe tables, routine, trigger and event",
        )
        self.compose("up", "-d", "--wait", "--wait-timeout", "120", "db-backup", phase="scheduler initial backup and health")
        first = list(self.backups.glob("*.sql.gz"))
        assert len(first) >= 2 and self.historical in first
        initial_points = [path for path in first if NAME_PATTERN.fullmatch(path.name)]
        assert initial_points
        self.check("scheduler initial execution and health")
        self.run([str(ROOT / "scripts/backup-production-db")], phase="manual backup through production wrapper")
        points = [path for path in self.backups.glob("*.sql.gz") if NAME_PATTERN.fullmatch(path.name)]
        # A real half-hour boundary can fall during a health/manual check.
        assert len(points) >= len(initial_points) + 1
        for path in points:
            verify_backup_checksum(path)
            verify_gzip(path)
        self.check("manual backup, unique UTC names, gzip and recorded SHA256")
        self.run(
            [python, "manage.py", "shell", "--settings=shop_mariadb_test_settings", "-c",
             "from payments.management.commands.reconcile_zibal_payments import validate_backup; "
             f"validate_backup({str(points[0])!r}, {self.database!r}); "
             "print('Fresh native dump accepted by recovery backup validator.')"],
            env=test_env, phase="native dump SQL headers and recorded checksum accepted by recovery",
        )
        self.check("fresh native backup passes recovery command's gzip, SQL header and SHA256 sidecar checks")
        version = self.compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "mariadb-dump", "db-backup", "--version",
                               phase="official backup client version").stdout.strip()
        self.check("MariaDB client environment", version=version)
        self.sql("CREATE TABLE backup_nontransactional_probe (id int PRIMARY KEY) ENGINE=MyISAM;\n",
                 phase="isolated non-transactional table fixture")
        count_before = len(list(self.backups.glob("*.sql.gz")))
        self.run([str(ROOT / "scripts/backup-production-db")], phase="non-transactional default refusal", expected=1)
        assert len(list(self.backups.glob("*.sql.gz"))) == count_before
        self.compose("run", "--rm", "--no-deps", "-T", "--env", "BACKUP_NON_TRANSACTIONAL_POLICY=lock-all",
                     "db-backup", "backup", phase="explicit lock-all backup of isolated MyISAM fixture")
        self.sql("ALTER TABLE backup_nontransactional_probe ENGINE=InnoDB;\n", phase="return QA fixture to InnoDB")
        self.check("actual non-transactional engine refusal and explicitly selected lock-all dump")
        preview = self.compose("run", "--rm", "--no-deps", "-T", "db-backup", "inspect", phase="retention/capacity dry-run")
        events = [json.loads(line) for line in preview.stdout.splitlines() if line.startswith("{")]
        self.report["database_measurement"] = next(event for event in events if event["event"] == "database_inspected")
        self.report["retention_dry_run"] = next(event for event in events if event["event"] == "retention_plan")
        self.check("actual isolated schema engine, size, retention and capacity inspection")

        hold_code = (
            "import sys; sys.path.insert(0, '/opt/minikandoo'); import db_backup as b; "
            "lock=b.directory_lock(b.Config.from_env().directory); lock.__enter__(); "
            "print('held',flush=True); sys.stdin.read(); lock.__exit__(None,None,None)"
        )
        hold_command = [str(ROOT / "scripts/compose-production"), "-f", str(self.override), "run", "--rm", "--no-deps", "-T",
                        "--entrypoint", "python3", "db-backup", "-c", hold_code]
        with subprocess.Popen(hold_command, cwd=ROOT, env=self.env, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as holder:
            try:
                assert holder.stdout.readline().strip() == "held"
                self.run([str(ROOT / "scripts/backup-production-db")], phase="cross-container concurrent backup rejection", expected=75)
                self.compose("run", "--rm", "--no-deps", "-T", "db-backup", "retention", "--dry-run",
                             phase="cross-container concurrent retention rejection", expected=75)
            finally:
                holder.communicate(input="", timeout=30)
        self.check("cross-container flock prevents manual/scheduled implementation and retention overlap")

        _, info = self.container("db-backup")
        assert not any(mount["Type"] == "volume" for mount in info["Mounts"])
        # Engine versions expose tmpfs in HostConfig rather than Mounts. Check
        # the live kernel's read-only filesystem flag as well as its config.
        assert "ro" in info["HostConfig"]["Tmpfs"]["/var/lib/mysql"].split(",")
        self.compose("exec", "-T", "db-backup", "python3", "-c",
                     "import os; assert os.statvfs('/var/lib/mysql').f_flag & os.ST_RDONLY",
                     phase="live kernel read-only flag for inherited database-data path")
        self.check("inherited database-data volume masked by read-only tmpfs; no anonymous backup-service volumes")
        restarts = info["RestartCount"]
        self.compose("exec", "-T", "db-backup", "python3", "-c", "import os, signal; os.kill(1, signal.SIGTERM)",
                     phase="QA scheduler process termination")

        def restarted():
            _, current = self.container("db-backup")
            return current["RestartCount"] > restarts and current["State"].get("Health", {}).get("Status") == "healthy"

        self.wait(restarted, phase="automatic scheduler container restart")
        self.check("restart policy automatically restarts terminated scheduler and performs new initial backup")

        self.compose("up", "-d", "--no-deps", "db-backup", bad=True, phase="QA unreachable MariaDB configuration")

        def failed():
            logs = self.compose("logs", "--no-color", "db-backup", phase="QA backup failure logs", bad=True).stdout
            return '"event": "backup_failed"' in logs

        self.wait(failed, phase="visible unavailable DB failure")
        assert all(path.exists() for path in points)
        self.compose("up", "-d", "--wait", "--wait-timeout", "120", "db-backup", phase="QA service recovery after failure")
        self.check("unavailable database failure visible in Compose logs; backups survive recreation and recovery")
        logs = self.compose("logs", "--no-color", "db-backup", phase="normal Compose backup logs").stdout
        assert '"event": "backup_completed"' in logs and self.password not in logs
        assert hashlib.sha256(self.historical.read_bytes()).hexdigest() == self.historical_hash
        self.check("historical backup preserved and credentials absent from backup logs")

        restore_env = self.directory / "restore-only.env"
        restore_env.write_text("MARIADB_ROOT_PASSWORD=" + self.password + "\n")
        restore_env.chmod(0o600)
        self.run(
            ["docker", "run", "-d", "--name", self.restore_name, "--label", "minikandoo.qa=" + self.token,
             "--network", self.project + "_shop-net", "--env-file", str(restore_env),
             "--tmpfs", "/var/lib/mysql:rw,nosuid,nodev,size=512m",
             "--health-cmd", "healthcheck.sh --connect --innodb_initialized", "--health-interval", "1s",
             "--health-start-period", "10s", "mariadb:11.4", "--character-set-server=utf8mb4", "--collation-server=utf8mb4_unicode_ci"],
            phase="isolated temporary restore target; no production volumes",
        )

        def restore_healthy():
            info = json.loads(self.run(["docker", "inspect", self.restore_name], phase="restore target health").stdout)[0]
            return info["State"].get("Health", {}).get("Status") == "healthy"

        self.wait(restore_healthy, phase="isolated restore target health")
        restore_extra = ("--env", "DATABASE_HOST=" + self.restore_name, "--env", "DATABASE_NAME=minikandoo_qa_restore")
        self.sql("CREATE DATABASE minikandoo_qa_restore CHARACTER SET utf8mb4;\n", phase="create isolated restore database",
                 extra_env=restore_extra, database=False)
        latest = sorted(path for path in self.backups.glob("*.sql.gz") if NAME_PATTERN.fullmatch(path.name))[-1]
        restore_code = (
            "import subprocess, gzip; config=b.Config.from_env(); path=config.directory/" + repr(latest.name) + "; "
            "b.verify_backup_checksum(path); b.verify_gzip(path);\n"
            "with b.client_options(config) as options, gzip.open(path,'rb') as stream:\n"
            " result=subprocess.run(['mariadb','--defaults-file='+str(options),config.database],input=stream.read(),"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=b.client_environment()); sys.exit(result.returncode)\n"
        )
        self.worker_python(restore_code, phase="SQL restore into isolated target", extra_env=restore_extra)
        result = self.sql(
            "SELECT COUNT(*) FROM backup_restore_probe WHERE (id=1 AND value='synthetic first') OR (id=2 AND value='synthetic second');\n"
            "SELECT COUNT(*) FROM backup_trigger_probe;\n"
            "SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE();\n"
            "SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE();\n"
            "SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE();\n"
            "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ('payments_payment','orders_order','products_productvariant');\n",
            phase="restored row counts, routine, trigger, event and Django tables", extra_env=restore_extra,
        )
        assert result.stdout.splitlines() == ["2", "2", "1", "1", "1", "3"]
        self.check("actual isolated SQL restore, row counts, routines, triggers, events and Django schema")
        self.report["backup_sizes_bytes"] = [path.stat().st_size for path in points]
        self.report["production_deployed"] = False
        self.report["production_verified"] = False

    def cleanup(self):
        result = subprocess.run(["docker", "inspect", self.restore_name], capture_output=True, text=True)
        if result.returncode == 0:
            info = json.loads(result.stdout)[0]
            if info["Config"]["Labels"].get("minikandoo.qa") == self.token:
                self.run(["docker", "rm", "-f", self.restore_name], phase="remove only isolated tmpfs restore container")
        self.compose("down", phase="stop only unique QA stack; no volume deletion", timeout=120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-django-tests", action="store_true", help="Run only backup integration checks.")
    args = parser.parse_args()
    qa = BackupStackQA()
    print("Private QA artifacts: " + str(qa.directory), flush=True)
    result = 0
    try:
        qa.run_checks(skip_django=args.skip_django_tests)
    except (QAError, AssertionError, OSError, subprocess.TimeoutExpired) as exc:
        qa.report["failure"] = str(exc) if isinstance(exc, QAError) else type(exc).__name__
        try:
            logs = qa.compose("logs", "--no-color", "--tail", "40", "db-backup", phase="QA failure diagnostics").stdout
            safe_events = []
            for line in logs.splitlines():
                if "{" in line:
                    try:
                        event = json.loads(line[line.index("{"):])
                        safe_events.append({key: event[key] for key in ("event", "reason", "filename") if key in event})
                    except ValueError:
                        pass
            qa.report["backup_events_on_failure"] = safe_events
        except (QAError, OSError):
            pass
        result = 1
    finally:
        qa.cleanup()
        (qa.directory / "report.json").write_text(json.dumps(qa.report, indent=2))
    print(json.dumps(qa.report, indent=2), flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
