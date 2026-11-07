#!/bin/sh
# Prepare a fresh Ubuntu 24.04 server for Market Trends. Run once, as root:
#   ssh root@<server> 'sh -s' < deploy/setup-server.sh
# It installs Docker, turns on the firewall and automatic security updates,
# creates the user "deploy" (who logs in with the same SSH key as root), turns off
# password and root logins, clones the project to /srv/market-trends and installs
# the nightly cron job. Running it again is safe.
set -eu
REPO=https://github.com/AntonisKol/music-catalogue-market-trends-wip.git
DIR=/srv/market-trends
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get -y upgrade
apt-get install -y docker.io docker-compose-v2 git rsync cron ufw unattended-upgrades
systemctl enable --now docker
dpkg-reconfigure -f noninteractive unattended-upgrades

# Only SSH and the web ports. The database publishes no port, so Docker never opens one.
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

id deploy > /dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
usermod -aG docker deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
if [ ! -s /root/.ssh/authorized_keys ]; then
  echo "root has no SSH key in /root/.ssh/authorized_keys; add one before running this." >&2
  exit 1
fi
install -m 600 -o deploy -g deploy /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
# The key the server uses to copy backups to the Storage Box.
[ -f /home/deploy/.ssh/id_ed25519 ] || su deploy -c "ssh-keygen -q -t ed25519 -N '' -C market-trends-backup -f /home/deploy/.ssh/id_ed25519"

cat > /etc/ssh/sshd_config.d/10-market-trends.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
CONF
# ssh.service may not be running on Ubuntu 24.04, where connections start it through
# ssh.socket. A plain reload then fails and stops this script; new logins read the file anyway.
systemctl try-reload-or-restart ssh

if [ ! -d "$DIR/.git" ]; then
  install -d -o deploy -g deploy "$DIR"
  su deploy -c "git clone $REPO $DIR"
fi
su deploy -c "mkdir -p $DIR/backups"
# Both the containers (uid 1000) and cron (as deploy) write logs here.
install -d -m 2775 -o 1000 -g deploy "$DIR/logs"
su deploy -c "crontab $DIR/deploy/crontab"

echo
echo "Server ready. From now on log in as: ssh deploy@<server>"
echo "Public key to add to the Storage Box:"
cat /home/deploy/.ssh/id_ed25519.pub
