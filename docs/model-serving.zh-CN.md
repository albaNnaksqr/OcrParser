# 模型服务示例

[English](model-serving.md) | 中文

OcrParser 通过 HTTP API 调用 OCR/model services。它不附带模型权重，
也不会自动启动模型服务器。

生产启用真实引擎前，应核对带日期的[引擎认证矩阵](engine-certification.zh-CN.md)
和独立的[第三方许可证审计](third-party-licenses.zh-CN.md)。

下面的示例展示 parser 期望的服务形态。请根据你的环境替换 paths、ports、
model names 和 runtime flags。

## Endpoint 形态

大多数 parser engines 期望 OpenAI-compatible chat/completions endpoint：

```text
http://YOUR_MODEL_ENDPOINT:PORT/v1/chat/completions
```

CLI flags 通常分开传 host 和 port：

```bash
python -m ocr_parser \
  --input_file /path/to/file.pdf \
  --output_dir /path/to/output \
  --engine dotsocr \
  --ip YOUR_MODEL_ENDPOINT \
  --port 13080 \
  --model_name DotsOCR
```

如果 endpoint 需要 API key，请通过本地环境变量或 platform model profile 传入。
不要把 key 提交到仓库中。

## DotsOCR-Style Endpoint

当你的 DotsOCR-compatible 模型已经暴露为 OpenAI-compatible service 时，可以这样使用：

```bash
python -m ocr_parser \
  --input_dir /shared/ocr-input \
  --output_dir /shared/ocr-output \
  --engine dotsocr \
  --ip YOUR_DOTSOCR_ENDPOINT \
  --port 13080 \
  --model_name DotsOCR \
  --file_concurrency 4 \
  --page_concurrency 16 \
  --api_concurrency_start 16 \
  --api_concurrency_max 16
```

对于高容量 endpoint，提高并发前先做 benchmark：

```bash
python3 tools/run_performance_baseline.py \
  --input-dir /tmp/ocr-benchmark-pdfs \
  --output-root /tmp/ocr-benchmark-results \
  --variant current=. \
  --run-mode directory \
  --engine dotsocr \
  --ip YOUR_DOTSOCR_ENDPOINT \
  --port 13080 \
  --model-name DotsOCR \
  --file-concurrency 4 \
  --page-concurrency 16
```

## MinerU-Style Endpoint

MinerU-style parsing 对 layout 和 recognition 都使用 VLM calls。
当 recognition 活跃时，应为 layout 保留 API lane：

```bash
python -m ocr_parser \
  --input_dir /shared/ocr-input \
  --output_dir /shared/ocr-output \
  --engine mineru \
  --ip YOUR_MINERU_ENDPOINT \
  --port 30090 \
  --model_name mineru \
  --file_concurrency 2 \
  --page_concurrency 2 \
  --api_concurrency_start 4 \
  --api_concurrency_max 4 \
  --block_concurrency 4 \
  --mineru_layout_reserved_api_slots 1 \
  --mineru_recognition_api_concurrency 3
```

已完成 smoke 验证的
[MinerU2.5-Pro-2604-1.2B](https://huggingface.co/opendatalab/MinerU2.5-Pro-2604-1.2B)
路径使用 vLLM、`mineru-vl-utils` 和 `MinerULogitsProcessor`：

```bash
MINERU_MODEL_PATH=/models/MinerU2.5-Pro-2604-1.2B \
MINERU_PORT=30090 \
bash start_mineru_server.sh
```

脚本默认只监听 loopback，固定为 2026-07-17 smoke 使用的 NVIDIA vLLM image，
GPU memory fraction 为 0.40，并加载必需的 logits processor。生产环境应构建固定依赖
的 image，避免每次启动临时安装包。本次认证中，通用 SGLang 服务虽然 HTTP 健康，
但返回了语义无效的重复 token，因此不认证该模型 revision 的 SGLang backend。

## PaddleOCR-VL-Style Endpoint

PaddleOCR-VL-style parsing 使用两个服务：

- layout detection endpoint，通过 `--layout_detection_url` 配置；
- OpenAI-compatible VLM endpoint，用于 block recognition。

Parser command：

```bash
python -m ocr_parser \
  --input_dir /shared/ocr-input \
  --output_dir /shared/ocr-output \
  --engine paddleocr-vl \
  --ip YOUR_PADDLE_VLM_ENDPOINT \
  --port 30001 \
  --model_name paddleocr-vl \
  --layout_detection_url http://YOUR_LAYOUT_ENDPOINT:30002 \
  --file_concurrency 2 \
  --page_concurrency 2 \
  --api_concurrency_start 4 \
  --api_concurrency_max 4 \
  --block_concurrency 4 \
  --paddle_layout_concurrency 1 \
  --paddle_block_backpressure_high_watermark 8 \
  --paddle_block_backpressure_low_watermark 2
```

v0.3.1 ARM64 认证使用
[`deploy/engines/paddleocr-vl`](../deploy/engines/paddleocr-vl/README.md)中的固定
SGLang 配方。该配方记录 base image digest、SGLang/kernel/runtime 版本、模型
revision 与权重 checksum，但不打包模型权重。认证前必须构建并记录 registry
RepoDigest。当前固定 base 组合采用显式受限的
`FLASHINFER_DISABLE_VERSION_CHECK=1` 策略；只有本地 image ID、没有 RepoDigest 时仍为
**Verified / limited**。

通用 VLM service 形态：

```bash
python -m sglang.launch_server \
  --model-path /models/PaddleOCR-VL \
  --served-model-name paddleocr-vl \
  --host 0.0.0.0 \
  --port 30001 \
  --trust-remote-code \
  --context-length 32768 \
  --mem-fraction-static 0.40 \
  --attention-backend triton \
  --sampling-backend pytorch
```

Layout detection service 与具体模型相关。请单独暴露该服务，并通过
`--layout_detection_url` 传入 URL。

## Control UI Model Profiles

对于重复任务，建议把 endpoint 和默认 parser options 存进 model profile，
而不是每次提交时手动输入。

推荐的公开模式：

- 在 profile 中保存 endpoint host、port、engine 和 model name；
- 尽量通过环境变量存储 API keys；
- profile defaults 保持保守；
- 只在代表性 PDF 上 benchmark 后提高并发。

Profile defaults 示例：

```json
{
  "engine": "dotsocr",
  "ip": "YOUR_DOTSOCR_ENDPOINT",
  "port": 13080,
  "model_name": "DotsOCR",
  "page_concurrency": 16,
  "extra_args": {
    "file_concurrency": 4,
    "api_concurrency_start": 16,
    "api_concurrency_max": 16,
    "num_cpu_workers": 8,
    "max_retries": 1,
    "timeout": 180,
    "skip_blank_pages": true
  }
}
```

## 调优经验

- 新 endpoint 先从低并发开始。
- 只有在 API wait time 较低且 error rate 稳定时，才提高 page concurrency。
- 大量小 PDF workload 可以提高 file concurrency。
- Two-stage layout 和 recognition limits 要分开配置。
- 同时关注输出质量和吞吐；更快地产生空输出没有意义。
- 在 benchmark 结果旁记录 endpoint resource budget。
