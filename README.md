# OcrParser

English | [中文](README.zh-CN.md)

OcrParser is a production-oriented PDF OCR parsing framework for long-running,
batch, and distributed OCR workloads. It turns PDFs into Markdown and structured
artifacts through OpenAI-compatible OCR/model services, while focusing on the
parts that usually matter after the first demo works: resumability, retries,
throughput control, worker orchestration, and job observability.

## Who This Is For

OcrParser is designed for teams that need to run **large batches of OCR work**
against one or more existing model services, not just parse a single PDF from a
notebook.

It fits best when your environment looks like this:

- PDF inputs and outputs live on a shared filesystem or shared storage mount.
- OCR/model serving is separate from parsing and job orchestration.
- Multiple worker machines can access the same input, output, and manifest
  paths.
- Operators need one control UI/API to submit jobs, watch progress, stop jobs,
  inspect workers, and diagnose failures.
- Jobs may run long enough that retry, resume, stale-worker recovery, and
  throughput visibility are operational requirements.
- Different OCR engines need different concurrency policies, such as high
  DotsOCR API concurrency or two-stage MinerU/PaddleOCR-VL layout-recognition
  backpressure.

It can still run as a local CLI, but its strongest use case is a private,
multi-machine OCR platform where model services, worker agents, shared storage,
and the control UI are deployed as separate pieces.

The project has two layers:

- `ocr_parser/`: a single-machine parser CLI for one PDF or a directory of PDFs.
- `ocr_platform/`: an optional FastAPI control UI and worker platform for
  shared-storage batch jobs.

This public snapshot does not include model weights, private documents,
production configuration, or API credentials. Bring your own OCR/model
endpoint.

## Why OcrParser

Most OCR examples stop at "send a PDF page to a model." OcrParser is built for
the operational layer around model services: batching, scheduling, shared-path
execution, worker coordination, recovery, and visibility.

- **Large batch execution**: process directories with page-level and file-level
  concurrency instead of serial PDF loops.
- **Multi-engine support**: run DotsOCR, MinerU-style, PaddleOCR-VL-style, or
  generic OpenAI-compatible OCR engines behind the same parser interface.
- **Resumable local parsing**: skip completed outputs, validate sidecar status,
  and avoid repeating expensive work after interruption.
- **Shared-storage distributed jobs**: create folder manifests, split work into
  shards, and let worker agents claim and report progress through a control API.
- **Failure recovery**: retry transient model/API failures, reclaim stale shards,
  quarantine bad worker update records, and keep terminal job summaries stable.
- **Observable operations**: inspect workers, shards, throughput, API inflight
  counters, leases, manifest integrity, and deployment readiness from the UI.

More detail:

- [Feature guide](docs/features.md)
- [Recovery model](docs/recovery-model.md)
- [Benchmark notes](docs/benchmarks.md)
- [Model serving examples](docs/model-serving.md)

## What It Can Recover From

| Failure or long-running condition | Framework behavior |
| --- | --- |
| Parser interrupted locally | Resume checks completed artifacts before reprocessing. |
| A worker dies mid-shard | Shard leases expire and another eligible worker can reclaim the shard. |
| Control API is temporarily unavailable | Worker-side update records can be spooled and replayed. |
| A malformed update record appears | The record is quarantined so later updates can continue. |
| A job is stopped while shards are running | Unclaimed shards stop immediately; running shards settle through lease/update paths. |
| A manifest or shard count looks wrong | Manifest freeze/integrity views expose count and file mismatches. |

## Benchmark Highlights

These numbers come from internal validation runs and are included to describe
the scheduling behavior of the framework. They are **not** a normalized model
leaderboard. Resource budgets differed between engines, and your endpoint,
hardware, data shape, and model queueing will change the results.

| Scenario | Resource / concurrency budget | Result |
| --- | --- | --- |
| DotsOCR directory run, 5 PDFs / 251 pages | Global API cap 80, `file_concurrency` 1 vs 3 | Throughput improved from 3.17 to 5.00 page/s, about +58%. |
| DotsOCR directory run, 50 PDFs / 2969 pages | Global API cap 80, `file_concurrency=8` | Direct CLI repeat: 7.82 page/s. Control/agent/shard path: 7.90 page/s. |
| DotsOCR page concurrency curve, 8 synthetic PDFs / 36 pages | `page_concurrency` 1 to 16 | Total time dropped from 193.6s to 64.2s; 20-page fixture improved 7.42x. |
| MinerU-style two-stage run, 50 one-page PDFs | `file_concurrency=4`, `page_concurrency=4`, API cap 8, recognition cap 6 | 50/50 success, no API errors/timeouts, separate layout/recognition metrics. |
| PaddleOCR-VL-style two-stage run, 50 one-page PDFs | `file_concurrency=4`, `page_concurrency=4`, API cap 8, layout cap 2 | 50/50 success, no API errors or layout fallbacks, block backlog observable. |

See [docs/benchmarks.md](docs/benchmarks.md) for the sanitized benchmark context
and reproduction commands.

## Repository Layout

```text
ocr_parser/       Parser CLI, engine adapters, pipeline, output writers
ocr_platform/     Control API/UI, worker agent, manifest and shard scheduling
dots_ocr/         DotsOCR-compatible utilities and S3 example helpers
configs/          Public environment-file templates
services/         systemd/logrotate templates
scripts/          Worker helper scripts
tools/            Installers, preflight checks, benchmark helpers, mock service
tests/            pytest coverage for parser and platform behavior
```

## Requirements

- Python 3.10+
- A reachable OpenAI-compatible OCR/model service
- PostgreSQL for production control-plane deployments
- Shared storage for multi-worker platform jobs

Install dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Quickstart: Control Flow Without A Real Model

Use the built-in mock OCR service when you want to validate the control UI,
worker loop, events, and job flow without standing up a real OCR model:

```bash
python3 tools/local_prod_env.py up --with-worker --with-mock-ocr --shared-root /tmp/ocr-shared
```

Open the UI at the URL printed by the command, then submit a small job using:

- engine: `dotsocr`
- model name: `mock-ocr`
- model endpoint: `127.0.0.1:18000`

The mock service is only for local control-flow validation. It does not measure
real OCR quality or performance.

Stop the local stack:

```bash
python3 tools/local_prod_env.py down
```

## Minimal CLI Run

The CLI calls your model service. It does not start or download models.

Single PDF:

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

Directory mode:

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

For high-throughput DotsOCR-style services, tune file and page concurrency
together. For MinerU and PaddleOCR-VL style two-stage engines, start lower and
use their stage-specific limits first. See [docs/model-serving.md](docs/model-serving.md).

## Optional Control UI

For local dev:

```bash
OCR_PLATFORM_HOST=0.0.0.0 \
OCR_PLATFORM_PORT=8080 \
python -m ocr_platform.control
```

Open `http://127.0.0.1:8080/ui/`.

When workers run on other machines, configure `OCR_CONTROL_URL` with an address
they can reach, such as `http://control.example.internal:8080`.

## Startup Modes

| Mode | DB | Ports | Env | Logs | Stop |
| --- | --- | --- | --- | --- | --- |
| `local dev` | `sqlite:///./ocr_platform.db` unless `OCR_PLATFORM_DATABASE_URL` is set | control default `8080` or `OCR_PLATFORM_PORT` | shell env such as `OCR_PLATFORM_HOST`, `OCR_PLATFORM_PORT`, `OCR_PLATFORM_DATABASE_URL` | foreground stdout/stderr | `Ctrl-C` |
| `single-machine production-like` | `postgresql+psycopg://...@127.0.0.1:15432/ocr_platform` | control `38080`, PostgreSQL `15432` | `.local/production/control.env`, optional `.local/production/worker.env` | `.local/production/logs/control.out.log`, `.local/production/logs/control.err.log`, optional worker logs | `python3 tools/local_prod_env.py down` |
| `real production` | production `OCR_PLATFORM_DATABASE_URL`, PostgreSQL only | control usually `8080`, PostgreSQL internal `5432`, workers outbound to control/model services | `/etc/ocr-platform/control.env`, `/etc/ocr-agent/worker.env` | `journalctl -u ocr-platform-control`, `journalctl -u ocr-agent-worker`, `/var/log/ocr-agent` | `systemctl stop ocr-platform-control` and `systemctl stop ocr-agent-worker` |

Bring up a local production-like stack:

```bash
python3 tools/local_prod_env.py up --with-worker --shared-root /tmp/ocr-shared
python3 tools/local_prod_env.py status
python3 tools/local_prod_env.py down
```

## Worker Agent

Workers need access to:

- The control API (`OCR_CONTROL_URL`)
- Shared input/output paths for the submitted job
- The OCR/model endpoint configured in the job model profile

Create an environment file from the public template:

```bash
sudo mkdir -p /etc/ocr-agent
sudo cp configs/ocr-agent-worker.env.example /etc/ocr-agent/worker.env
sudo editor /etc/ocr-agent/worker.env
```

Then start the worker:

```bash
scripts/ocr_agent_worker.sh start /etc/ocr-agent/worker.env
scripts/ocr_agent_worker.sh status /etc/ocr-agent/worker.env
scripts/ocr_agent_worker.sh logs /etc/ocr-agent/worker.env
```

Systemd examples are in `services/ocr-agent-worker.service.example` and
`services/ocr-platform-control.service.example`.

## Deployment Doctor

The control UI includes **Deployment Doctor** for preflight visibility before
submitting jobs.

- `/healthz`: lightweight process liveness probe.
- `/readyz`: readiness probe covering database and worker summary.
- `/api/system/diagnostics`: UI diagnostics endpoint, protected when API auth is
  enabled.

Run the read-only preflight checker before production rollout:

```bash
python3 tools/production_preflight.py \
  --host worker-1.example.internal \
  --user ocr_user \
  --shared-root /shared/ocr-input \
  --platform-root /shared/ocr-platform \
  --control-url http://control.example.internal:8080
```

The production installer keeps control/UI and worker installation separate:

```bash
sudo python3 tools/install_production.py control --dry-run
sudo python3 tools/install_production.py worker --dry-run
```

Run with `--dry-run` first, then review the generated plan before applying it
to a host.

## Benchmarking Your Endpoint

Generate local synthetic fixtures:

```bash
python3 tools/generate_benchmark_pdfs.py --output-dir /tmp/ocr-benchmark-pdfs
```

Run a directory benchmark:

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

The benchmark runner writes CSV and Markdown summaries under the output root.

## Configuration

Public examples intentionally contain no credentials:

- `configs/ocr-platform-control.env.example`
- `configs/ocr-agent-worker.env.example`
- `dots_ocr/s3_download_config.example.json`
- `dots_ocr/s3_upload_config.example.json`

Copy examples outside the repository and fill in real values locally. Do not
commit API keys, private endpoints, customer data, runtime databases, logs, or
downloaded model weights.

## Development Checks

```bash
python -m compileall ocr_parser dots_ocr ocr_platform
pytest tests
```

The `Makefile` also provides:

```bash
make verify
```

## License And Notices

This project is released under the MIT License. See [LICENSE](LICENSE).
Third-party attribution is listed in [NOTICE](NOTICE).
