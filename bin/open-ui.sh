#!/bin/bash
set -e

HOST="${VIBE_HOST:-localhost}"
PORT="${VIBE_GATEWAY_PORT:-8443}"

exec xdg-open "https://$HOST:$PORT/"
