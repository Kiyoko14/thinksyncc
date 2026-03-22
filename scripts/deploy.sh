#!/usr/bin/env bash
# scripts/deploy.sh — build and deploy via Docker Compose
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/infra/docker-compose.yml"

echo "▶  Building ThinkSync images…"
docker compose -f "$COMPOSE_FILE" build --no-cache

echo "▶  Deploying ThinkSync…"
docker compose -f "$COMPOSE_FILE" up -d

echo ""
echo "✓  ThinkSync deployed."
docker compose -f "$COMPOSE_FILE" ps
