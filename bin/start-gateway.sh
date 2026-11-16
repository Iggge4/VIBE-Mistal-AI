#!/bin/bash
set -e

DATA_DIR="${SNAP_DATA:-$HOME/.local/share/vibe-mistral-ai}"
export VIBE_TLS_CERT="$DATA_DIR/certs/cert.pem"
export VIBE_TLS_KEY="$DATA_DIR/certs/key.pem"
export VIBE_GATEWAY_PORT="${VIBE_GATEWAY_PORT:-8443}"
export VIBE_BACKEND="${VIBE_BACKEND:-http://127.0.0.1:8080}"

exec python3 "$(dirname "$(readlink -f "$0")")/../gateway/gateway.py"
