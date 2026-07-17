#!/bin/bash
# Start the MinerU backend validated in docs/engine-certification.md.

set -euo pipefail

MODEL_PATH="${MINERU_MODEL_PATH:?Set MINERU_MODEL_PATH to the local model directory}"
HOST="${MINERU_HOST:-127.0.0.1}"
PORT="${MINERU_PORT:-30000}"
SERVED_MODEL_NAME="${MINERU_SERVED_MODEL_NAME:-mineru}"
GPU_MEMORY_UTILIZATION="${MINERU_GPU_MEMORY_UTILIZATION:-0.40}"
MAX_MODEL_LEN="${MINERU_MAX_MODEL_LEN:-8192}"
MAX_BATCHED_TOKENS="${MINERU_MAX_BATCHED_TOKENS:-8192}"
CONTAINER_IMAGE="${MINERU_VLLM_IMAGE:-nvcr.io/nvidia/vllm:26.03-py3}"
CONTAINER_NAME="${MINERU_CONTAINER_NAME:-ocrparser-mineru-vllm}"

if [[ ! -d "$MODEL_PATH" ]]; then
    echo "MINERU_MODEL_PATH is not a directory: $MODEL_PATH" >&2
    exit 2
fi

exec docker run --rm --gpus all \
    --name "$CONTAINER_NAME" \
    -p "$HOST:$PORT:8000" \
    -v "$MODEL_PATH:/model:ro" \
    -e MINERU_SERVED_MODEL_NAME="$SERVED_MODEL_NAME" \
    -e MINERU_GPU_MEMORY_UTILIZATION="$GPU_MEMORY_UTILIZATION" \
    -e MINERU_MAX_MODEL_LEN="$MAX_MODEL_LEN" \
    -e MINERU_MAX_BATCHED_TOKENS="$MAX_BATCHED_TOKENS" \
    "$CONTAINER_IMAGE" \
    bash -lc 'python -m pip install --no-cache-dir mineru-vl-utils==1.0.5 && exec vllm serve /model \
        --served-model-name "$MINERU_SERVED_MODEL_NAME" \
        --host 0.0.0.0 \
        --port 8000 \
        --trust-remote-code \
        --gpu-memory-utilization "$MINERU_GPU_MEMORY_UTILIZATION" \
        --max-model-len "$MINERU_MAX_MODEL_LEN" \
        --max-num-batched-tokens "$MINERU_MAX_BATCHED_TOKENS" \
        --enforce-eager \
        --no-enable-prefix-caching \
        --mm-processor-cache-gb 0 \
        --logits-processors mineru_vl_utils:MinerULogitsProcessor'
