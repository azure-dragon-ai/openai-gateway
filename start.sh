#!/usr/bin/env bash
# ============================================================
#  openai-gateway launcher (Linux / macOS / Git Bash)
#
#  Usage:
#    ./start.sh                  start on config.yaml's host:port
#    ./start.sh 9000             override port
#    ./start.sh 9000 127.0.0.1   override port and host
#
#  Reuses .venv when present, otherwise creates it, installs
#  missing dependencies, then boots uvicorn.
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

PORT="${1:-}"
HOST="${2:-}"

echo "============================================"
echo " openai-gateway"
echo " dir : $(pwd)"
if [ -n "$HOST" ]; then
  echo " bind: ${HOST}:${PORT}"
else
  echo " bind: from config.yaml"
fi
echo "============================================"

# ---------- locate python ----------
PY=""
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
  echo "[info] using existing venv: .venv"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "[ERROR] Python not found. Install Python 3.10+ first."
  exit 1
fi

echo "[info] interpreter: $PY"

# ---------- create venv if needed ----------
if [ ! -x ".venv/bin/python" ]; then
  echo "[info] creating virtualenv .venv ..."
  "$PY" -m venv .venv
  PY=".venv/bin/python"
else
  PY=".venv/bin/python"
fi

# ---------- ensure dependencies ----------
if ! "$PY" -c "import fastapi, uvicorn, httpx, yaml" >/dev/null 2>&1; then
  echo "[info] installing dependencies from requirements.txt ..."
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r requirements.txt
else
  echo "[info] dependencies already satisfied."
fi

# ---------- run ----------
echo "[info] starting gateway... (Ctrl+C to stop)"
if [ -n "$HOST" ]; then
  exec "$PY" main.py --config config.yaml --host "$HOST" --port "$PORT"
else
  exec "$PY" main.py --config config.yaml
fi
