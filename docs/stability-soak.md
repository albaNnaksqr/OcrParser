# v0.3 Stability Soak

English | [中文](stability-soak.zh-CN.md)

This runbook validates a released wheel in an isolated production-like
environment. It uses only public or sanitized PDFs and must not share database,
service, port, spool, or output state with a production deployment.

## Topology And Safety Boundary

- one task-owned PostgreSQL 16 database;
- one Control process installed from the release wheel;
- two Agent processes with distinct server IDs, work directories, and spool
  directories;
- one task-owned mock OCR endpoint for the 24-hour soak;
- task-owned MinerU/Paddle services for outage tests; never stop a shared model
  service;
- runtime secrets supplied only through environment variables.

Store reports outside the checkout. `stability-artifacts/` is ignored as an
additional safeguard, but an absolute scratch path is preferred.

## Required Gates

Before the first job, the runner verifies the release wheel version, immutable
revision, clean-build provenance, unauthenticated `/source.json`, migration
checksums, and concurrent PostgreSQL shard claims. The v0.3.0 stability run
expects:

- version `0.3.0`;
- revision `47e1c0399db97f4ec48715548b8c937bc77c20ba`;
- `release_build=true` from the running Control.

Download the wheel from the public GitHub Release and install it in the
Control/Agent runtime environment. Keep a separate checkout for the validation
tools when testing an already-released wheel.

The production-like helper can create the required two-Agent layout while
preserving the historical single-Agent default:

```bash
python3 tools/local_prod_env.py --state-dir /scratch/ocr-soak/runtime up \
  --with-worker \
  --worker-count 2 \
  --with-mock-ocr \
  --shared-root /scratch/ocr-soak/shared
```

## Mock Soak

Export the Control token and disposable database URL without placing either
secret in argv or a report:

```bash
export OCR_SOAK_CONTROL_TOKEN='set-at-runtime'
export OCR_SOAK_DATABASE_URL='postgresql+psycopg://user:password@127.0.0.1:15432/database'
```

Run 20 cycles over 24 hours, with 100 generated PDFs per cycle and two workers:

```bash
python3 tools/run_stability_soak.py \
  --wheel /scratch/releases/ocrparser_platform-0.3.0-py3-none-any.whl \
  --source-json-url http://127.0.0.1:38080/source.json \
  --database-url-env-var OCR_SOAK_DATABASE_URL \
  --control-url http://127.0.0.1:38080 \
  --control-token-env-var OCR_SOAK_CONTROL_TOKEN \
  --runtime-python /scratch/ocr-v030/bin/python \
  --runtime-repo-dir /scratch/ocrparser-v030 \
  --shared-root /scratch/ocr-soak/shared \
  --report-dir /scratch/ocr-soak/report \
  --worker-id soak-worker-01 \
  --worker-id soak-worker-02 \
  --engine-profile mock \
  --engine dotsocr \
  --ocr-host 127.0.0.1 \
  --ocr-port 18000 \
  --model-name mock-ocr \
  --cycles 20 \
  --duration-seconds 86400 \
  --documents-per-cycle 100
```

The modes rotate through `directory`, `existing_manifest`, and
`distributed_remote_folder_snapshot`. Each cycle records job state, manifest
integrity, output audit, sidecar stage/fallback labels, fault results, and
resource samples. Add process samples with repeatable
`--resource-pid-file LABEL=PATH` arguments.

## Fault Hooks

Fault hooks are argv arrays, never shell strings. They run while the configured
cycle is active and receive `OCR_SOAK_CYCLE` and `OCR_SOAK_REPORT_DIR`. A hook
must operate only on task-owned PID files, tmux sessions, containers, or
loopback ports and must return non-zero when recovery assertions fail.

```json
{
  "hooks": [
    {
      "name": "terminate-agent-02-and-wait-for-lease-reclaim",
      "cycle": 4,
      "after_seconds": 5,
      "argv": ["/scratch/ocr-soak/hooks/agent-reclaim"]
    },
    {
      "name": "control-outage-and-spool-replay",
      "cycle": 8,
      "after_seconds": 5,
      "argv": ["/scratch/ocr-soak/hooks/control-outage"]
    },
    {
      "name": "agent-shutdown-no-late-reporting",
      "cycle": 12,
      "after_seconds": 5,
      "argv": ["/scratch/ocr-soak/hooks/agent-shutdown"]
    }
  ]
}
```

Pass the file with `--fault-plan`. The operator-owned hook implementations must
verify lease recovery, spool replay, and absence of late reporting rather than
only sending a signal.

For the task-owned MinerU/Paddle services, add a fourth hook that stops the
model service for 60 seconds, restarts the exact pinned runtime, and checks that
the job records a bounded retry/failure category without a false success.

## Acceptance And Evidence

`report.json` and `report.md` are the authoritative outputs. Release is blocked
when any of these conditions occurs:

- a migration/source/wheel/claim gate fails;
- a job remains non-terminal after twice the configured lease window;
- a manifest or output audit fails;
- claims, artifacts, or events are duplicated or lost;
- a fault hook does not execute or its recovery assertion fails;
- an unknown stage/fallback label appears;
- warm-process RSS or file descriptors grow more than 20%;
- last-quartile mock throughput is more than 10% below the first quartile.

Real-engine runs use 50 public pages per engine with concurrency 1-2. Record
their current deployment evidence but do not compare throughput with historical
reports that used different server versions or replica counts.

## Cleanup And Rollback

Stop task-owned Agents, Control, mock/model services, and PostgreSQL. Confirm
their ports are closed, no task container or GPU process remains, and preserve
only the sanitized report directory. If a P0/P1 defect is found, keep v0.3.0 as
the latest production recommendation, publish the report as release-blocking
evidence, and fix the defect before tagging v0.3.1. The post-stabilization
direction is recorded in the [v0.4 operational-maturity RFC](rfc-v0.4.md).
