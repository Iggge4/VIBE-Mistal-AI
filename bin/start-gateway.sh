#!/bin/bash
set -e

DATA_DIR="${SNAP_DATA:-$HOME/.local/share/vibe-mistral-ai}"
export VIBE_TLS_CERT="$DATA_DIR/certs/cert.pem"
export VIBE_TLS_KEY="$DATA_DIR/certs/key.pem"
export VIBE_GATEWAY_PORT="${VIBE_GATEWAY_PORT:-8443}"
export VIBE_BACKEND="${VIBE_BACKEND:-http://127.0.0.1:8080}"
export VIBE_ADMIN_PORT="${VIBE_ADMIN_PORT:-8444}"
export VIBE_APPROVAL_TIMEOUT="${VIBE_APPROVAL_TIMEOUT:-120}"
export VIBE_REQUIRE_APPROVAL="${VIBE_REQUIRE_APPROVAL:-1}"
export VIBE_STATE_FILE="$DATA_DIR/usage.json"

exec python3 "$(dirname "$(readlink -f "$0")")/../gateway/gateway.py"
