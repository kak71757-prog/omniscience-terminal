#!/usr/bin/env bash
# PROJECT OMNISCIENCE — entrypoint (Railway, Docker, bare metal)
#
# Railway injects $PORT and expects the process to bind 0.0.0.0.  The engine is
# stateful (open websockets, in-memory models, paper broker), so it runs as a
# single uvicorn process with no workers and no forking.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

echo "[omniscience] starting on ${HOST}:${PORT}"
echo "[omniscience] db=${OMNI_DB:-data/omniscience.db} mode=${OMNI_MODE:-PAPER} focus=${OMNI_FOCUS:-BTC-USDT-SWAP}"

exec python3 -m uvicorn server.app:app \
  --host "${HOST}" \
  --port "${PORT}" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips "*" \
  --timeout-keep-alive 75 \
  --log-level info
