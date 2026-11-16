#!/usr/bin/env bash
set -euo pipefail

SNAP_NAME="vibe-mistral-ai"

say() { printf '\n==> %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "Kör som root: sudo ./install.sh" >&2
    exit 1
fi

say "Kontrollerar snapd..."
if ! command -v snap >/dev/null 2>&1; then
    apt-get update && apt-get install -y snapd
fi
systemctl enable --now snapd.socket 2>/dev/null || true
snap install core 2>/dev/null || snap refresh core || true

say "Installerar byggverktyg (snapcraft + lxd)..."
snap install snapcraft --classic
snap install lxd || true
lxd init --auto 2>/dev/null || true

say "Bygger snap-paketet (kan ta lång tid första gången, llama.cpp kompileras)..."
if ! snapcraft pack --use-lxd 2>/dev/null; then
    snapcraft pack
fi

SNAP_FILE=$(ls -t "${SNAP_NAME}"_*.snap 2>/dev/null | head -n1)
if [ -z "${SNAP_FILE}" ]; then
    echo "Hittade inget .snap-paket efter bygget" >&2
    exit 1
fi

say "Installerar ${SNAP_FILE} lokalt..."
snap install --dangerous "${SNAP_FILE}"

say "Laddar ner Mistral-modellen (~4,4 GB)..."
snap run "${SNAP_NAME}".setup-model

say "Startar tjänster..."
snap start --enable "${SNAP_NAME}".llamaserver || true
snap start --enable "${SNAP_NAME}".gateway || true

sleep 3
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
IP="${IP:-127.0.0.1}"

cat <<EOF

============================================================
  Klart! Krypterat HTTPS-API körs nu:

    https://${IP}:8443/v1/chat/completions

  Certifikatet är självsignerat. Klienter använder
  curl -k/--insecure, eller litar på certet:
    /var/snap/${SNAP_NAME}/common/certs/cert.pem

  Testa lokalt:
    vibe-mistral-ai.ask "Hej, vem är du?"

  Testa från annan enhet:
    curl -sk https://${IP}:8443/v1/chat/completions \\
      -H 'Content-Type: application/json' \\
      -d '{"messages":[{"role":"user","content":"Hej!"}]}'
============================================================
EOF
