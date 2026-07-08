#!/bin/bash
# Download PP-DocLayoutV2 weights (safetensors, PyTorch-compatible) from HuggingFace.
# Usage: bash download_model.sh [local_dir]

set -e

LOCAL_DIR="${1:-/home/ocr_user/workspace/models/PP-DocLayoutV2}"

echo "Downloading PP-DocLayoutV2 to: $LOCAL_DIR"
python - <<EOF
from huggingface_hub import snapshot_download
path = snapshot_download(
    repo_id="PaddlePaddle/PP-DocLayoutV2_safetensors",
    local_dir="$LOCAL_DIR",
)
print("Done:", path)
EOF
