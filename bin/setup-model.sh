#!/bin/bash
set -e

DATA_DIR="${SNAP_DATA:-$HOME/.local/share/vibe-mistral-ai}"
MODEL_DIR="$DATA_DIR/models"
TARGET="$MODEL_DIR/mistral-7b-instruct-v0.2.Q4_K_M.gguf"
MODEL_URL="https://huggingface.co/TheBloke/Mistral-7B-Instruct-v0.2-GGUF/resolve/main/mistral-7b-instruct-v0.2.Q4_K_M.gguf"

mkdir -p "$MODEL_DIR"

if [ -f "$TARGET" ]; then
    echo "[setup-model] Modellen finns redan: $TARGET"
    exit 0
fi

echo "[setup-model] Laddar ner Mistral 7B Instruct (Q4_K_M, ~4,4 GB)..."
echo "[setup-model] Källa: $MODEL_URL"
curl -fL --progress-bar -o "$TARGET.part" "$MODEL_URL"
mv "$TARGET.part" "$TARGET"
echo "[setup-model] Klar: $TARGET"
