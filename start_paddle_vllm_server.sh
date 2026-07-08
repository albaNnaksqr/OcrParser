#!/bin/bash
# Start PaddleOCR-VL as an OpenAI-compatible vLLM service.

MODEL_PATH="${PADDLE_VLM_MODEL_PATH:-/home/ocr_user/workspace/models/PaddleOCR-VL-1.5}"
HOST="${PADDLE_VLM_HOST:-0.0.0.0}"
PORT="${PADDLE_VLM_PORT:-30001}"
SERVED_MODEL_NAME="${PADDLE_VLM_SERVED_MODEL_NAME:-paddleocr-vl}"
GPU_MEMORY_UTILIZATION="${PADDLE_VLM_GPU_MEMORY_UTILIZATION:-0.35}"
MAX_MODEL_LEN="${PADDLE_VLM_MAX_MODEL_LEN:-32768}"
DTYPE="${PADDLE_VLM_DTYPE:-bfloat16}"
CONDA_BIN="${CONDA_BIN:-conda}"
CONDA_ENV="${PADDLE_VLM_CONDA_ENV:-vllm}"

export PYTHONNOUSERSITE="${PYTHONNOUSERSITE:-1}"

CMD=(
    "$CONDA_BIN" run -n "$CONDA_ENV" --no-capture-output
    python -m vllm.entrypoints.openai.api_server
    --model "$MODEL_PATH"
    --served-model-name "$SERVED_MODEL_NAME"
    --host "$HOST"
    --port "$PORT"
    --trust-remote-code
    --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
    --max-model-len "$MAX_MODEL_LEN"
    --dtype "$DTYPE"
)

if [[ "${PADDLE_VLM_ENFORCE_EAGER:-1}" != "0" ]]; then
    CMD+=(--enforce-eager)
fi

exec "${CMD[@]}"
