#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
# Isolated from production uvicorn on :8000 / app.db
export DATABASE_URL="${DATABASE_URL:-sqlite:///./app.dev.db}"
export CORS_ORIGINS="${CORS_ORIGINS:-http://localhost:3001,https://devtest.narxoz.kz}"
export PUBLIC_APP_BASE_URL="${PUBLIC_APP_BASE_URL:-https://devtest.narxoz.kz}"
exec "${ROOT}/venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port 8001 --reload --proxy-headers
