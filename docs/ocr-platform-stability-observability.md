# OCR Platform Stability and Observability

Date: 2026-05-25

## Purpose

This document defines the stability and observability checklist for the OCR
platform without binding it to the current execution implementation.

The current MVP uses one control plane, one execution agent, and one managed
OCR parser subprocess per job. Future versions may add multiple agents per job,
file-level scheduling, page-batch scheduling, retries across servers, or other
performance-oriented execution models. The stability model should remain useful
across those changes.

The goal is not to maximize throughput. The goal is to make execution
trustworthy:

- operators know whether work is progressing;
- failures have categories and evidence;
- terminal states are reliable;
- stop and resume behavior is explainable;
- outputs can be checked for completeness;
- performance changes can evolve without rewriting monitoring semantics.

## Abstract Execution Model

Use these concepts when describing stability checks:

- `job`: the user-created task and its desired outcome;
- `attempt`: one concrete execution attempt for a job;
- `runner`: the agent, worker, subprocess, container, or remote executor doing work;
- `work_unit`: a bounded unit of work, such as a PDF, page range, page batch, or future shard;
- `artifact`: a produced output, such as Markdown, page JSON, image crops, logs, or manifest files;
- `event`: a structured state change or measurement;
- `metric`: a sampled or aggregated measurement used for monitoring.

Current code may map `work_unit` mostly to PDFs and pages, but stability checks
should not require that mapping forever.

## Status Semantics

### Job Status

- `queued`: accepted by the control plane but not actively running;
- `running`: at least one active attempt is making or expected to make progress;
- `stopping`: a stop request has been accepted and is being applied;
- `succeeded`: the intended job output is complete enough to accept;
- `failed`: the job cannot complete without intervention;
- `stopped`: the job ended because an operator requested stop.

Terminal statuses are `succeeded`, `failed`, and `stopped`.

Terminal jobs must have stable summary metrics. Values such as `pages/sec` and
`files/min` must not continue changing after terminal state.

### Attempt Status

- `created`: attempt was recorded but not started;
- `starting`: runner is preparing inputs, configuration, or subprocesses;
- `running`: runner is actively processing work;
- `stopping`: runner has received a stop request;
- `succeeded`: attempt finished successfully;
- `failed`: attempt ended because of an error;
- `stopped`: attempt ended because of a stop request;
- `lost`: runner disappeared before reporting a terminal state.

The platform should eventually allow multiple attempts per job so recovery,
retry, and migration do not overwrite the original evidence.

### Work Unit Status

The platform should support these statuses independent of whether a work unit is
a PDF, page batch, or future scheduling shard:

- `pending`;
- `running`;
- `succeeded`;
- `failed`;
- `skipped`;
- `retrying`;
- `stopped`.

Work unit state should be queryable by bounded search or recent-error views, not
by loading every unit into the default UI.

## Event Taxonomy

Events should be structured JSON records with at least:

- `event_type`;
- `job_id`;
- optional `attempt_id`;
- optional `runner_id`;
- optional `work_unit_id`;
- optional `work_unit_type`;
- `created_at`;
- `payload`.

Recommended event types:

- `job_created`;
- `job_started`;
- `job_stopping`;
- `job_stopped`;
- `job_done`;
- `job_failed`;
- `attempt_started`;
- `attempt_heartbeat`;
- `attempt_stopping`;
- `attempt_done`;
- `attempt_failed`;
- `attempt_lost`;
- `work_unit_started`;
- `work_unit_progress`;
- `work_unit_done`;
- `work_unit_failed`;
- `work_unit_skipped`;
- `artifact_produced`;
- `log_line`;
- `metric_sample`.

The current parser event names such as `file_started`, `page_done`, and
`file_done` can be mapped into this taxonomy without requiring an immediate
database rewrite.

## Failure Categories

Failures should have machine-readable categories. Free-form error text is still
useful, but it should not be the only signal.

Recommended top-level categories:

- `input_missing`: input path does not exist or is not visible to the runner;
- `input_empty`: no eligible source documents were found;
- `input_invalid`: source document cannot be opened or parsed;
- `output_unwritable`: output path cannot be created or written;
- `config_invalid`: job configuration is missing, malformed, or unsupported;
- `runner_start_failed`: agent, container, or subprocess could not start;
- `runner_lost`: runner heartbeat disappeared before terminal state;
- `parser_failed`: parser emitted a terminal failure event without a more
  specific category;
- `process_failed`: parser subprocess exited with a non-zero exit code;
- `process_killed`: parser subprocess was terminated by a signal;
- `model_unreachable`: OCR endpoint cannot be reached;
- `model_timeout`: OCR endpoint did not respond before timeout;
- `model_error`: OCR endpoint returned an error response;
- `model_output_invalid`: model response was not parseable or did not match expected schema;
- `postprocess_failed`: parser post-processing failed after OCR responses were received;
- `artifact_missing`: expected output artifact was not produced;
- `artifact_invalid`: expected output artifact exists but is unusable, for
  example an empty file;
- `artifact_incomplete`: the local OCR sidecar exists, but the file did not
  reach a successful terminal state;
- `operator_stopped`: operator-requested stop ended the work;
- `unknown`: fallback category when no better category is available.

Failure categories should be stable across DotsOCR, MinerU, PaddleOCR-VL, and
future engines.

The control plane infers these categories from terminal events when the parser
or agent does not provide an explicit `failure_category`. Typical endpoint
connection failures map to `model_unreachable`, HTTP status / rate-limit errors
map to `model_error`, response parsing problems map to `model_output_invalid`,
storage failures such as disk-full, disk-quota, and read-only filesystem errors
map to `output_unwritable` even when the raw exception does not mention an
output path, and timeout text maps to the legacy `api_timeout` category for compatibility
with existing job history. Obvious parser crash text maps to `parser_failed`;
otherwise truly unmapped control-plane failures may remain `unknown` so callers
with more context can apply a better fallback. The shard update API applies the same inference when
an older worker reports a failed shard with only `error_message`, so shard,
attempt, and job-level summaries still carry a machine-readable category.
The parser also applies the same lightweight inference before emitting
`file_failed` events and writing `.ocr_status.json`, so per-PDF sidecars and
live worker events carry categories such as `api_timeout`, `model_unreachable`,
`model_output_invalid`, `output_unwritable`, and `input_invalid` even when the
exception originated below the control API boundary.
Distributed scan-unit failures also persist a `failure_category`, allowing scan
failures such as missing input roots, unwritable manifest roots, and parser
process failures to be filtered without parsing free-form error text. If a scan
unit failure has error text but cannot be mapped more specifically, the control
plane records it as `input_invalid` rather than leaving it unclassified, because
scan units are primarily reading and sharding input paths.

## Health Checklist

### Control Plane

- API is reachable from execution hosts.
- Database is reachable and accepting writes.
- Job creation records all required configuration.
- Job summaries are bounded and do not load all work units.
- Terminal job metrics remain stable.
- Unknown jobs return explicit 404 responses.
- Non-terminal deletion is rejected.
- Control restart does not lose persisted job state.

### Runner / Agent

- Runner registers with the control API.
- Runner sends heartbeats while idle and while running.
- Heartbeat includes runner identity and basic capacity.
- Running heartbeat includes active `job_id`, attempt identity, and current phase.
- Runner reports subprocess/container start failures as structured failures.
- Runner reports terminal state exactly once per attempt.
- Runner can recover enough local state after restart to avoid silent orphan work.
- Runner can spool events locally when the control API is temporarily unavailable.

### Job Progress

- Job has a latest progress event time.
- Job has a latest heartbeat time.
- Job exposes whether progress is fresh, stale, or terminal.
- Stale detection distinguishes no heartbeat from no work progress.
- Progress counters are monotonic unless a new attempt explicitly resets scope.
- Current throughput and terminal throughput are calculated with different time
  windows.
- UI shows job-level health by default, not all work units.

### Work Unit Progress

- Work unit start, progress, success, skip, and failure can be represented.
- Recent failed units can be queried with bounded limits.
- Recent processed units can be queried with bounded limits.
- A single unit can be searched by stable input reference.
- Retried units preserve enough history to explain what happened.

### Artifact Completeness

For each successful work unit or job, the platform should eventually be able to
check:

- expected artifacts;
- produced artifacts;
- artifact paths;
- artifact sizes;
- optional checksums;
- missing artifact list;
- output manifest path.

The database should store artifact metadata and paths, not large artifact
contents.

Current parser-level idempotency uses one `.ocr_status.json` sidecar per input
PDF output directory. The sidecar records `status`, `failure_category`,
`error_type` when available, `duration_seconds`, `output_md_path`, page count,
input `size_bytes`/`mtime_ns` when stat is available, a secret-free
`model_config` summary, and a normalized
`artifacts` list for Markdown, JSON, layout PDF, and engine-native artifacts
when those outputs exist. When manifest freshness validation fails before OCR,
the failure sidecar also records `manifest_input_size_bytes` and
`manifest_input_mtime_ns` so operators can compare the manifest snapshot with
the current file stat without reopening the manifest row; its `error_type` is
one of `InputMissing`, `InputChanged`, or `InputInvalid`. Resume/skip checks must call the output artifact
completeness checker instead of trusting `status=success` alone; missing,
empty, or malformed JSON/JSONL declared artifacts force reprocessing of that PDF on a future shard
attempt. The sidecar is intended as a lightweight per-PDF audit record; it
should not contain API keys or other secrets.
The manifest output audit command surfaces failed sidecar `failure_category`
and `error_type` in issue samples, allowing operators to triage retry causes
from the aggregate audit JSON before drilling into individual output folders.

Manifest/shard execution is stricter than legacy one-off resume behavior:
existing Markdown without a successful `.ocr_status.json` is treated as
incomplete and does not skip manifest freshness validation or OCR execution.
This prevents shard reruns from accepting partial historical output as a
successful PDF. Manifest execution also requires successful sidecars to include
input `size_bytes`/`mtime_ns` that match the manifest row before reusing old
output, so older sidecars without input provenance and complete artifact sets
produced for a different input snapshot are reprocessed instead of silently
counted as skipped.

### Stop and Resume

- Stop request is acknowledged by the control plane.
- Runner observes stop request.
- Runner reports `stopping`.
- Runner gives active work a bounded graceful shutdown window.
- Runner escalates to force termination when graceful stop expires.
- Final state is `stopped`, not `failed`, when the operator-requested stop is
  the cause.
- Resume creates a new attempt or explicit rerun record.
- Force reprocess is explicit and auditable.

## Monitoring Views

Default UI should show:

- job status;
- active runner or runner count;
- active attempt;
- progress counters;
- terminal-safe throughput;
- fresh/stale indicator;
- last event time;
- last heartbeat time;
- failure category summary;
- bounded recent errors;
- bounded recent logs.

Default UI should not show:

- every PDF;
- every page;
- every work unit;
- parser-internal debug state as primary layout;
- engine-specific response internals.

Detailed views should be reached through bounded drill-downs such as recent
errors, recent processed units, logs, artifact summary, and exact search.

## Stability Smoke Suite

This suite should be executable against the current MVP and remain meaningful if
the execution strategy changes later.

Minimum cases:

- one small valid PDF succeeds;
- one long valid PDF succeeds;
- input directory is missing;
- input directory exists but contains no eligible documents;
- output directory is unwritable;
- OCR endpoint is unreachable;
- OCR endpoint times out;
- model output is malformed;
- operator stops a running job;
- operator stops during post-processing or artifact writing;
- control service restarts while runner is idle;
- control service restarts while runner is running;
- runner restarts while idle;
- runner restarts while running;
- resume after partial output;
- force reprocess after prior output;
- terminal job metrics remain stable over repeated UI refreshes.

## Smoke Run: 2026-05-25

Environment:

- control machine: local Mac, control API/UI on `0.0.0.0:8080`;
- runner: `worker-1.example.internal`, server id `ocr-node-a`;
- parser repo on runner: `/home/ocr_user/workspace/ocrparser-monitor-test`;
- runner work dir: `/home/ocr_user/ocr-agent`;
- DotsOCR endpoint under test: `127.0.0.1:13080`.

Results:

| Case | Job ID | Expected | Observed | Result |
| --- | --- | --- | --- | --- |
| Small valid PDF | `ee384c1d-2d42-4377-a627-74bf61eb6aee` | `succeeded` | `succeeded`, 1 file, 12 pages | pass |
| Missing input dir | `e972a547-0cf9-4ab9-980c-773a0208a3d0` | `failed` with `input_missing` | `failed`, no category | partial |
| Empty input dir | `366dd598-3b62-4a2d-acbc-bd4d11f92f90` | `failed` with `input_empty` | `failed`, no category | partial |
| Bad OCR endpoint | `14bbcf7b-4f10-43a6-ad65-9af1c8671b10` | `failed` with `model_unreachable` | `succeeded`, pages marked `success_fallback_image` | fail |
| Stop running job | `6a66c0e2-4964-4fc7-a3c3-b71e2ad0d252` | terminal `stopped` | stuck in `stopping`, stale, orphan parser workers remained | fail |

Findings:

- The bad-endpoint command did reach the runner with `--ip 127.0.0.1 --port 9`.
  The unexpected success came from parser success semantics: pages that failed
  OCR can still become `success_fallback_image`, and file/job success currently
  accepts that status.
- The stop failure was a runner lifecycle issue. The agent sent termination only
  to the parser parent process. Parser worker children could become orphaned and
  keep running after the parent was killed.
- Terminal throughput stability is covered by regression tests and passed local
  verification.

Fix applied after this smoke run:

- The agent now starts parser subprocesses in their own process group and
  terminates the whole process group on stop escalation. Local regression tests
  cover both process-group startup and process-group termination.

Open decisions:

- Decide whether `success_fallback_image` is acceptable production success,
  degraded success, or failure. For batch OCR quality, it should probably be a
  degraded or failed category unless the operator explicitly allows image-only
  fallback.
- Add failure categories so missing input, empty input, bad endpoint, timeout,
  malformed model output, and operator stop are distinguishable in the UI and API.
- Re-run the live stop smoke after deploying the updated agent code to
  `worker-1.example.internal` and cleaning up the stale test job.

## Current MVP Coverage

Covered or partially covered:

- central API and database;
- execution agent registration;
- assigned server polling;
- managed parser subprocess;
- structured parser events;
- job summary endpoint;
- bounded recent files endpoint;
- stop request path;
- terminal job deletion protection;
- terminal throughput stability after the latest fix.
- local regression coverage for stopping an agent-managed parser process group.
- job-level `failure_category` and `error_message` are recorded from terminal
  failure events and returned by job detail and summary APIs.
- image-only fallback pages are surfaced as degraded quality in job summaries
  through `degraded_pages` and `quality_flags`.

Important gaps:

- durable attempt table;
- agent heartbeat while idle and running;
- explicit current phase;
- broader failure category coverage beyond the first input and terminal failure
  paths;
- log read endpoint;
- recent error endpoint;
- artifact manifest and artifact completeness checks;
- local event spooling when control API is unavailable;
- runner restart recovery;
- control restart while job is running;
- output completeness validation before accepting success;
- final production policy for image-only fallback outputs: currently surfaced as
  degraded quality, not converted to job failure;
- distinct OCR throughput versus end-to-end throughput metrics.

## Design Constraints

Do not bind stability checks to a specific performance architecture.

Avoid assumptions such as:

- one job always maps to one subprocess;
- one job always maps to one server;
- one work unit always means one PDF;
- every PDF must be listed in the default UI;
- success can be inferred only from process exit code;
- DotsOCR-specific output shape is the platform-level contract.

Stable contracts should be:

- job lifecycle;
- attempt lifecycle;
- work unit lifecycle;
- structured events;
- failure categories;
- artifact metadata;
- bounded summary and drill-down APIs.

## Suggested Next Implementation Steps

1. Add attempt identity to event payloads and future schemas without breaking
   existing events.
2. Add runner heartbeat API and show last heartbeat in server and job summary.
3. Expand failure category mapping for model timeout, model unreachable,
   malformed model output, post-processing failures, and artifact failures.
4. Add bounded log-read and recent-error endpoints.
5. Add artifact completeness rollups to job/shard summaries and production
   audit commands.
6. Add smoke tests for missing input, empty input, bad endpoint, stop, resume,
   and terminal metric stability.
