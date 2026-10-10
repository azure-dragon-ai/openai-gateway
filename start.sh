#!/usr/bin/env bash
# ============================================================
#  openai-gateway launcher (Linux / macOS / Git Bash)
#
#  Usage:
#    ./start.sh                    start on config.yaml's port
#    ./start.sh 8801               override port
#    ./start.sh 8801 127.0.0.1     override port and host
#
#  Strategy: prefer an interpreter that ALREADY has the deps, so no
#  network install is needed. Only bootstrap if none is found.
#  Set PYTHON_EXE=/path/to/python to force a specific interpreter.
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

PORT="${1:-}"
HOST="${2:-}"

echo "============================================"
echo " openai-gateway"
echo " dir : $(pwd)"
echo "============================================"
echo

DEPS_CHECK='import fastapi, uvicorn, httpx, yaml'

# ---------- step 1: find a python that already has the deps ----------
echo "[1/3] Looking for a Python that already has dependencies ..."

have_deps() { "$@" -c "$DEPS_CHECK" >/dev/null 2>&1; }

PY=""
for cand in \
    ".venv/bin/python" \
    ".venv/Scripts/python.exe" \
    "$HOME/miniconda3/bin/python" \
    "$HOME/anaconda3/bin/python" \
    "$HOME/miniforge3/bin/python" \
    "$HOME/miniconda3/python.exe" \
    "$HOME/anaconda3/python.exe" \
    "$HOME/miniforge3/python.exe" \
    "/opt/conda/bin/python" \
    "${PYTHON_EXE:-}" \
    "$(command -v python3 || true)" \
    "$(command -v python || true)"
do
    [ -n "$cand" ] || continue
    [ -x "$cand" ] || continue
    if have_deps "$cand"; then
        PY="$cand"
        echo "      OK  $cand"
        break
    fi
done

if [ -n "$PY" ]; then
    echo
    echo "[2/3] Dependencies already present - skipping install."
else
    echo "      none found - will bootstrap instead."
    echo
    echo "[2/3] Bootstrapping environment ..."

    # pick any usable base interpreter
    BASE=""
    for cand in "${PYTHON_EXE:-}" python3 python; do
        [ -n "$cand" ] || continue
        if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then
            BASE="$cand"
            break
        fi
    done
    if [ -z "$BASE" ]; then
        echo "[ERROR] No Python found. Install Python 3.10+ or set PYTHON_EXE="
        exit 1
    fi
    echo "      base interpreter: $BASE"

    # venv layout differs: Scripts/python.exe on Windows, bin/python elsewhere
    if [ -x ".venv/bin/python" ]; then
        echo "      reusing virtualenv .venv"
        PY=".venv/bin/python"
    elif [ -x ".venv/Scripts/python.exe" ]; then
        echo "      reusing virtualenv .venv"
        PY=".venv/Scripts/python.exe"
    else
        echo "      creating virtualenv .venv ..."
        "$BASE" -m venv .venv
        if   [ -x ".venv/bin/python" ];          then PY=".venv/bin/python"
        elif [ -x ".venv/Scripts/python.exe" ];   then PY=".venv/Scripts/python.exe"
        else echo "[ERROR] venv created but no interpreter found inside."; exit 1
        fi
    fi

    echo "      installing requirements.txt ..."
    installed=0
    for mirror in \
        "https://mirrors.aliyun.com/pypi/simple" \
        "https://pypi.tuna.tsinghua.edu.cn/simple" \
        "https://pypi.org/simple"
    do
        echo "      trying $mirror"
        if "$PY" -m pip install -r requirements.txt -i "$mirror" --trusted-host "${mirror#https://}" --trusted-host "${mirror%%/*}"; then
            installed=1
            break
        fi
    done
    if [ "$installed" -ne 1 ]; then
        echo "[ERROR] All pip mirrors failed."
        exit 1
    fi
fi

# ---------- step 3: resolve port, warn if busy, serve ----------
if [ -z "$PORT" ]; then
    PORT="$("$PY" -c "import yaml; print(yaml.safe_load(open('config.yaml', encoding='utf-8'))['server']['port'])" 2>/dev/null || echo 8800)"
fi

echo
echo "[3/3] Starting gateway on port ${PORT} - Ctrl+C to stop."
echo "      health: http://127.0.0.1:${PORT}/health"
echo

if [ -n "$HOST" ]; then
    exec "$PY" main.py --config config.yaml --host "$HOST" --port "$PORT"
else
    exec "$PY" main.py --config config.yaml --port "$PORT"
fi
