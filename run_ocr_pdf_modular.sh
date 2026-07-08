#!/bin/bash

set -euo pipefail

# 1. 新的模块化入口
PYTHON_SCRIPT_PATH="./ocr_parser_cli.py"

# 2. 默认输入
INPUT_DIR="${INPUT_DIR:-./data/input}"

# 3. 默认输出
OUTPUT_DIR="${OUTPUT_DIR:-./data/output}"

# 4. 认证（可选，通过环境变量 API_KEY 注入）
API_KEY="${API_KEY:-}"

# 5. 可选能力
ADD_PAGE_TAG="false"
ENABLE_TABLE_REPARSE="false"
ENABLE_TABLE_SCREENSHOT="false"
SAVE_PAGE_JSON="true"
SAVE_PAGE_LAYOUT="false"
FILTER_AUTHOR_BLOCKS="false"
TRIM_FIRST_PAGE_SUMMARY="false"
NORMALIZE_SUPERSCRIPT="false"

# 6. 模型服务
IP="${OCR_SERVER_IP:-127.0.0.1}"
PORT="${OCR_SERVER_PORT:-8000}"
MODEL_NAME="${OCR_MODEL_NAME:-DotsOCR}"

# 7. 并发与性能
NUM_CPU_WORKERS=8
PAGE_CONCURRENCY=16
MD_GEN_CONCURRENCY=8
QUEUE_SIZE=300
TIMEOUT=900

if [ ! -f "$PYTHON_SCRIPT_PATH" ]; then
    echo "错误: 未找到入口脚本: $PYTHON_SCRIPT_PATH"
    exit 1
fi

ADDITIONAL_ARGS=("$@")

if [ "$ADD_PAGE_TAG" = "true" ]; then
    ADDITIONAL_ARGS+=(--add_page_tag)
fi
if [ "$ENABLE_TABLE_REPARSE" = "true" ]; then
    ADDITIONAL_ARGS+=(--enable_table_reparse)
fi
if [ "$ENABLE_TABLE_SCREENSHOT" = "true" ]; then
    ADDITIONAL_ARGS+=(--enable_table_screenshot)
fi
if [ "$SAVE_PAGE_JSON" = "true" ]; then
    ADDITIONAL_ARGS+=(--save_page_json)
fi
if [ "$SAVE_PAGE_LAYOUT" = "true" ]; then
    ADDITIONAL_ARGS+=(--save_page_layout)
fi
if [ "$FILTER_AUTHOR_BLOCKS" = "true" ]; then
    ADDITIONAL_ARGS+=(--filter_author_blocks)
fi
if [ "$TRIM_FIRST_PAGE_SUMMARY" = "true" ]; then
    ADDITIONAL_ARGS+=(--trim_first_page_summary)
fi
if [ "$NORMALIZE_SUPERSCRIPT" = "true" ]; then
    ADDITIONAL_ARGS+=(--normalize_superscript)
fi

echo "================================================="
echo " OCR Modular PDF Task"
echo "================================================="
echo "入口脚本: $PYTHON_SCRIPT_PATH"
echo "输入目录: $INPUT_DIR"
echo "输出目录: $OUTPUT_DIR"
echo "模型服务: $IP:$PORT"
echo "-------------------------------------------------"

CMD=(
  python "$PYTHON_SCRIPT_PATH"
  --input_dir "$INPUT_DIR"
  --output_dir "$OUTPUT_DIR"
  --ip "$IP"
  --port "$PORT"
  --model_name "$MODEL_NAME"
  --timeout "$TIMEOUT"
  --num_cpu_workers "$NUM_CPU_WORKERS"
  --page_concurrency "$PAGE_CONCURRENCY"
  --md_gen_concurrency "$MD_GEN_CONCURRENCY"
  --queue_size "$QUEUE_SIZE"
  --skip_blank_pages
)

if [ -n "$API_KEY" ]; then
  CMD+=(--api_key "$API_KEY")
fi

CMD+=("${ADDITIONAL_ARGS[@]}")

"${CMD[@]}"

echo "================================================="
echo " 完成"
echo "================================================="
