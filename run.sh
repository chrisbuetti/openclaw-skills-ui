#!/usr/bin/env bash
# Launch the OpenClaw Manager server.
set -euo pipefail

cd "$(dirname "$0")"

# Activate venv if present
if [[ -d "venv" ]]; then
  source venv/bin/activate
fi

exec uvicorn main:app --host 127.0.0.1 --port "${PORT:-8000}" --reload
