# 功能指南

[English](features.md) | 中文

OcrParser 既是 parser library/CLI，也是一个轻量级分布式 OCR 平台。
单机路径适合开发和小批量任务；当 job 运行时间足够长，调度、恢复和可见性开始重要时，
platform 路径更有价值。

## Parser CLI

- 使用 `--input_file` 解析单个 PDF。
- 使用 `--input_dir` 解析一个目录。
- 目录任务默认保留输入目录结构。
- 使用 `--input_manifest` 处理 platform 创建的 manifest shard。
- 通过检查已完成输出产物恢复本地运行。
- 生成每个文件的 status sidecar 和 job/runtime events。
- 写出 Markdown、page JSON 和 engine-native artifacts。

## Engine Adapters

| Engine | CLI value | 形态 |
| --- | --- | --- |
| DotsOCR-style | `dotsocr` | 全页 VLM 抽取，并进行 Markdown/JSON 后处理。 |
| MinerU-style | `mineru` | 两阶段 VLM layout 和 block recognition pipeline。 |
| PaddleOCR-VL-style | `paddleocr-vl` | Layout detection service 加 VLM block recognition。 |
| Native OpenAI-compatible | `native_openai` | 通用 OpenAI-compatible OCR/text extraction endpoint。 |

Parser 把 engine 视为共享调度层之下的 adapter。这样同一套 file/page/resource
控制可以跨多个服务复用，而不要求每个 engine 内部实现完全一致。

## 吞吐控制

| 控制项 | 作用 |
| --- | --- |
| `--file_concurrency` | 在一个目录 job 中并发处理多个 PDF。 |
| `--page_concurrency` | 在一个 PDF 内并发处理多个页面。 |
| `--api_concurrency_start` / `--api_concurrency_max` | 限制 OpenAI-compatible model calls。 |
| `--num_cpu_workers` | 限制 CPU-heavy rendering 和 post-processing work。 |
| `--md_gen_concurrency` | 限制 Markdown/output generation work。 |
| `--render_concurrency` / `--encode_concurrency` / `--postprocess_concurrency` | 配置后限制对应本地资源 lane。 |

如果 endpoint 有足够后端容量，DotsOCR-style 服务可能受益于较高 API 并发。
MinerU 和 PaddleOCR-VL style 引擎通常应先配置 stage-specific limits，因为 layout、
block creation 和 recognition 容易失衡。

## 两阶段 Engine 控制

MinerU-style 控制项：

- `--mineru_layout_reserved_api_slots`
- `--mineru_recognition_api_concurrency`
- `--mineru_min_block_area_ratio`
- `--mineru_max_blocks_per_page`
- `--mineru_skip_visual_block_recognition`

PaddleOCR-VL-style 控制项：

- `--layout_detection_url`
- `--paddle_layout_concurrency`
- `--paddle_block_backpressure_high_watermark`
- `--paddle_block_backpressure_low_watermark`
- `--block_concurrency`

目标是让 layout generation、block recognition 和 VLM calls 都有边界且可观测，
而不是让较快的上游阶段制造无界 recognition backlog。

## Platform 功能

- FastAPI control API 和静态 control UI。
- Model profiles，用于保存 endpoint、engine、model name 和默认 parser options。
- Worker 注册和 heartbeat。
- Worker readiness 和 shared-path 可见性。
- 分布式 folder scan 和 manifest 创建。
- Static shard scheduling。
- 通过 lease 回收 stale shard。
- Job、shard、scan unit 和 worker summaries。
- 终态安全的吞吐和状态 summary。
- Deployment Doctor 和只读 production preflight checks。
- Control 和 worker 角色分离的 production installers。

## 公开快照边界

仓库有意不包含：

- 模型权重；
- 私有 PDF 或非公开数据集；
- 生产 endpoint URL；
- API keys；
- host-specific environment files；
- runtime databases、logs 或 output artifacts。

请先使用 mock OCR service 验证控制流，再接入你自己的 OpenAI-compatible
OCR/model endpoint 进行真实解析。
