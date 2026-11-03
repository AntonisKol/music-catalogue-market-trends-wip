#!/bin/sh
# Put the merged main branch live. Run on your Mac from the project folder:
#   sh deploy/deploy.sh          the server, then the page
#   sh deploy/deploy.sh server   only the API on the server
#   sh deploy/deploy.sh page     only the question box on adnsaudio.com/market-trends/
# Reads DEPLOY_SERVER (for example deploy@203.0.113.10) from the local .env; it is
# not secret, since the server's address is public through DNS. The page goes to
# the website's repository (adns-space), which GitHub Pages serves as adnsaudio.com.
set -eu
cd "$(dirname "$0")/.."
setting() { sed -n "s/^$1=//p" .env | tail -1; }
SERVER=$(setting DEPLOY_SERVER)
API_URL=https://api.adnsaudio.com
SITE_REPO=AntonisKol/adns-space
SITE_DIR=${SITE_DIR:-$HOME/.cache/market-trends-site}
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
  rev=$(git rev-parse --short HEAD)
  [ -d "$SITE_DIR/.git" ] || git clone --quiet "https://github.com/$SITE_REPO.git" "$SITE_DIR"
  git -C "$SITE_DIR" pull --ff-only --quiet
  # The page itself (market-trends/index.html, with the live API address and the
  # site key) belongs to the website; only the question box's two files come from here.
  cp widget/ask.js widget/ask.css "$SITE_DIR/market-trends/"
  cd "$SITE_DIR"
  git add market-trends/ask.js market-trends/ask.css
  if git diff --cached --quiet; then
    echo "The website already has this question box."
  else
    git commit --quiet -m "Market Trends: publish AntonisKol/music-catalogue-market-trends-wip@$rev"
    # The website may have moved on meanwhile.
    git pull --rebase --quiet origin main
    git push --quiet origin HEAD:main
    echo "Pushed to $SITE_REPO; GitHub Pages publishes https://adnsaudio.com/market-trends/ within a minute or two."
  fi
}

case "$what" in
  all) deploy_server; deploy_page ;;
  server) deploy_server ;;
  page) deploy_page ;;
  *) echo "Usage: sh deploy/deploy.sh [server|page]" >&2; exit 2 ;;
esac
