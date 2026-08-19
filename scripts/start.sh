#!/usr/bin/env bash
# scripts/start.sh — start both services locally for development
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "▶  Starting ThinkSync backend…"
cd "$ROOT_DIR/backend"
python3 -m venv .venv --upgrade-deps
source .venv/bin/activate
pip install -q -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

echo "▶  Starting ThinkSync frontend…"
cd "$ROOT_DIR/frontend"
npm install --silent
npm run dev &
FRONTEND_PID=$!

echo ""
echo "  Backend  → http://localhost:8000"
echo "  Frontend → http://localhost:3000"
echo ""
echo "Press Ctrl+C to stop both services."

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; echo 'Stopped.'" INT TERM
wait
