#!/bin/bash
set -e

HOST="${VIBE_HOST:-localhost}"
PORT="${VIBE_GATEWAY_PORT:-8443}"
PROMPT="${*:-Hej! Berätta kort vad du är.}"
ESCAPED=$(printf '%s' "$PROMPT" | sed 's/\\/\\\\/g; s/"/\\"/g')

DATA=$(printf '{"messages":[{"role":"user","content":"%s"}],"max_tokens":256,"stream":false}' "$ESCAPED")

exec curl -sk "https://$HOST:$PORT/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "$DATA"
