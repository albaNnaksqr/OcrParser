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
checksums, and concurrent PostgreSQL shard claims. For the v0.3.1 release
candidate, pass these values explicitly:

- version `0.3.1`;
- the exact frozen candidate commit as `--expected-revision`;
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
export OCR_SOAK_EXPECTED_REVISION='set-to-the-frozen-v0.3.1-commit'
```

Run 20 cycles over 24 hours, with 100 generated PDFs per cycle and two workers:

```bash
python3 tools/run_stability_soak.py \
  --wheel /scratch/releases/ocrparser_platform-0.3.1-py3-none-any.whl \
  --expected-version 0.3.1 \
  --expected-revision "$OCR_SOAK_EXPECTED_REVISION" \
  --source-json-url http://127.0.0.1:38080/source.json \
  --database-url-env-var OCR_SOAK_DATABASE_URL \
  --control-url http://127.0.0.1:38080 \
  --control-token-env-var OCR_SOAK_CONTROL_TOKEN \
  --runtime-python /scratch/ocr-v031/bin/python \
  --runtime-repo-dir /scratch/ocrparser-v031 \
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

## v0.3.1 Wave 5 Preflight Evidence

The first short run at revision
`12abb795aa55f986cea29aa0e24451be40bd6f77` failed correctly and exposed P1
same-server stale-reclaim starvation. Eligibility-to-claim was 49.862 seconds
and eligibility-to-job-terminal was 57.436 seconds. The failure was audited and
cleaned up; the 24-hour soak did not start. Its retained summary hashes are:

- `report.json`: `9d4387207b37f649ee2c48abe1f509c42188883560d5429be7224e3565f1fdbe`
- `report.md`: `cb9e0a999b3f26bd404c270cc46257bcbe3844a3c19fd50ed443d08612a4fa1e`

Revision `1d3c8f560e94c4550718fc9910e8344ef38eae89` fences the previous
running shard/current attempt and previous running scan unit on same-server registration,
clears their owner/lease, and prioritizes stale/retrying claims over pending
work. It keeps public interfaces unchanged. GitHub CI run `29895536565` passed
11/11 jobs and 846 tests. The clean wheel used for repeat validation has
SHA256 `33482a9265f68b9be0b7b9bebc8b845bc9d5e6443b9a4c606c844f70f2c838d3`.

One post-fix run fired its fault too early. The fence and claim assertions
passed, but seven ordinary pending shards remained, so job completion took 48
seconds. The original result remains **FAIL**. Its summary hashes are:

- JSON: `051f14d8d6352f996564e6f04129fd1507871b8cd84e54cf74d1d16fff4dfb01`
- Markdown: `9e4e8a6c16a8442b86f984232e2c899f10641c24e80d0a8b7566bd49dde0dfa4`

A fresh strict r4 run injected the restart while exactly two ordinary pending
shards remained and passed 3/3 cycles:

- cycle 1 termination/fault-injection-to-attempt-2-claim: 19.327 seconds;
- cycle 2: 30 event/log records and one shard update replayed; migration
  `plan`, `apply`, and `verify` passed;
- cycle 3: registration fence 0.095 seconds, eligibility-to-claim 0.550
  seconds, eligibility-to-target-shard-terminal 1.725 seconds, stale selected before pending, and job
  terminal in 18.487 seconds;
- 300/300 documents and 30/30 shards succeeded; duplicates were zero,
  spool/quarantine were empty, manifest/output audit was 100%, resources
  passed, and cleanup residue was zero.

Successful-run SHA256 values:

- `report.json`: `107a8d18edca13bd5c8458c12f5e73b631c3468ef0ae066a83dfe34619e7fcce`
- `report.md`: `a0d4d98051aae0d0d60aadf169a182e8597204f2eea388ddff5f1949e06a3959`
- operator timeline: `671edca29d5bc89843baae42cc3b291e393c339ed0f4d7fd50b8bbacf68f82f7`
- audit: `422e5359cbade42e930f05885ae1db7f41aa6091f77d4a0863c25c514ed45848`
- cleanup: `e93b0ac4a63bdf17998b60deceaf3d0b00988e21ee61ddecffc0f34bde597e53`

This remains a short preflight. It does not replace the exact final-candidate
24-hour run, which has not started. See the
[v0.3.1 release notes](release-v0.3.1.md).

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
