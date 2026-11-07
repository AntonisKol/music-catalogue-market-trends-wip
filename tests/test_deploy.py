"""The server setup in deploy/: the parts that keep secrets and the database private.

These read the files rather than starting containers; the full setup was run by
hand (see docs/decisions.md, issue 44).
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "deploy" / "compose.yml").read_text())
SERVICES = COMPOSE["services"]


def test_only_caddy_publishes_ports():
    published = {name for name, service in SERVICES.items() if service.get("ports")}
    assert published == {"caddy"}


def test_the_database_gets_only_its_own_settings():
    db = SERVICES["db"]
    assert "env_file" not in db
    assert set(db["environment"]) == {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}


def test_the_api_trusts_exactly_one_proxy_and_reaches_the_database_by_name():
    environment = SERVICES["api"]["environment"]
    assert environment["TRUSTED_PROXIES"] == "1"
    assert environment["POSTGRES_HOST"] == "db"


def test_caddy_forwards_to_the_api():
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text()
    assert "reverse_proxy api:8000" in caddyfile


def test_the_image_never_contains_the_env_file():
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert ".env" in ignored
    dockerfile = (ROOT / "Dockerfile").read_text()
    copied = [line for line in dockerfile.splitlines() if line.startswith("COPY")]
    assert not any(".env" in line or line.split()[1] == "." for line in copied)


def test_the_image_does_not_run_as_root():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "\nUSER app\n" in dockerfile


def test_cron_runs_the_nightly_update_with_the_backup():
    crontab = (ROOT / "deploy" / "crontab").read_text()
    assert "sh deploy/nightly.sh" in crontab
    nightly = (ROOT / "deploy" / "nightly.sh").read_text()
    assert "ingestion.nightly" in nightly and "deploy/backup.sh" in nightly


def test_the_server_settings_template_lists_every_setting_without_values():
    lines = [line for line in (ROOT / "deploy" / "server.env.example").read_text().splitlines() if "=" in line and not line.startswith("#")]
    names = {line.split("=", 1)[0] for line in lines}
    for secret in ("POSTGRES_PASSWORD", "DISCOGS_TOKEN", "ANTHROPIC_API_KEY", "TURNSTILE_SECRET_KEY", "VISITOR_HASH_SECRET"):
        assert secret in names
        value = next(line.split("=", 1)[1] for line in lines if line.startswith(secret + "="))
        assert value == ""


def test_the_server_settings_template_has_no_comments_after_values():
    # backup.sh and deploy.sh read everything after the = as the value.
    lines = (ROOT / "deploy" / "server.env.example").read_text().splitlines()
    assert "BACKUP_TARGET=" in lines
    assert not [line for line in lines if "=" in line and not line.startswith("#") and "#" in line]


def test_a_backup_removes_what_a_failed_run_left_behind_before_copying(tmp_path):
    (tmp_path / "deploy").mkdir()
    shutil.copy(ROOT / "deploy" / "backup.sh", tmp_path / "deploy" / "backup.sh")
    (tmp_path / ".env").write_text("BACKUP_TARGET=box:market-trends\n")
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "market-trends-2026-01-01.dump.partial").write_text("")
    # Stand-ins for the two outside programs: the dump succeeds, and rsync records what it would copy.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", "echo dump"), ("rsync", 'ls backups > copied.txt')):
        program = bin_dir / name
        program.write_text(f"#!/bin/sh\n{body}\n")
        program.chmod(program.stat().st_mode | stat.S_IEXEC)

    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    subprocess.run(["sh", "deploy/backup.sh"], cwd=tmp_path, env=env, check=True, capture_output=True)

    copied = (tmp_path / "copied.txt").read_text().split()
    assert len(copied) == 1 and copied[0].endswith(".dump")
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_the_server_never_logs_addresses_and_limits_its_logs():
    # Gunicorn's own access log would hold every visitor's address; web/app.py writes a hashed one.
    assert "access-logfile" not in (ROOT / "Dockerfile").read_text()
    for name, service in SERVICES.items():
        assert service["logging"]["options"]["max-size"], name
        assert service["logging"]["options"]["max-file"], name


def test_the_server_settings_template_lists_the_sign_up_settings():
    names = {line.split("=", 1)[0] for line in (ROOT / "deploy" / "server.env.example").read_text().splitlines()
             if "=" in line and not line.startswith("#")}
    assert {"RESEND_API_KEY", "PUBLIC_API_URL", "SIGN_IN_RETURN_URL", "SIGN_IN_EMAIL_FROM"} <= names
