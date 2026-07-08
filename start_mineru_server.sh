#!/bin/bash
# Start sglang server with MinerU2.5-Pro model
# Memory limit: ~50GB out of 119GB total => mem-fraction-static=0.42

MODEL_PATH="/home/ocr_user/workspace/models/MinerU2.5-Pro-2604-1.2B"
PORT=30000

conda run -n sglang python -m sglang.launch_server \
    --model-path "$MODEL_PATH" \
    --port $PORT \
    --host 0.0.0.0 \
    --mem-fraction-static 0.42 \
    --trust-remote-code \
    --chat-template qwen2-vl \
    2>&1
