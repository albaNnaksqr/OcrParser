# OCR Parser / OCR Platform

OCR Parser turns PDF documents into Markdown and structured artifacts through
OpenAI-compatible OCR/model services. It has two layers:

- `ocr_parser/`: a single-machine parser CLI for one PDF or a directory of PDFs.
- `ocr_platform/`: an optional FastAPI control UI and worker platform for
  shared-storage batch jobs.

This public snapshot does not include model weights, private documents,
production configuration, or API credentials. Bring your own OCR/model
endpoint.

## Features

- PDF-to-Markdown/JSON parsing with resumable local execution.
- DotsOCR, MinerU, and PaddleOCR-VL style engine adapters.
- Optional control UI for jobs, workers, shards, throughput, and diagnostics.
- Shared-storage manifest/shard scheduling for distributed workers.
- Public environment templates for local and production-like deployment.

## Repository Layout

```text
ocr_parser/       Parser CLI, engine adapters, pipeline, output writers
ocr_platform/     Control API/UI, worker agent, manifest and shard scheduling
dots_ocr/         DotsOCR-compatible utilities and S3 example helpers
configs/          Public environment-file templates
services/         systemd/logrotate templates
scripts/          Worker helper scripts
tools/            Installers, preflight checks, benchmark helpers, mock service
docs/             Deployment, backup, architecture, and validation notes
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
python -m pip install -r requirements-dev.txt
```

## Minimal CLI Run

```bash
python -m ocr_parser \
  --input_file /path/to/file.pdf \
  --output_dir /path/to/output \
  --profile local \
  --engine dotsocr \
  --ip 127.0.0.1 \
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
  --ip 127.0.0.1 \
  --port 13080
```

The CLI calls your model service. It does not start or download models.

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

To validate UI/job flow without a real OCR service:

```bash
python3 tools/local_prod_env.py up --with-worker --with-mock-ocr --shared-root /tmp/ocr-shared
```

The mock service listens on `http://127.0.0.1:18000/v1` with model name
`mock-ocr`. It is only for local control-flow validation.

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

## Production Docs

- [Deployment guide](docs/ocr-platform-deployment.md)
- [中文部署指南](docs/ocr-platform-deployment.zh-CN.md)
- [Worker service guide](docs/ocr-agent-worker-service.md)
- [Backup and restore](docs/ocr-platform-backup-restore.md)
- [Manifest/shard architecture](docs/ocr-platform-manifest-shard-architecture.md)

Run the read-only preflight checker before production rollout:

```bash
python3 tools/production_preflight.py \
  --host worker-1.example.internal \
  --user ocr_user \
  --shared-root /shared/ocr-data \
  --platform-root /shared/ocr-data/ocr-platform \
  --control-url http://control.example.internal:8080
```

The production installer keeps control/UI and worker installation separate:

```bash
sudo python3 tools/install_production.py control --dry-run
sudo python3 tools/install_production.py worker --dry-run
```

Run with `--dry-run` first, then review the generated plan before applying it
to a host.

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

## License

This project is released under the MIT License. See [LICENSE](LICENSE).
