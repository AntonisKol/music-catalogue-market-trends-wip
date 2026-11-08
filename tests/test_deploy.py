"""The server setup in deploy/: the parts that keep secrets and the database private.

These read the files rather than starting containers; the full setup was run by
hand (see the README, Run it on a server, and issue 44).
"""
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
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
    assert "TRUSTED_PROXIES=1" in environment
    assert "POSTGRES_HOST=db" in environment


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


def test_the_page_goes_to_the_website_through_a_pull_request():
    # The website's main only takes pull requests with passing checks.
    script = (ROOT / "deploy" / "deploy.sh").read_text()
    page = script[script.index("deploy_page() {"):script.index("\n}\n", script.index("deploy_page() {"))]
    assert "HEAD:main" not in page and "gh pr create" in page
    assert "cp widget/ask.js widget/ask.css" in page


# --- What each container is given (issue 68) ---------------------------------------------


READ_SETTING = re.compile(r'os\.environ(?:\.get)?[\[(]"([A-Z_]+)"')


def settings_read_by(*paths):
    return {name for path in paths for name in READ_SETTING.findall((ROOT / path).read_text())}


def api_settings():
    return {item.split("=", 1)[0] for item in SERVICES["api"]["environment"]}


def test_the_api_gets_every_setting_its_code_reads_and_nothing_else():
    needed = settings_read_by("web/config.py", "queries/config.py") | {"ANTHROPIC_API_KEY"}

    assert api_settings() == needed


def test_the_api_never_gets_the_discogs_token_or_the_owners_database_password():
    assert "env_file" not in SERVICES["api"]
    assert not api_settings() & settings_read_by("ingestion/config.py") - {"POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB"}


def test_the_admin_container_gets_every_setting_and_only_runs_when_asked():
    admin = SERVICES["admin"]
    assert admin["env_file"] == "../.env" and admin["profiles"] == ["admin"]
    assert admin["image"] == SERVICES["api"]["image"]
    every_setting = {line.split("=", 1)[0] for line in (ROOT / "deploy" / "server.env.example").read_text().splitlines()
                     if "=" in line and not line.startswith("#")}
    assert settings_read_by("ingestion/config.py") - {"POSTGRES_HOST", "POSTGRES_PORT"} <= every_setting


@pytest.mark.parametrize("script", ["nightly.sh", "deploy.sh", "restore.sh"])
def test_ingestion_commands_run_in_the_admin_container(script):
    text = (ROOT / "deploy" / script).read_text()
    assert "run --rm -T admin python -m ingestion." in text
    assert "run --rm -T api python -m ingestion." not in text


def test_new_label_requests_are_emailed_at_once_by_a_watcher_that_always_runs():
    watcher = SERVICES["label-requests"]
    assert watcher["command"] == "python -m ingestion.label_requests watch"
    assert watcher["restart"] == "unless-stopped" and "profiles" not in watcher
    # Like the admin container: the Discogs token for the matches, and the owner's login.
    assert watcher["env_file"] == "../.env" and watcher["image"] == SERVICES["api"]["image"]
    assert watcher["environment"] == SERVICES["admin"]["environment"]
    # cron no longer sends them too, so nobody is emailed twice.
    assert "label_requests" not in (ROOT / "deploy" / "crontab").read_text()


def test_the_review_script_only_imports_after_an_approval_went_through():
    script = (ROOT / "scripts" / "label-requests").read_text()
    assert script.index('[ "$status" -eq 0 ] || exit "$status"') < script.index("python -m ingestion.ingest_label")
    assert "*[!0-9]*" in script
    # approve <request> <label>: the label, not the request number, is imported.
    assert 'printf \'%s\' "$3"' in script
