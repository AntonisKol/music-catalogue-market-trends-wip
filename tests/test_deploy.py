"""The server setup in deploy/: the parts that keep secrets and the database private.

These read the files rather than starting containers; the full setup was run by
hand (see docs/decisions.md, issue 44).
"""
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
    for secret in ("POSTGRES_PASSWORD", "DISCOGS_TOKEN", "ANTHROPIC_API_KEY", "TURNSTILE_SECRET_KEY", "RESEND_API_KEY", "SMTP_PASSWORD"):
        assert secret in names
        value = next(line.split("=", 1)[1] for line in lines if line.startswith(secret + "="))
        assert value.split("#")[0].strip() == ""
