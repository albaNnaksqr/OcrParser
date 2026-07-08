# Feature Guide

English | [中文](features.zh-CN.md)

OcrParser is both a parser library/CLI and a lightweight distributed OCR
platform. The single-machine path is useful for development and small batches;
the platform path is useful when jobs are long-running enough that scheduling,
recovery, and visibility matter.

## Parser CLI

- Parse one PDF with `--input_file`.
- Parse a directory with `--input_dir`.
- Preserve directory structure by default for directory jobs.
- Process manifest shards with `--input_manifest` for platform-created work.
- Resume local runs by checking completed output artifacts.
- Emit per-file status sidecars and job/runtime events.
- Write Markdown, page JSON, and engine-native artifacts.

## Engine Adapters

| Engine | CLI value | Shape |
| --- | --- | --- |
| DotsOCR-style | `dotsocr` | Full-page VLM extraction with Markdown/JSON post-processing. |
| MinerU-style | `mineru` | Two-stage VLM layout and block recognition pipeline. |
| PaddleOCR-VL-style | `paddleocr-vl` | Layout detection service plus VLM block recognition. |
| Native OpenAI-compatible | `native_openai` | Generic OpenAI-compatible OCR/text extraction endpoint. |

The parser treats engines as adapters below a shared scheduling layer. This
lets the same file/page/resource controls work across multiple services without
forcing every engine to use identical internals.

## Throughput Controls

| Control | Purpose |
| --- | --- |
| `--file_concurrency` | Run multiple PDFs from one directory job concurrently. |
| `--page_concurrency` | Run multiple pages from a PDF concurrently. |
| `--api_concurrency_start` / `--api_concurrency_max` | Bound OpenAI-compatible model calls. |
| `--num_cpu_workers` | Bound CPU-heavy rendering and post-processing work. |
| `--md_gen_concurrency` | Bound Markdown/output generation work. |
| `--render_concurrency` / `--encode_concurrency` / `--postprocess_concurrency` | Bound resource-specific local lanes when configured. |

DotsOCR-style services may benefit from high API concurrency if the endpoint has
enough backend capacity. MinerU and PaddleOCR-VL style engines usually need
stage-specific limits first, because layout, block creation, and recognition can
otherwise become unbalanced.

## Two-Stage Engine Controls

MinerU-style controls:

- `--mineru_layout_reserved_api_slots`
- `--mineru_recognition_api_concurrency`
- `--mineru_min_block_area_ratio`
- `--mineru_max_blocks_per_page`
- `--mineru_skip_visual_block_recognition`

PaddleOCR-VL-style controls:

- `--layout_detection_url`
- `--paddle_layout_concurrency`
- `--paddle_block_backpressure_high_watermark`
- `--paddle_block_backpressure_low_watermark`
- `--block_concurrency`

The goal is to keep layout generation, block recognition, and VLM calls bounded
and observable instead of letting a fast upstream stage create an uncontrolled
recognition backlog.

## Platform Features

- FastAPI control API and static control UI.
- Model profiles for endpoint, engine, model name, and default parser options.
- Worker registration and heartbeat.
- Worker readiness and shared-path visibility.
- Distributed folder scan and manifest creation.
- Static shard scheduling.
- Stale shard reclamation through leases.
- Job, shard, scan unit, and worker summaries.
- Terminal-safe throughput and status summaries.
- Deployment Doctor and read-only production preflight checks.
- Separate production installers for control and worker roles.

## Public Snapshot Boundaries

The repository intentionally does not include:

- model weights;
- private PDFs or production datasets;
- production endpoint URLs;
- API keys;
- host-specific environment files;
- runtime databases, logs, or output artifacts.

Use the mock OCR service for control-flow validation, then connect your own
OpenAI-compatible OCR/model endpoint for real parsing.
