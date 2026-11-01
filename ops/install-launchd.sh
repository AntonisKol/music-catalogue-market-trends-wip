#!/bin/sh
# Install (or reinstall) the nightly run as a launchd job for the current user.
#   sh ops/install-launchd.sh            install
#   sh ops/install-launchd.sh remove     uninstall
set -eu
NAME=com.adnsaudio.market-trends.nightly
PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
TARGET="$HOME/Library/LaunchAgents/$NAME.plist"

launchctl bootout "gui/$(id -u)/$NAME" 2>/dev/null || true
if [ "${1:-}" = "remove" ]; then
  rm -f "$TARGET"
  echo "Removed the nightly run."
  exit 0
fi
if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
  echo "No virtual environment at $PROJECT_DIR/.venv; create it first (see README)." >&2
  exit 1
fi
mkdir -p "$PROJECT_DIR/logs" "$HOME/Library/LaunchAgents"
sed "s#PROJECT_DIR#$PROJECT_DIR#g" "$PROJECT_DIR/ops/$NAME.plist" > "$TARGET"
launchctl bootstrap "gui/$(id -u)" "$TARGET"
echo "Installed: python -m ingestion.nightly runs every night at 03:00 from $PROJECT_DIR."
echo "Run it now to test:  launchctl kickstart gui/$(id -u)/$NAME"
