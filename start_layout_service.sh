#!/bin/bash
# Start the PP-DocLayoutV2 layout detection service (runs in the vllm conda env).

MODEL_PATH="${LAYOUT_MODEL_PATH:-/home/ocr_user/workspace/models/PP-DocLayoutV2}"
PORT="${LAYOUT_PORT:-30002}"
DEVICE="${LAYOUT_DEVICE:-cuda}"
CONF="${LAYOUT_CONF:-0.3}"
CONDA_BIN="${CONDA_BIN:-conda}"

export LAYOUT_MODEL_PATH="$MODEL_PATH"
export LAYOUT_PORT="$PORT"
export LAYOUT_DEVICE="$DEVICE"
export LAYOUT_CONF="$CONF"
export PYTHONNOUSERSITE="${PYTHONNOUSERSITE:-1}"

SERVICE_DIR="$(cd "$(dirname "$0")/services/layout_detection" && pwd)"

exec "$CONDA_BIN" run -n vllm --no-capture-output \
    python "$SERVICE_DIR/server.py"
