#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/thinksync}"

apt-get update
apt-get install -y --no-install-recommends \
  nginx \
  python3-venv python3-pip \
  certbot \
  python3-certbot-nginx \
  nodejs npm \
  ufw

mkdir -p /etc/thinksync
mkdir -p /var/log/thinksync
mkdir -p /root/workspaces

install -m 0644 "$APP_DIR/infra/systemd/thinksync-backend.service" /etc/systemd/system/thinksync-backend.service
install -m 0644 "$APP_DIR/infra/systemd/thinksync-frontend.service" /etc/systemd/system/thinksync-frontend.service

install -m 0644 "$APP_DIR/infra/nginx/thinksync.conf" /etc/nginx/sites-available/thinksync.conf
ln -sf /etc/nginx/sites-available/thinksync.conf /etc/nginx/sites-enabled/thinksync.conf
rm -f /etc/nginx/sites-enabled/default

install -m 0644 "$APP_DIR/infra/logrotate/thinksync" /etc/logrotate.d/thinksync

systemctl daemon-reload
systemctl enable --now thinksync-backend.service
systemctl enable --now thinksync-frontend.service
systemctl enable --now nginx

nginx -t
systemctl reload nginx

