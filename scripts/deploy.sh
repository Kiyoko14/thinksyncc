#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/thinksync}"
BRANCH="${BRANCH:-main}"

cd "$APP_DIR"
git fetch --all --prune
git checkout "$BRANCH"
git pull --ff-only

mkdir -p /var/log/thinksync

cd "$APP_DIR/backend"
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
deactivate

cd "$APP_DIR/frontend"
npm ci
npm run build

systemctl daemon-reload
systemctl restart thinksync-backend.service
systemctl restart thinksync-frontend.service
systemctl reload nginx
