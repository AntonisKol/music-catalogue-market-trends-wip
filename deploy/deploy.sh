#!/bin/sh
# Put the merged main branch live. Run on your Mac from the project folder:
#   sh deploy/deploy.sh          the server, then the page
#   sh deploy/deploy.sh server   only the API on the server
#   sh deploy/deploy.sh page     only the page on adnsaudio.com/market-trends/
# Reads DEPLOY_SERVER (for example deploy@203.0.113.10) and TURNSTILE_SITE_KEY
# from the local .env. Neither is secret: the server's address is public through
# DNS, and the site key is in the page for everyone to see.
set -eu
cd "$(dirname "$0")/.."
setting() { sed -n "s/^$1=//p" .env | tail -1; }
SERVER=$(setting DEPLOY_SERVER)
SITE_KEY=$(setting TURNSTILE_SITE_KEY)
API_URL=https://api.adnsaudio.com
PAGE_REPO=AntonisKol/market-trends
PAGE_DIR=${PAGE_DIR:-$HOME/.cache/market-trends-page}
what=${1:-all}

deploy_server() {
  [ -n "$SERVER" ] || { echo "Set DEPLOY_SERVER in .env first." >&2; exit 1; }
  echo "Updating the server ($SERVER)..."
  ssh "$SERVER" 'set -e
    cd /srv/market-trends
    git pull --ff-only
    crontab deploy/crontab
    COMPOSE="docker compose --env-file .env -f deploy/compose.yml"
    $COMPOSE up -d --build
    # New tables and rights from the update; both are safe to run every time.
    $COMPOSE run --rm -T api python -m ingestion.apply_schema
    $COMPOSE run --rm -T api python -m ingestion.create_limits_role
    docker image prune -f > /dev/null
    git log --oneline -1'
  # The API answers once it is up; give it a few seconds.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS "$API_URL/api/account" > /dev/null 2>&1; then echo "The API answers at $API_URL."; return; fi
    sleep 3
  done
  echo "The API at $API_URL does not answer; check with: ssh $SERVER 'cd /srv/market-trends && docker compose --env-file .env -f deploy/compose.yml logs --tail 50 api'" >&2
  exit 1
}

deploy_page() {
  [ -n "$SITE_KEY" ] || { echo "Set TURNSTILE_SITE_KEY in .env first." >&2; exit 1; }
  rev=$(git rev-parse --short HEAD)
  [ -d "$PAGE_DIR/.git" ] || git clone "git@github.com:$PAGE_REPO.git" "$PAGE_DIR"
  git -C "$PAGE_DIR" pull --ff-only --quiet
  cp widget/ask.js widget/ask.css "$PAGE_DIR/"
  # The local demo page, pointed at the live API and the real site key.
  sed -e "s#data-api=\"[^\"]*\"#data-api=\"$API_URL\"#" \
      -e "s#data-turnstile-sitekey=\"[^\"]*\"#data-turnstile-sitekey=\"$SITE_KEY\"#" \
      -e "s#Market Trends question box (local demo)#Market Trends · ADNS Audio#" \
      -e "/Cloudflare's test site key/d" \
      widget/index.html > "$PAGE_DIR/index.html"
  cd "$PAGE_DIR"
  git add index.html ask.js ask.css
  if git diff --cached --quiet; then
    echo "The page is already up to date."
  else
    git commit --quiet -m "Update the question box from $rev"
    git push --quiet
    echo "Page pushed; GitHub Pages publishes it at https://adnsaudio.com/market-trends/ within a minute or two."
  fi
}

case "$what" in
  all) deploy_server; deploy_page ;;
  server) deploy_server ;;
  page) deploy_page ;;
  *) echo "Usage: sh deploy/deploy.sh [server|page]" >&2; exit 2 ;;
esac
