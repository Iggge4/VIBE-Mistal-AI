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

exec "$SERVER" \
    -m "$MODEL" \
    --host 127.0.0.1 \
    --port 8080 \
    -c "${VIBE_CTX:-8192}" \
    -t "$THREADS"
