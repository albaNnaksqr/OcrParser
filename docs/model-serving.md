# Model Serving Examples

English | [中文](model-serving.zh-CN.md)

OcrParser calls OCR/model services through HTTP APIs. It does not ship model
weights and does not start model servers automatically.

Before enabling a real engine in production, review the dated
[engine certification matrix](engine-certification.md) and the independent
[third-party license audit](third-party-licenses.md).

The examples below show the shape of the services the parser expects. Replace
paths, ports, model names, and runtime flags with values appropriate for your
environment.

## Endpoint Shape

Most parser engines expect an OpenAI-compatible chat/completions endpoint:

```text
http://YOUR_MODEL_ENDPOINT:PORT/v1/chat/completions
```

CLI flags usually use host and port separately:

```bash
python -m ocr_parser \
  --input_file /path/to/file.pdf \
  --output_dir /path/to/output \
  --engine dotsocr \
  --ip YOUR_MODEL_ENDPOINT \
  --port 13080 \
  --model_name DotsOCR
```

If your endpoint requires an API key, pass it through your local environment or
the platform model profile. Do not commit keys to this repository.

## DotsOCR-Style Endpoint

Use this when your DotsOCR-compatible model is already exposed as an
OpenAI-compatible service:

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

For high-capacity endpoints, benchmark before raising concurrency:

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

MinerU-style parsing uses VLM calls for both layout and recognition. Keep an
API lane available for layout while recognition is active:

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

The certified smoke path for
[MinerU2.5-Pro-2604-1.2B](https://huggingface.co/opendatalab/MinerU2.5-Pro-2604-1.2B)
uses vLLM with `mineru-vl-utils` and `MinerULogitsProcessor`:

```bash
MINERU_MODEL_PATH=/models/MinerU2.5-Pro-2604-1.2B \
MINERU_PORT=30090 \
bash start_mineru_server.sh
```

The script defaults to loopback, the exact NVIDIA vLLM image used for the
2026-07-17 smoke test, a 0.40 GPU-memory fraction, and the required logits
processor. Build a pinned image instead of installing packages at startup for
production. A generic SGLang service returned healthy HTTP responses but
semantically invalid repeated-token output in this certification run, so it is
not an approved backend for this model revision.

## PaddleOCR-VL-Style Endpoint

PaddleOCR-VL-style parsing uses two services:

- a layout detection endpoint, configured with `--layout_detection_url`;
- an OpenAI-compatible VLM endpoint for block recognition.

Parser command:

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

The v0.3.1 ARM64 certification path uses the pinned SGLang recipe under
[`deploy/engines/paddleocr-vl`](../deploy/engines/paddleocr-vl/README.md).
The recipe records the base-image digest, SGLang/kernel/runtime versions, model
revision, and weight checksum without bundling model weights. Build and record
the resulting registry RepoDigest before certification. The currently validated
base combination uses the explicitly limited
`FLASHINFER_DISABLE_VERSION_CHECK=1` strategy; a local image ID without a
RepoDigest remains **Verified / limited**.

Generic VLM service shape:

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

The layout detection service is model-specific. Expose it separately and pass
its URL through `--layout_detection_url`.

## Control UI Model Profiles

For repeated jobs, store endpoint and default parser options in a model profile
instead of typing them for every submission.

Recommended public pattern:

- keep endpoint host, port, engine, and model name in the profile;
- store API keys in environment variables when possible;
- keep profile defaults conservative;
- raise concurrency only after a benchmark on representative PDFs.

Example profile defaults:

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

## Tuning Rules Of Thumb

- Start with low concurrency on a new endpoint.
- Raise page concurrency only if API wait time is low and error rate is stable.
- Raise file concurrency for many-small-PDF workloads.
- Keep two-stage layout and recognition limits separate.
- Watch output quality as well as throughput; faster empty output is not useful.
- Record endpoint resource budget next to benchmark results.
