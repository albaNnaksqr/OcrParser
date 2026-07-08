# OCR Platform Production Deployment Guide

中文版见 `docs/ocr-platform-deployment.zh-CN.md`.

This guide is written for production deployment. Hostnames, paths, database
URLs, and model endpoints are production placeholders; do not use test machine
names or personal workspace paths as defaults.

## Installer-first path

Use the local installer on each production host:

```bash
sudo python3 tools/install_production.py control --dry-run
sudo python3 tools/install_production.py worker --dry-run
```

Run the control command on the control/UI host and the worker command on each
execution host. The installer asks for an existing service user/group, validates
access, defaults each worker id to the host's primary IP, prints the install
plan, and only applies changes after confirmation. API authentication is
supported but disabled by default for the first install.

## Startup Modes

Use one of these three entry points instead of mixing temporary commands with
production services:

- `local dev`: run `python -m ocr_platform.control` directly. This may use
  SQLite and is meant for fast UI/API development only.
- `single-machine production-like`: run the local orchestration script. It
  starts containerized PostgreSQL, applies SQL migrations, starts the local
  control UI with production guards enabled, and can optionally start one local
  worker.
- `real production`: use PostgreSQL managed by production operations, systemd
  services, execution hosts, shared storage, and model service endpoints.

| Mode | DB | Ports | Env | Logs | Stop |
| --- | --- | --- | --- | --- | --- |
| `local dev` | `sqlite:///./ocr_platform.db` unless `OCR_PLATFORM_DATABASE_URL` is set | control default `8080` or `OCR_PLATFORM_PORT` | shell env such as `OCR_PLATFORM_HOST`, `OCR_PLATFORM_PORT`, `OCR_PLATFORM_DATABASE_URL` | foreground stdout/stderr | `Ctrl-C` in the foreground shell |
| `single-machine production-like` | `postgresql+psycopg://...@127.0.0.1:15432/ocr_platform` | control `38080`, PostgreSQL `15432` | `.local/production/control.env`, optional `.local/production/worker.env` | `.local/production/logs/control.out.log`, `.local/production/logs/control.err.log`, optional worker logs | `python3 tools/local_prod_env.py down` |
| `real production` | production `OCR_PLATFORM_DATABASE_URL`, PostgreSQL only | control usually `8080`, PostgreSQL internal `5432`, worker hosts outbound to control/model services | `/etc/ocr-platform/control.env`, `/etc/ocr-agent/worker.env` | `journalctl -u ocr-platform-control`, `journalctl -u ocr-agent-worker`, `/var/log/ocr-agent` | `systemctl stop ocr-platform-control` and `systemctl stop ocr-agent-worker` |

Local production-like commands:

```bash
python3 tools/local_prod_env.py up --with-worker --shared-root /tmp/ocr-shared
python3 tools/local_prod_env.py status
python3 tools/local_prod_env.py down
```

For a UI walkthrough when no real OCR model endpoint is available, add the
mock OpenAI-compatible endpoint:

```bash
python3 tools/local_prod_env.py up --with-worker --with-mock-ocr --shared-root /tmp/ocr-shared
```

The mock listens on `http://127.0.0.1:18000/v1` with model `mock-ocr`. Use it
only to verify control, worker, distributed shard progress, events, and Jobs UI
behavior; it is not a production model and does not measure OCR quality or
throughput. For the walkthrough job, choose or create a UI model profile with
`engine=dotsocr`, `ip=127.0.0.1`, `port=18000`, and `model_name=mock-ocr`.

The default UI is `http://127.0.0.1:38080/ui/`, the default API token is
`local-dev-token`, and the local PostgreSQL data directory is
`.local/production/postgres-data`. Use `tools/local_prod_env.py up --dry-run`
to inspect the plan before starting Docker Compose or local Python processes.
Use `tools/local_prod_env.py status` after startup to print the resolved DB,
ports, env files, log files, health probes, and stop command.

Avoid running multiple control processes against the same port or database. If
you previously started an ad hoc `launchctl` or foreground control process,
stop that process before using the single-machine production-like script.

## Production Topology

```text
control host
  - runs OCR control API and UI
  - connects to the production database
  - creates jobs, folder snapshots, manifests, shards, and recovery state

execution host pool
  - runs ocr_platform.agent workers
  - heartbeats to the control API
  - reports git version, Python path, shared-path access, and current load
  - claims shards and calls model services

shared filesystem
  - mounted at the same paths on every execution host
  - stores input PDFs, manifests, and output directories

model services
  - run outside the platform workers
  - are exposed to execution hosts through production load-balanced URLs
```

For `distributed folder scan`, the control host does not need direct access to
the PDF folder. Execution hosts must be able to read `input_dir` and write
`output_dir` and `manifest_root`.

## Runtime Users And Shared-Disk Permissions

Use dedicated service users for the control API and workers, but do not let
shared-disk access depend on the interactive login user that happened to deploy
the system. Create one runtime group that owns OCR platform data on the shared
filesystem:

```bash
# Use the same numeric UID/GID on every host, or manage them through LDAP/SSSD.
sudo groupadd --system --gid 2400 ocr-runtime
sudo groupadd --system --gid 2401 ocr-platform
sudo groupadd --system --gid 2402 ocr-agent
sudo useradd --system --uid 2401 --gid ocr-platform --groups ocr-runtime \
  --create-home --home-dir /var/lib/ocr-platform --shell /bin/bash ocr-platform
sudo useradd --system --uid 2402 --gid ocr-agent --groups ocr-runtime \
  --create-home --home-dir /var/lib/ocr-agent --shell /bin/bash ocr-agent

# Optional: allow the human deployment user to inspect and create OCR batches.
sudo usermod -aG ocr-runtime "$USER"
```

The exact UID/GID numbers are examples; pick values that are unused in your
environment and keep them identical on every host that mounts the shared
filesystem. If a centralized identity service already provides users and
groups, use it instead of local `useradd`, but keep the same `ocr-runtime`
group model.

`/shared/ocr-data` is a placeholder for the real shared filesystem mount, such as
`/shared/ocr-data`. `input_dir` and `output_dir` may live anywhere under that shared
filesystem according to the batch. Reserve a platform-owned subtree only for
platform runtime artifacts such as manifests and job-level shared files, and
make it group-writable with setgid so new directories keep the `ocr-runtime`
group:

```bash
sudo mkdir -p /shared/ocr-data/ocr-platform/{manifests,jobs}
sudo chown -R root:ocr-runtime /shared/ocr-data/ocr-platform
sudo chmod 2775 /shared/ocr-data/ocr-platform
sudo find /shared/ocr-data/ocr-platform -type d -exec chmod 2775 {} +
sudo find /shared/ocr-data/ocr-platform -type f -exec chmod 0664 {} +
```

Run these checks on the control host and on every execution host before
starting services. They must pass as the service accounts, not just as the
interactive login user:

```bash
findmnt /shared/ocr-data
sudo -u ocr-agent test -r /shared/ocr-data/ocr-platform
sudo -u ocr-agent test -w /shared/ocr-data/ocr-platform
sudo -u ocr-platform test -r /shared/ocr-data/ocr-platform
sudo -u ocr-platform test -w /shared/ocr-data/ocr-platform
```

Use these paths in jobs and worker configuration:

```text
OCR_AGENT_SHARED_ROOTS=/shared/ocr-data
input_dir=/shared/ocr-data/project-a/pdfs
output_dir=/shared/ocr-data/project-a/output
manifest_root=/shared/ocr-data/ocr-platform/manifests
```

If any host shows `/shared/ocr-data` as a local root filesystem instead of the
shared filesystem, stop the deployment and fix the mount before enabling the
agent.

Before changing production services, run the read-only preflight tool from an
operator workstation or the control host. It only executes SSH read checks and
does not write remote files, restart services, or repair mounts:

```bash
python tools/production_preflight.py \
  --host control.example.internal \
  --host worker-1.example.internal \
  --host worker-2.example.internal \
  --host worker-3.example.internal \
  --user ocr_user \
  --identity-file ~/.ssh/ocr_prod_ed25519 \
  --shared-root /shared/ocr-data \
  --platform-root /shared/ocr-data/ocr-platform \
  --control-host control.example.internal \
  --control-url http://control.example.internal:8080 \
  --json
```

Use the JSON output to verify PostgreSQL/control API readiness, shared mount
consistency, service-user access to the platform manifest directory, agent
processes, and deployed git refs before running a production gray job.

## Control Host

Create stable directories. If you already created `ocr-platform` in the common
runtime-user step above, do not recreate it here:

```bash
sudo mkdir -p /opt/ocr-platform /etc/ocr-platform /var/log/ocr-platform
sudo chown -R ocr-platform:ocr-platform /opt/ocr-platform /var/log/ocr-platform
```

Deploy code and dependencies:

```bash
sudo -iu ocr-platform
cd /opt/ocr-platform
git clone https://github.com/YOUR_ORG/ocrparser ocrparser
cd ocrparser
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Use PostgreSQL for production. SQLite is only for local development or temporary
smoke validation.

For first-time PostgreSQL provisioning, have the DBA or operator create the
database and role, then apply every SQL migration from the control checkout in
filename order:

```bash
cd /opt/ocr-platform/ocrparser
python tools/apply_control_migrations.py \
  --database-url "$OCR_PLATFORM_DATABASE_URL"
```

The baseline migration creates the control-plane tables, records
`schema_migrations`, and installs the production query indexes. Incremental
migrations add production constraints such as the job-global shard index. Future
schema changes should append new SQL migration files; production should not rely
only on application startup compatibility upgrades.

After startup, verify the live database migration state through the control
API:

```bash
curl -H "Authorization: Bearer $OCR_PLATFORM_API_TOKEN" \
  http://ocr-control.internal:8080/api/system/database
```

The response reports the database dialect, migration table presence, known SQL
migration files, applied migrations from `schema_migrations`, and
`latest_applied_migration`. In production, `dialect` should be `postgresql` and
`latest_applied_migration` should match the latest SQL migration shipped with
the deployed code.

Backup and restore procedures are covered in the
[OCR Platform Production Backup and Restore Runbook](ocr-platform-backup-restore.md).
Production backups must include both PostgreSQL and the JSONL manifest/shard
files under shared-disk `manifest_root`; backing up only one side is not enough
to restore a large job that has already entered execution.

Before a gray production run, stress-check shard claiming on PostgreSQL or an
equivalent test database:

```bash
cd /opt/ocr-platform/ocrparser
. .venv/bin/activate
python tools/pg_claim_stress.py \
  --database-url "$OCR_PLATFORM_DATABASE_URL" \
  --shards 1000 \
  --scan-units 1000 \
  --scan-unit-shards 2 \
  --workers 64 \
  --json
```

The result should report `ok: true`, empty `duplicate_claims`,
`missing_claims: 0`, `attempt_conflict_rejected: true`, and a
`scan_unit_claims` object whose `ok` is also `true`. With
`--scan-unit-shards`, the result should also include
`scan_unit_completion_shards.ok: true`, proving concurrent distributed
scan-unit completion generated a contiguous, duplicate-free global shard index
range. The tool refuses non-PostgreSQL DSNs and exercises real
`FOR UPDATE SKIP LOCKED` shard claiming, distributed scan-unit claiming and
completion, attempt-aware updates, and production indexes.
Use `--apply-init-db` only for an empty disposable test database; production
should be initialized through SQL migrations first.

Create `/etc/ocr-platform/control.env` from the template and restrict
permissions:

```bash
cd /opt/ocr-platform/ocrparser
sudo cp configs/ocr-platform-control.env.example /etc/ocr-platform/control.env
sudo chown root:ocr-platform /etc/ocr-platform/control.env
sudo chmod 0640 /etc/ocr-platform/control.env
sudo editor /etc/ocr-platform/control.env
```

Production example:

```bash
OCR_PLATFORM_DATABASE_URL=postgresql+psycopg://ocr_platform:CHANGE_ME@postgres.internal:5432/ocr_platform
OCR_PLATFORM_REQUIRE_POSTGRES=1
OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS=1
OCR_PLATFORM_HOST=0.0.0.0
OCR_PLATFORM_PORT=8080
OCR_PLATFORM_API_TOKEN=CHANGE_ME_LONG_RANDOM_TOKEN
OCR_PLATFORM_REQUIRE_API_TOKEN=1
OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS=1

OCR_JOB_STALE_AFTER_SECONDS=120
OCR_SERVER_STALE_AFTER_SECONDS=120
OCR_SHARD_LEASE_SECONDS=300
OCR_SCAN_UNIT_CLAIM_BATCH_SIZE=100

OCR_JOB_FILE_DETAIL_LIMIT=10000
OCR_JOB_EVENT_DETAIL_LIMIT=50000
OCR_JOB_LOG_DETAIL_LIMIT=10000
OCR_JOB_FAILED_FILE_SAMPLE_LIMIT=100
OCR_JOB_RECENT_ERROR_SAMPLE_LIMIT=100
```

Set `OCR_PLATFORM_REQUIRE_POSTGRES=1` in production. With this guard enabled,
the control service refuses SQLite or any non-PostgreSQL DSN so a real batch
cannot accidentally run on the local development database.

Set `OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS=1` in production. With this guard
enabled, the control service refuses to start on PostgreSQL unless
`schema_migrations` exists and all SQL migrations shipped with the deployed
code are applied. This catches stale production databases before workers begin
claiming jobs, and makes "database migrations are current" an explicit startup
requirement.

Set `OCR_PLATFORM_REQUIRE_API_TOKEN=1` in production. With this guard enabled,
the control service refuses to start unless `OCR_PLATFORM_API_TOKEN` is set,
preventing accidental unauthenticated `/api/` exposure.

Set `OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS=1` in production. With this
guard enabled, the model profile API rejects new direct `saved_api_key` values
and also refuses to keep an existing saved key during profile edits unless the
request clears it. Use `api_key_env_var` so model API keys live in the control
process secret environment instead of the control database.

`OCR_SCAN_UNIT_CLAIM_BATCH_SIZE` bounds each distributed scan-unit claim query.
The control plane uses PostgreSQL `FOR UPDATE SKIP LOCKED` on that batch before
checking worker path eligibility, avoiding an unbounded candidate scan while
keeping concurrent scan workers from claiming the same directory.

For very large production runs, set `OCR_JOB_FILE_DETAIL_LIMIT=0`,
`OCR_JOB_EVENT_DETAIL_LIMIT=0`, and/or `OCR_JOB_LOG_DETAIL_LIMIT=0` to stop
writing per-file detail rows, raw event rows, or forwarded stdout/stderr log
rows. Job summaries still use aggregate counters, shard counters, and manifest
counters. The control service keeps bounded recent failed-file samples and
job-level failure event samples in `job_counters` for `recent-files?kind=failed`
and `recent-errors/page`; tune that retention with
`OCR_JOB_FAILED_FILE_SAMPLE_LIMIT` and `OCR_JOB_RECENT_ERROR_SAMPLE_LIMIT`.
Raw-event and DB log troubleshooting views become less detailed when those
detail rows are disabled. The production migration baseline also creates indexes
for the bounded detail tables: `job_events(job_id, created_at, id)`,
`job_files(job_id, file_path)` for high-frequency event upserts,
`job_files(job_id, updated_at, id)` for pruning, and
`job_logs(job_id, created_at, id)`.

Install the control service:

```bash
cd /opt/ocr-platform/ocrparser
sudo cp services/ocr-platform-control.service.example /etc/systemd/system/ocr-platform-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now ocr-platform-control
sudo systemctl status ocr-platform-control
```

Open:

```text
http://ocr-control.internal:8080/ui/
```

Do not open the UI with `file://.../index.html`; file mode will not reliably
connect to the control API.

Set `OCR_PLATFORM_API_TOKEN` in production. When it is set, every `/api/`
request must include `Authorization: Bearer <token>`, `X-API-Key: <token>`, or
`X-OCR-Platform-Token: <token>`. Use the same value as `OCR_CONTROL_API_TOKEN`
on every execution host, keep it out of git, and restrict env-file permissions.

## Execution Host Agent

Repeat this section on every execution host. The production default is one agent
per host. Run multiple agents on one host only when CPU, memory, network, and
shared-disk IO have enough headroom.

Create stable directories. If you already created `ocr-agent` in the common
runtime-user step above, do not recreate it here:

```bash
sudo mkdir -p /opt/ocr-platform /etc/ocr-agent /var/lib/ocr-agent /var/log/ocr-agent
sudo chown -R ocr-agent:ocr-agent /opt/ocr-platform /var/lib/ocr-agent /var/log/ocr-agent
```

Deploy code:

```bash
sudo -iu ocr-agent
cd /opt/ocr-platform
git clone https://github.com/YOUR_ORG/ocrparser ocrparser
cd ocrparser
git checkout v2026.05.28
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Create `/etc/ocr-agent/worker.env` from the template and restrict permissions:

```bash
cd /opt/ocr-platform/ocrparser
sudo cp configs/ocr-agent-worker.env.example /etc/ocr-agent/worker.env
sudo chown root:ocr-agent /etc/ocr-agent/worker.env
sudo chmod 0640 /etc/ocr-agent/worker.env
sudo editor /etc/ocr-agent/worker.env
```

Production example:

```bash
OCR_AGENT_SERVER_ID=worker-1.example.internal
OCR_CONTROL_URL=http://ocr-control.internal:8080
OCR_CONTROL_API_TOKEN=CHANGE_ME_LONG_RANDOM_TOKEN

OCR_REPO_DIR=/opt/ocr-platform/ocrparser
OCR_AGENT_WORK_DIR=/var/lib/ocr-agent/worker-1.example.internal
OCR_AGENT_PYTHON=/opt/ocr-platform/ocrparser/.venv/bin/python

OCR_AGENT_SHARED_ROOTS=/shared/ocr-data

OCR_AGENT_POLL_INTERVAL=2
OCR_AGENT_HEARTBEAT_INTERVAL=5
OCR_AGENT_CONTROL_RETRY_INITIAL=1
OCR_AGENT_CONTROL_RETRY_MAX=30
OCR_AGENT_EVENT_SPOOL_DIR=/var/lib/ocr-agent/worker-1.example.internal/event-spool
OCR_AGENT_EVENT_SPOOL_MAX_MB=1024
OCR_AGENT_TERMINATION_TIMEOUT=10
OCR_AGENT_STOP_POLL_INTERVAL=1
OCR_MANIFEST_SCAN_PROGRESS_INTERVAL_FILES=10000

OCR_AGENT_RUNNER=tmux
OCR_AGENT_LOG_DIR=/var/log/ocr-agent/worker-1.example.internal
OCR_AGENT_TMUX_SESSION=ocr-agent-worker-1.example.internal

OCR_AGENT_GIT_REF=v2026.05.28
OCR_AGENT_SCRIPT_VERSION=ocr-agent-worker-v1
```

`OCR_MANIFEST_SCAN_PROGRESS_INTERVAL_FILES` controls how often distributed
folder scan emits `manifest_scan_progress` events while discovering PDFs.
Job summary keeps scan counters from the latest progress event, but preserves a
bounded set of recent scan error samples from recent progress events. A later
progress update without `skipped_errors` will not hide an earlier sampled scan
error while `skipped_error_count` is still non-zero. The scanner also emits a
final `done` progress event when it discovers zero PDFs, so permission or stat
errors still reach the control summary instead of living only in the manifest
metadata file. Folder-snapshot metadata, including both control-host and
distributed scans, records the true `skipped_error_count` but keeps only a
bounded `skipped_errors` sample, so a bad permissions subtree does not create an
unbounded `manifest.meta.json`. Job summary falls back to this metadata when no
live progress event exists, so control-host `folder_snapshot` jobs still show
scan completion and sampled scan errors in the API/UI.

`OCR_AGENT_EVENT_SPOOL_MAX_MB` bounds each pending local event/log spool file
when control is unavailable. The default is `256` MiB per pending file; use a
larger value on workers with dedicated persistent disks, or `0` for an
unbounded spool. When the bound is exceeded, the agent keeps the newest records,
drops the oldest records, and reports `dropped_events` / `dropped_logs` plus
spool byte counts in heartbeat capabilities so the control UI and preflight
warnings make the data loss visible.

`OCR_AGENT_EVENT_SPOOL_DIR` stores job events locally when the control API is
temporarily unreachable or returns a 5xx response. The agent replays this queue
after a successful heartbeat. Keep this directory on local persistent disk under
the worker work dir; do not place it in a temporary directory that is deleted on
restart. Pending replay records live in `events.jsonl`. If an old record hits a
permanent 4xx error during replay, the agent moves it to `events.failed.jsonl`
and continues with later records, so one stale bad event cannot block fresh
progress updates. Check `events.failed.jsonl` during incident review.

Validate the host:

```bash
cd /opt/ocr-platform/ocrparser
scripts/ocr_agent_worker.sh doctor /etc/ocr-agent/worker.env
```

Install the agent service:

```bash
cd /opt/ocr-platform/ocrparser
sudo cp services/ocr-agent-worker.service.example /etc/systemd/system/ocr-agent-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now ocr-agent-worker
sudo systemctl status ocr-agent-worker
```

Install log rotation for worker file logs:

```bash
sudo cp services/ocr-agent-worker.logrotate.example /etc/logrotate.d/ocr-agent-worker
sudo logrotate -d /etc/logrotate.d/ocr-agent-worker
```

For hosts that intentionally run multiple local worker processes, install the
instance template instead of cloning the single-worker unit:

```bash
cd /opt/ocr-platform/ocrparser
sudo cp services/ocr-agent-worker@.service.example /etc/systemd/system/ocr-agent-worker@.service
sudo cp /etc/ocr-agent/worker.env /etc/ocr-agent/worker-01.env
sudo cp /etc/ocr-agent/worker.env /etc/ocr-agent/worker-02.env
sudoedit /etc/ocr-agent/worker-01.env /etc/ocr-agent/worker-02.env
sudo systemctl daemon-reload
sudo systemctl enable --now ocr-agent-worker@worker-01 ocr-agent-worker@worker-02
```

Each instance env file must set a unique `OCR_AGENT_SERVER_ID`,
`OCR_AGENT_WORK_DIR`, `OCR_AGENT_EVENT_SPOOL_DIR`, `OCR_AGENT_LOG_DIR`, and
`OCR_AGENT_TMUX_SESSION`. The single-worker installer defaults
`OCR_AGENT_SERVER_ID` to the host primary IP, such as `worker-1.example.internal`; pass
`--server-id` only when you need to override that default.

## Shared Filesystem

Every selected worker must see the same shared paths. For example:

```text
input_dir=/shared/ocr-data/project-a/pdfs
output_dir=/shared/ocr-data/project-a/output
manifest_root=/shared/ocr-data/ocr-platform/manifests
```

All selected workers must be able to read the input path and write the output
and manifest paths. Nested input folders are supported. Distributed folder scan
streams PDF discovery and writes manifest/shard files on the shared filesystem;
it does not put every discovered path into the control database. For production
batches, prefer immutable input directories or versioned manifests so the input
set does not change while a job is running.

## Model Services

Model endpoints should be production load-balanced addresses selected from the
control UI model profile. Execution agents receive the resolved model settings
from each job.

```text
DotsOCR:
  engine=dotsocr
  endpoint=http://dotsocr-lb.internal:13080
  model_name=DotsOCR

MinerU:
  engine=mineru
  endpoint=http://mineru-lb.internal:30090

PaddleOCR-VL:
  engine=paddleocr-vl
  endpoint=http://paddleocr-vl-lb.internal:30001
```

Do not commit API keys. Enter them at job submission time or provide them through
a production secret-management mechanism.

For profile-level keys, prefer `api_key_env_var` in production. The control
server resolves that environment variable when creating a job or serving
`next-job`, so the actual model API key can live in the process secret
environment rather than in the control database. Enable
`OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS=1` to prevent profile edits from
saving or retaining direct DB-backed keys and to reject new jobs that try to
store `extra_args.api_key` directly. If an API key is saved directly on a
model profile in development, it remains a profile secret in the control
database. Job creation does not copy profile keys into the job's
`extra_args_json`, and ordinary job API responses do not echo them. The control
service injects the key only into the `next-job` response consumed by an
execution agent. For one-off jobs without a model profile, set
`extra_args.api_key_env_var` instead of `extra_args.api_key`; with the production
guard enabled, `extra_args.api_key` is rejected. The control server validates the
env var at job creation, stores only the env var name, and injects the resolved
key only into `next-job`. The agent passes that key to the parser through the
child process `API_KEY` environment variable, not through `--api_key` argv or the
local `command.json` record. Still restrict database access, control API tokens,
and agent logs; do not write raw agent job payloads into general-purpose logs. Do not put
`api_key` inside model profile `extra_args JSON`; use `api_key_env_var` or the
dedicated `saved_api_key` field instead. The backend rejects profile
`extra_args` that contain `api_key` or other secret-like keys such as tokens,
passwords, authorization headers, or client secrets. Job `extra_args` uses only
the dedicated `api_key` / `api_key_env_var` channels for secrets; other
secret-like keys are rejected by the control API and ignored by the agent command
builder so they cannot land in argv or `command.json`.

## Production Job Baseline

Current recommended production baseline:

```text
target_files_per_shard: 1000-5000
page_concurrency: 80
file_concurrency: 8
num_cpu_workers: 56
max_shard_attempts: 3
OCR_SHARD_LEASE_SECONDS: 300
OCR_SCAN_UNIT_CLAIM_BATCH_SIZE: 100
```

Tune these values down if the execution host, shared filesystem, or model
service cannot sustain them. Before a very large run, use the same production
configuration on a gray-release batch such as 100-1000 PDFs and confirm
throughput, failure rate, and recovery behavior.

For very large input folders with enough subdirectories to split work, use
`distributed manifest scan`. This mode creates a scan-unit queue in the control
database. A worker claims one directory, scans only direct PDFs in that
directory, and submits child directories back as new scan units. Multiple
execution hosts can then expand the directory tree in parallel. This distributes
the scanner work, but shared-filesystem metadata throughput remains the hard
limit.

For `distributed manifest scan`, the manifest integrity API treats each
completed scan unit's `manifest_path` as an authoritative manifest fragment. It
checks scan-unit manifest row counts, declared metadata files, and all shard
files. The top-level `manifest_path` may be a logical aggregate path in this
mode, so the control host does not have to see a merged global `manifest.jsonl`
for the integrity report to pass.

The job summary API and UI work plan expose the scan snapshot state directly:
`scan_status` is the live scan lifecycle, `manifest_status` is the manifest row
state, `manifest_snapshot_status` is `scanning`, `ready`, `frozen`, or
`missing`, `shards_created` is the number of shard rows already generated for
the job, `executable_shards` is the current pending/running/retrying/stale shard
count that can still execute or recover, and `manifest_frozen_at` is populated once the control plane has
closed all scan units and fixed the shard plan. Treat `frozen` as "the platform
will not add more scan units or shards for this manifest snapshot." Still run
the manifest integrity check, and for large runs also sample or audit output
artifacts, because freeze status does not prove the files still exist or that
OCR output is complete.
When a distributed scan freezes, the summary also mirrors the stored freeze
report's `manifest_integrity_status`, `manifest_integrity_ok`, and
`manifest_integrity_issue_count`, so list views can flag a frozen snapshot whose
manifest or shard files failed validation without rescanning the filesystem.

Manifest/shard jobs must preserve `relative_path` parent directories in
`output_dir`. Do not run manifest inputs with `--flatten_output`; flattening can
merge different PDFs with the same basename into one output location and breaks
shard rerun idempotency and output audits. External manifests must not contain
duplicate `relative_path` rows, because duplicates point two input PDFs at the
same output key. When the control host can read an external manifest at
registration time, it rejects duplicate output keys immediately; for remote-only
paths, keep the integrity/audit checks in the release checklist.

When using `tools/audit_manifest_outputs.py --max-items` for a large-run sample,
check the JSON report's `truncated` field. `truncated=true` means only the first
N manifest rows were audited; it is a sampling result, not proof that the whole
shard or job output is complete.
The audit also reports duplicate `relative_path` values, because duplicate rows
map multiple PDFs to the same output key and make shard reruns non-idempotent.
`--sample-limit` only bounds the number of example issues in `issue_samples`;
when `issue_samples_truncated=true`, use `issue_count` and `issues_by_category`
for the aggregate result and rerun with a higher sample limit if more examples
are needed. For failed sidecars, issue samples include the sidecar's
`failure_category` and `error_type`, so retry triage can start from the audit
JSON without opening each output directory.
During manifest/shard reruns, old output is reused only when the successful
sidecar records an input snapshot matching the manifest row and all declared
artifacts stay under that PDF's output directory, still exist, are non-empty,
and JSON/JSONL artifacts parse successfully. If
sidecar `input_size_bytes` or `input_mtime_ns` is missing or differs from the
manifest row, the worker reprocesses that PDF instead of counting the old output
as skipped; output audit reports these as `sidecar_input_missing` or
`sidecar_input_mismatch`. If a success sidecar points at an artifact outside
the expected output directory, output audit reports `artifact_invalid` with
`outside_output_dir`. If a success sidecar reports failed pages in its page
summary, audit reports `page_failure`. Failed sidecars include `failure_category` and, when the parser can
identify it, `error_type`, which helps separate timeout/network/model-output
failures during shard retry triage. Pre-OCR manifest freshness failures use
`InputMissing`, `InputChanged`, or `InputInvalid` as the sidecar `error_type`.
Agent and control failure inference classifies both negative signal return codes
and shell-style `128 + signal` exits such as `137` as `process_killed`, so OOM
kills and SIGKILL terminations do not collapse into generic `process_failed`.

## Pre-Production Checks

Control host:

```bash
curl http://ocr-control.internal:8080/api/servers | python3 -m json.tool
curl 'http://ocr-control.internal:8080/api/jobs/page?limit=50&offset=0' | python3 -m json.tool
curl http://ocr-control.internal:8080/api/jobs/summary | python3 -m json.tool
curl 'http://ocr-control.internal:8080/api/jobs/summary/page?limit=50&offset=0' | python3 -m json.tool
```

`/api/jobs` and `/api/jobs/summary` keep their legacy list responses. For
production tooling and the UI, use `/api/jobs/page` or `/api/jobs/summary/page`;
the paged responses return `items`, `total`, `limit`, `offset`, and `has_more`
so large job histories can be paged without guessing whether another page
exists.

Before creating a production job, run the UI `Preflight` check or call:

```bash
curl -X POST http://ocr-control.internal:8080/api/jobs/preflight \
  -H 'Content-Type: application/json' \
  -d '{
    "model_profile_id": "dotsocr_15",
    "input_dir": "/shared/ocr-data/project-a/pdfs",
    "output_dir": "/shared/ocr-data/project-a/output",
    "engine": "dotsocr",
    "input_mode": "distributed_remote_folder_snapshot",
    "manifest_root": "/shared/ocr-data/.ocr_platform/manifests"
  }' | python3 -m json.tool
```

`ok=false` means a blocking issue exists, such as no eligible worker, a model
profile without a required API key, an unwritable `output_dir`, an unwritable
`manifest_root`, or a PostgreSQL control database whose SQL migrations have not
been applied. `warning` issues call out production risks such as SQLite, mixed
worker versions, resource-constrained eligible workers, or large
per-file/raw-event retention limits. Preflight uses the latest worker
shared-path and resource heartbeat data to judge read/write access and whether
selected workers are already under pressure.
The job creation API also refuses PostgreSQL jobs when required SQL migrations
are missing, so scripted submissions cannot bypass this database guard.
For selected or eligible distributed workers, every worker that can read
`input_dir` must also be able to write `output_dir` and `manifest_root`;
otherwise the job is blocked before creation. If `manifest_root` is omitted,
preflight checks the inferred default path under the matched shared root, such
as `/shared/ocr-data/.ocr_platform/manifests`. Worker heartbeat data must be fresh.

The resource guard also cooperates with running shards. Each agent writes
`OCR_AGENT_WORK_DIR/jobs/<job-id>/execution-control.json` and passes it to the
parser with `--execution_control_file`. When the host is under memory or disk
pressure, the parser pauses new model API calls and temporarily lowers the API
concurrency limit to `1`; in-flight calls finish normally. When pressure clears,
the agent restores `paused=false` and the job's configured concurrency limit.
The agent also mirrors each execution-control change to the current shard row,
preserving existing progress counters, so operators can see pause and restore
state before the parser emits its next file event.
Use stop requests for operator-initiated termination; resource guard is a
drain-and-resume mechanism. The job summary UI and shard inspector surface this
state as execution paused/running, current API concurrency limit, and the
pressure reason so operators can distinguish a healthy drain from a stuck shard.
The shard inspector is server-paged and can filter by status, worker,
`failure_category`, minimum attempt count, and running duration, so incident
review can focus on failed/stale/retrying shards without loading the whole shard
set into the browser. The production migration baseline includes supporting
indexes for `work_shards(job_id, failure_category, status, shard_index)` and
`work_shards(job_id, status, started_at, shard_index)` so these incident filters
remain bounded on large jobs.

Execution host:

```bash
cd /opt/ocr-platform/ocrparser
scripts/ocr_agent_worker.sh doctor /etc/ocr-agent/worker.env
scripts/ocr_agent_worker.sh status /etc/ocr-agent/worker.env
```

Check that workers are online, shared paths are green, `git_ref` and
`script_version` match the release, shards are claimed by multiple workers, and
output files appear under `output_dir`.

For a controlled recovery drill, temporarily lower `OCR_SHARD_LEASE_SECONDS` to
`60`, stop one running worker, confirm another worker reclaims the shard, then
restore the production value such as `300`.
