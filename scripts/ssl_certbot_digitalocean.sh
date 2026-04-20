#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${DOMAIN:-thinksync.art}"
EMAIL="${EMAIL:-admin@thinksync.art}"
DO_INI="${DO_INI:-/etc/letsencrypt/digitalocean.ini}"

apt-get update
apt-get install -y --no-install-recommends certbot python3-certbot-dns-digitalocean

chmod 600 "$DO_INI"

certbot certonly \
  --dns-digitalocean \
  --dns-digitalocean-credentials "$DO_INI" \
  -d "$DOMAIN" \
  -d "*.$DOMAIN" \
  --agree-tos \
  --email "$EMAIL" \
  --non-interactive

systemctl enable --now certbot.timer
systemctl reload nginx

