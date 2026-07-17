# OcrParser

> v0.2 明确本公开仓库为唯一源码主线，并启用 Control 安全默认值。参见
> [架构说明](docs/architecture.zh-CN.md)和
> [v0.2 安全迁移](docs/security-migration-v0.2.zh-CN.md)。

[English](README.md) | 中文

OcrParser 是一个面向生产场景的 PDF OCR 解析框架，适合长时间运行、
大批量和分布式 OCR 任务。它通过 OpenAI-compatible 的 OCR/模型服务，
把 PDF 转成 Markdown 和结构化产物，同时关注第一个 demo 跑通之后真正会遇到的问题：
断点恢复、重试、吞吐控制、worker 编排和任务可观测性。

## 适合谁

OcrParser 面向需要对一个或多个已有模型服务执行**大批量 OCR 任务**的团队，
而不是只想在 notebook 里解析单个 PDF 的场景。

它最适合这样的环境：

- PDF 输入和输出位于共享文件系统或共享存储挂载点上。
- OCR/model serving 与解析、调度、任务管理是分离的。
- 多台 worker 机器可以访问相同的 input、output 和 manifest 路径。
- 操作人员需要一个 control UI/API 来提交任务、观察进度、停止任务、检查 worker、
  诊断失败。
- 任务运行时间足够长，因此 retry、resume、stale-worker recovery 和吞吐可见性
  都是实际运维需求。
- 不同 OCR 引擎需要不同的并发策略，例如 DotsOCR 的高 API 并发，
  或 MinerU/PaddleOCR-VL 的 layout-recognition 两阶段背压。

它也可以作为本地 CLI 使用，但最强的使用场景是私有化、多机器 OCR 平台：
模型服务、worker agent、共享存储和 control UI 分别部署，各自承担清晰职责。

项目分为两层：

- `ocr_parser/`：单机 parser CLI，可处理单个 PDF 或一个 PDF 目录。
- `ocr_platform/`：可选的 FastAPI control UI 和 worker 平台，用于共享存储上的批处理任务。

这个公开快照不包含模型权重、私有文档、生产配置或 API 凭据。
你需要接入自己的 OCR/model endpoint。

## 为什么是 OcrParser

大多数 OCR 示例停留在“把一页 PDF 发给模型”。OcrParser 关注的是模型服务周围的
生产执行层：批处理、调度、共享路径执行、worker 协调、恢复和可观测性。

- **大批量执行**：目录任务支持页级并发和文件级并发，而不是串行 PDF 循环。
- **多引擎支持**：用同一套 parser 接口接入 DotsOCR、MinerU-style、
  PaddleOCR-VL-style 或通用 OpenAI-compatible OCR 引擎。
- **本地可恢复解析**：跳过已完成产物，校验 sidecar 状态，避免中断后重复昂贵工作。
- **共享存储分布式任务**：创建目录 manifest，把工作拆成 shard，让 worker agent
  通过 control API 领取和上报进度。
- **失败恢复**：重试瞬时模型/API 错误，回收 stale shard，隔离损坏的 worker update
  记录，并保持终态 job summary 稳定。
- **运维可观测性**：在 UI 中查看 worker、shard、吞吐、API inflight、lease、
  manifest integrity 和部署 readiness。

更多细节：

- [功能指南](docs/features.zh-CN.md)
- [恢复模型](docs/recovery-model.zh-CN.md)
- [Benchmark 说明](docs/benchmarks.zh-CN.md)
- [模型服务示例](docs/model-serving.zh-CN.md)

## 能从哪些问题中恢复

| 失败或长任务场景 | 框架行为 |
| --- | --- |
| 本地 parser 被中断 | 重新处理前检查已完成产物。 |
| worker 在 shard 中途退出 | shard lease 过期后，其他符合条件的 worker 可以重新领取。 |
| control API 暂时不可用 | worker 端 update 记录可以写入 spool，之后重放。 |
| 出现损坏的 update 记录 | 该记录会被隔离，后续有效 update 可以继续处理。 |
| job stop 时仍有 shard 运行 | 未领取 shard 立即停止；运行中的 shard 通过 lease/update 路径收敛。 |
| manifest 或 shard 数量异常 | manifest freeze/integrity 视图暴露 count 和文件不匹配问题。 |

## Benchmark 摘要

这些数字来自内部验证，用于说明框架调度行为。它们**不是**归一化的模型排行榜。
不同引擎的资源预算不同，你自己的 endpoint、硬件、PDF 形态和模型排队情况都会改变结果。

| 场景 | 资源 / 并发预算 | 结果 |
| --- | --- | --- |
| DotsOCR 目录任务，5 个 PDF / 251 页 | 全局 API cap 80，`file_concurrency` 1 vs 3 | 吞吐从 3.17 提升到 5.00 page/s，约 +58%。 |
| DotsOCR 目录任务，50 个 PDF / 2969 页 | 全局 API cap 80，`file_concurrency=8` | 直接 CLI repeat：7.82 page/s；control/agent/shard 路径：7.90 page/s。 |
| DotsOCR 页级并发曲线，8 个 synthetic PDF / 36 页 | `page_concurrency` 1 到 16 | 总耗时从 193.6s 降到 64.2s；20 页 fixture 提升 7.42x。 |
| MinerU-style 两阶段任务，50 个单页 PDF | `file_concurrency=4`、`page_concurrency=4`、API cap 8、recognition cap 6 | 50/50 成功，无 API error/timeout，layout/recognition 指标分离。 |
| PaddleOCR-VL-style 两阶段任务，50 个单页 PDF | `file_concurrency=4`、`page_concurrency=4`、API cap 8、layout cap 2 | 50/50 成功，无 API error 或 layout fallback，block backlog 可观测。 |

详见 [docs/benchmarks.zh-CN.md](docs/benchmarks.zh-CN.md)，其中包含脱敏后的 benchmark 背景和复现命令。

## 仓库结构

```text
ocr_parser/       Parser CLI、engine adapters、pipeline、output writers
ocr_platform/     Control API/UI、worker agent、manifest 和 shard 调度
dots_ocr/         DotsOCR-compatible 工具和 S3 示例 helper
configs/          公开环境变量模板
services/         systemd/logrotate 模板
scripts/          Worker helper scripts
tools/            Installer、preflight、benchmark helper、mock service
tests/            parser 和 platform 行为的 pytest 覆盖
```

## 环境要求

- Python 3.10+
- 可访问的 OpenAI-compatible OCR/model service
- 生产 control-plane 部署需要 PostgreSQL
- 多 worker platform job 需要共享存储

默认安装只包含 Parser、远程引擎 client 和 PDF/图片处理。从 package index 安装时
使用以下 profile：

```bash
pip install ocrparser-platform
pip install 'ocrparser-platform[platform]'
pip install 'ocrparser-platform[s3]'
pip install 'ocrparser-platform[layout]'
pip install 'ocrparser-platform[full]'
pip install 'ocrparser-platform[dev]'
```

从源码 checkout 开发时：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

`full` 明确不包含硬件相关的本地 layout runtime。基础安装仍会提供四个 console
script 名称；如果没有安装 `[platform]` 就运行 Platform 命令，程序会输出准确安装命令
并退出，不会暴露 import traceback。

## 快速开始：不用真实模型验证控制流

如果你想先验证 control UI、worker loop、events 和 job flow，而不是立刻启动真实 OCR 模型，
可以使用内置 mock OCR service：

```bash
python3 tools/local_prod_env.py up --with-worker --with-mock-ocr --shared-root /tmp/ocr-shared
```

打开命令输出中的 UI URL，然后用下面配置提交一个小任务：

- engine: `dotsocr`
- model name: `mock-ocr`
- model endpoint: `127.0.0.1:18000`

mock service 只用于本地控制流验证，不代表真实 OCR 质量或性能。

停止本地 stack：

```bash
python3 tools/local_prod_env.py down
```

## 最小 CLI 运行

CLI 会调用你的模型服务。它不会启动或下载模型。

单个 PDF：

```bash
python -m ocr_parser \
  --input_file /path/to/file.pdf \
  --output_dir /path/to/output \
  --profile local \
  --engine dotsocr \
  --ip YOUR_MODEL_ENDPOINT \
  --port 13080 \
  --model_name DotsOCR
```

目录模式：

```bash
python -m ocr_parser \
  --input_dir /path/to/pdfs \
  --output_dir /path/to/output \
  --profile balanced \
  --engine dotsocr \
  --ip YOUR_MODEL_ENDPOINT \
  --port 13080 \
  --file_concurrency 4 \
  --page_concurrency 16
```

对于高吞吐 DotsOCR-style 服务，应同时调优文件级和页级并发。
对于 MinerU 和 PaddleOCR-VL style 两阶段引擎，建议先从更低并发开始，
优先使用各自的 stage-specific limit。参见 [docs/model-serving.zh-CN.md](docs/model-serving.zh-CN.md)。

## Python SDK façade

v0.2 保持顶层导入兼容。配置现在是严格的：未知配置会直接报错，不再静默忽略。

```python
import asyncio
from ocr_parser import DotsOCRParser, ParserConfig

async def main():
    parser = DotsOCRParser(ParserConfig(ip="127.0.0.1", port=8000))
    await parser.initialize()
    try:
        await parser.parse_file("sample.pdf", output_dir="./output")
    finally:
        await parser.shutdown()

asyncio.run(main())
```

## 可选 Control UI

先安装 `[platform]`。生产 PostgreSQL 部署推荐先显式执行：

```bash
export OCR_PLATFORM_DATABASE_URL='postgresql+psycopg://user:password@db/ocr_platform'
ocr-platform-migrate plan
ocr-platform-migrate apply
ocr-platform-migrate verify
```

详细说明见[数据库迁移操作](docs/database-migrations.zh-CN.md)。仅使用 SQLite 的本地
开发无需执行上述 PostgreSQL migration 命令。然后启动：

```bash
OCR_PLATFORM_PORT=8080 \
python -m ocr_platform.control
```

打开 `http://127.0.0.1:8080/ui/`。

如果 worker 运行在其他机器上，先配置强 `OCR_PLATFORM_API_TOKEN`，再将 Control
监听到非 loopback 地址；同时把 `OCR_CONTROL_URL` 配置成 worker 可以访问的地址，
例如 `http://control.example.internal:8080`。

## 启动模式

| 模式 | DB | 端口 | Env | 日志 | 停止 |
| --- | --- | --- | --- | --- | --- |
| `local dev` | 默认 `sqlite:///./ocr_platform.db`，除非设置了 `OCR_PLATFORM_DATABASE_URL` | control 默认 `8080` 或 `OCR_PLATFORM_PORT` | shell env，例如 `OCR_PLATFORM_HOST`、`OCR_PLATFORM_PORT`、`OCR_PLATFORM_DATABASE_URL` | 前台 stdout/stderr | `Ctrl-C` |
| `single-machine production-like` | `postgresql+psycopg://...@127.0.0.1:15432/ocr_platform` | control `38080`，PostgreSQL `15432` | `.local/production/control.env`，可选 `.local/production/worker.env` | `.local/production/logs/control.out.log`、`.local/production/logs/control.err.log`、可选 worker logs | `python3 tools/local_prod_env.py down` |
| `real production` | 生产 `OCR_PLATFORM_DATABASE_URL`，仅 PostgreSQL | control 通常 `8080`，PostgreSQL 内部 `5432`，worker outbound 访问 control/model services | `/etc/ocr-platform/control.env`、`/etc/ocr-agent/worker.env` | `journalctl -u ocr-platform-control`、`journalctl -u ocr-agent-worker`、`/var/log/ocr-agent` | `systemctl stop ocr-platform-control` 和 `systemctl stop ocr-agent-worker` |

启动一个本地 production-like stack：

```bash
python3 tools/local_prod_env.py up --with-worker --shared-root /tmp/ocr-shared
python3 tools/local_prod_env.py status
python3 tools/local_prod_env.py down
```

## Worker Agent

Worker 需要访问：

- control API (`OCR_CONTROL_URL`)
- 任务提交时指定的共享 input/output 路径
- job model profile 中配置的 OCR/model endpoint

从公开模板创建环境文件：

```bash
sudo mkdir -p /etc/ocr-agent
sudo cp configs/ocr-agent-worker.env.example /etc/ocr-agent/worker.env
sudo editor /etc/ocr-agent/worker.env
```

启动 worker：

```bash
scripts/ocr_agent_worker.sh start /etc/ocr-agent/worker.env
scripts/ocr_agent_worker.sh status /etc/ocr-agent/worker.env
scripts/ocr_agent_worker.sh logs /etc/ocr-agent/worker.env
```

systemd 示例在 `services/ocr-agent-worker.service.example` 和
`services/ocr-platform-control.service.example`。

## Deployment Doctor

Control UI 包含 **Deployment Doctor**，用于在提交任务前查看部署状态。

- `/healthz`：轻量进程存活探针。
- `/readyz`：部署就绪探针，覆盖数据库和 worker 摘要。
- `/api/system/diagnostics`：UI diagnostics endpoint；启用 API auth 时受保护。

生产 rollout 前可运行只读 preflight checker：

```bash
python3 tools/production_preflight.py \
  --host worker-1.example.internal \
  --user ocr_user \
  --shared-root /shared/ocr-input \
  --platform-root /shared/ocr-platform \
  --control-url http://control.example.internal:8080
```

生产 installer 将 control/UI 和 worker 安装分开：

```bash
sudo python3 tools/install_production.py control --dry-run
sudo python3 tools/install_production.py worker --dry-run
```

先使用 `--dry-run`，审核生成的计划后再应用到主机。

## Benchmark 你的 Endpoint

生成本地 synthetic fixtures：

```bash
python3 tools/generate_benchmark_pdfs.py --output-dir /tmp/ocr-benchmark-pdfs
```

运行目录 benchmark：

```bash
python3 tools/run_performance_baseline.py \
  --input-dir /tmp/ocr-benchmark-pdfs \
  --output-root /tmp/ocr-benchmark-results \
  --variant current=. \
  --run-mode directory \
  --engine dotsocr \
  --ip YOUR_MODEL_ENDPOINT \
  --port 13080 \
  --model-name DotsOCR \
  --file-concurrency 4 \
  --page-concurrency 16
```

benchmark runner 会在 output root 下写入 CSV 和 Markdown summary。

## 配置

公开示例不包含凭据：

- `configs/ocr-platform-control.env.example`
- `configs/ocr-agent-worker.env.example`
- `dots_ocr/s3_download_config.example.json`
- `dots_ocr/s3_upload_config.example.json`

请把示例复制到仓库外部并在本地填写真实值。不要提交 API key、私有 endpoint、
客户数据、运行数据库、日志或下载的模型权重。

## 开发检查

```bash
python -m compileall ocr_parser dots_ocr ocr_platform
pytest tests
```

`Makefile` 也提供：

```bash
make verify
```

## License And Notices

OcrParser 原创代码使用 MIT License。安装必需依赖 PyMuPDF 的 AGPL build 时，
组合部署按 GNU AGPLv3 提供，并通过无需认证的 `/source` 入口公开准确运行版本源码。
参见 [AGPL 合规](docs/agpl-compliance.zh-CN.md)、[LICENSE](LICENSE)、
[NOTICE](NOTICE)和[第三方许可证审计](docs/third-party-licenses.zh-CN.md)。已经取得
Artifex 商业许可的部署改为遵守对应商业协议。
