#!/usr/bin/env bash
# Deploy the dashboard to the Hetzner VPS.
#
#   ./deploy/deploy-dashboard.sh [domain]
#
# The box is CloudPanel-managed with only ~500 MB of RAM free, so this
# deliberately does NOT install Docker, Node or a database server. It is a
# Flask app behind gunicorn behind the nginx CloudPanel already runs, with
# SQLite for state — two small Python processes and a file.
#
# Prerequisites, done once in the CloudPanel UI:
#   1. Create a site with the site-user below, type "Python" or "Static"
#   2. Point the domain at this box. If it sits behind Cloudflare the record
#      must stay DNS-only (grey cloud): the free plan rejects proxied uploads
#      over 100 MB, and a finished video is routinely larger than that, so
#      proxying it would break the worker's upload on longer songs.
set -euo pipefail

DOMAIN="${1:?usage: deploy-dashboard.sh <domain>}"
SITE_USER="${SITE_USER:-hopewell}"
SSH_HOST="${SSH_HOST:-hetzner}"
APP_DIR="/home/${SITE_USER}/htdocs/${DOMAIN}"
SERVICE="hopewell-dashboard"
PORT="${PORT:-5060}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "Packaging"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/app"
# Only what the dashboard itself needs. The render engine deliberately does
# NOT ship here: it would pull numpy and Pillow onto a box with a few hundred
# MB of headroom, purely to read six theme names. The dashboard reads those
# from dashboard/themes.json instead (see scripts/export_themes.py).
python3 scripts/export_themes.py >/dev/null
cp -R dashboard "$TMP/app/"
find "$TMP/app" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
tar -czf "$TMP/app.tar.gz" -C "$TMP" app

say "Uploading to ${SSH_HOST}:${APP_DIR}"
ssh "$SSH_HOST" "mkdir -p '${APP_DIR}'"
scp -q "$TMP/app.tar.gz" "${SSH_HOST}:/tmp/hopewell-app.tar.gz"

say "Installing"
ssh "$SSH_HOST" bash -se <<REMOTE
set -euo pipefail
APP_DIR='${APP_DIR}'
SITE_USER='${SITE_USER}'
PORT='${PORT}'
SERVICE='${SERVICE}'

rm -rf "\$APP_DIR/app.new"
mkdir -p "\$APP_DIR/app.new"
tar -xzf /tmp/hopewell-app.tar.gz -C "\$APP_DIR/app.new" --strip-components=1
rm -f /tmp/hopewell-app.tar.gz

# Keep data (SQLite + finished videos) outside the code directory so a deploy
# can never wipe the queue.
mkdir -p "/home/\$SITE_USER/hopewell-data/media"

# Check for a WORKING interpreter, not merely a directory. A venv whose
# creation failed part-way (python3-venv missing, disk full) leaves the folder
# behind, and a directory-only test then skips rebuilding it forever — every
# later deploy fails on a missing pip instead of fixing itself.
if [ ! -x "\$APP_DIR/venv/bin/pip" ]; then
  rm -rf "\$APP_DIR/venv"
  python3 -m venv "\$APP_DIR/venv"
fi
"\$APP_DIR/venv/bin/pip" -q install --upgrade pip
"\$APP_DIR/venv/bin/pip" -q install flask gunicorn

rm -rf "\$APP_DIR/app.old"
[ -d "\$APP_DIR/app" ] && mv "\$APP_DIR/app" "\$APP_DIR/app.old"
mv "\$APP_DIR/app.new" "\$APP_DIR/app"

chown -R "\$SITE_USER:\$SITE_USER" "\$APP_DIR" "/home/\$SITE_USER/hopewell-data"

cat > /etc/systemd/system/\$SERVICE.service <<UNIT
[Unit]
Description=Hopewell lyric video dashboard
After=network.target

[Service]
Type=simple
User=\$SITE_USER
WorkingDirectory=\$APP_DIR/app
Environment=HOPEWELL_DATA=/home/\$SITE_USER/hopewell-data
Environment=PYTHONUNBUFFERED=1
# Two workers only: this box has ~500 MB free and each one is ~60 MB.
ExecStart=\$APP_DIR/venv/bin/gunicorn \\
    --workers 2 --threads 4 --timeout 1800 \\
    --bind 127.0.0.1:\$PORT \\
    dashboard.app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable \$SERVICE >/dev/null 2>&1 || true
systemctl restart \$SERVICE
sleep 2
systemctl is-active \$SERVICE
echo "worker token: \$(cat /home/\$SITE_USER/hopewell-data/worker-token.txt 2>/dev/null || echo '(created on first request)')"
REMOTE

say "Health check"
ssh "$SSH_HOST" "curl -sS -m 10 http://127.0.0.1:${PORT}/healthz && echo"

cat <<NEXT

Still to do by hand, once:

  1. Point nginx at the app and put the shared password in front of it.
     In the CloudPanel vhost for ${DOMAIN}, proxy to 127.0.0.1:${PORT} and add:

        auth_basic           "Hopewell";
        auth_basic_user_file /etc/nginx/.htpasswd_hopewell;
        client_max_body_size 700m;          # finished videos are large
        proxy_read_timeout   1800s;

        location /api/ {                     # the worker uses a bearer token,
            auth_basic off;                  # not the shared page password
            proxy_pass http://127.0.0.1:${PORT};
        }

  2. Create the shared password:

        ssh ${SSH_HOST} "htpasswd -Bc /etc/nginx/.htpasswd_hopewell hopewell"

  3. Save that password AND the worker token above into 1Password.

NEXT
