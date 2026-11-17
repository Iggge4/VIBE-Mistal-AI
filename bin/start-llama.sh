#!/bin/bash
set -e

DATA_DIR="${SNAP_DATA:-$HOME/.local/share/vibe-mistral-ai}"
MODEL="$DATA_DIR/models/mistral-7b-instruct-v0.2.Q4_K_M.gguf"
SERVER="$SNAP/usr/local/bin/llama-server"

if [ ! -x "$SERVER" ]; then
    SERVER="$(dirname "$(readlink -f "$0")")/../llama-server"
fi

export LD_LIBRARY_PATH="$SNAP/usr/local/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

if [ ! -f "$MODEL" ]; then
    echo "[llamaserver] Modellen saknas: $MODEL" >&2
    echo "[llamaserver] Kör: sudo snap run vibe-mistral-ai.setup-model" >&2
    exit 1
fi

THREADS="${VIBE_THREADS:-}"
if [ -z "$THREADS" ]; then
    THREADS=$(lscpu -p=Core 2>/dev/null | grep -v '^#' | sort -u | wc -l)
    if ! [ "$THREADS" -ge 1 ] 2>/dev/null; then
        THREADS=$(nproc)
    fi
fi

SETTINGS="$DATA_DIR/settings.json"
MODE="cpu"
NGL=0
if [ -f "$SETTINGS" ]; then
    MODE_NGL=$(python3 - "$SETTINGS" <<'PYREAD'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        d = json.load(fh)
except Exception:
    print("cpu 0")
    sys.exit()
mode = "gpu" if d.get("compute_mode") == "gpu" else "cpu"
try:
    ngl = int(d.get("gpu_layers", 0))
except (TypeError, ValueError):
    ngl = 0
print(mode, ngl)
PYREAD
) && MODE=$(echo "$MODE_NGL" | cut -d' ' -f1) && NGL=$(echo "$MODE_NGL" | cut -d' ' -f2)
fi

echo "[llamaserver] Startar med $THREADS trådar, kontext ${VIBE_CTX:-8192}"

ARGS=( -m "$MODEL" --host 127.0.0.1 --port 8080 -c "${VIBE_CTX:-8192}" -t "$THREADS" )
if [ "$MODE" = "gpu" ] && [ "$NGL" -ge 1 ] 2>/dev/null; then
    echo "[llamaserver] GPU-läge: $NGL lager på GPU"
    ARGS+=( -ngl "$NGL" )
else
    echo "[llamaserver] CPU-läge"
fi

exec "$SERVER" "${ARGS[@]}"
