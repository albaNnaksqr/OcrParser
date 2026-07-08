# OCR Platform Purpose

Date: 2026-05-20

## Project Goal

This project is intended to evolve from a local OCR CLI/parser into a centralized OCR task control platform for high-volume document processing in a data-company environment.

The desired end state is that operators can use a web page, for example from a Mac browser, to create, start, monitor, stop, and resume OCR jobs without manually logging into every execution server.

## Operating Scenario

- PDF files to process are placed on a shared filesystem mounted by multiple servers.
- OCR model services such as DotsOCR, MinerU, and PaddleOCR may each expose a single logical endpoint.
- Those endpoints are expected to be load-balanced internally by the service side.
- The OCR execution servers are responsible for reading PDFs from the shared filesystem, calling the model endpoints, and writing outputs back to the shared filesystem.
- Multiple OCR execution servers may exist, but a typical job is expected to be bound to one chosen server and one chosen input folder.

## Preferred First-Version Model

The first platform version should not start with distributed workers competing for individual files.

Instead, the preferred model is:

- A user creates "Task 1" in the web UI.
- The task points to a specific input folder on the shared filesystem.
- The task has a specific output folder, engine, engine config, and concurrency settings.
- The task is assigned to a specific execution server.
- That server starts one managed OCR process for that task.
- The web UI shows task status, progress, logs, and failure information.
- The user can stop the task, restart it, or resume from existing outputs.

This keeps the mental model close to the current CLI workflow while removing manual SSH and manual process management.

## Chosen Runtime Model

The preferred first implementation should use a central control plane plus a
lightweight agent on each OCR execution server.

The runtime shape is:

```text
central UI/API/database
  -> assigns a directory-level OCR job to one execution server
execution-server agent
  -> starts and supervises one ocr_parser CLI process for that job
ocr_parser CLI process
  -> reads PDFs from the shared filesystem
  -> calls the selected OCR model endpoint
  -> writes outputs back to the shared filesystem
agent
  -> reports status, progress, logs, and failures back to the central API
```

In the first version, one job should be bound to one chosen server. A single
server should normally run only one OCR job at a time unless a later capacity
model is added. This avoids early ambiguity around GPU memory, output
collisions, cancellation semantics, and debugging.

The multi-server mode where files inside one input folder are split across
multiple execution servers should be treated as a later scheduling feature, not
the default first milestone.

## Execution Agent Responsibilities

Each OCR execution server should run a small long-lived `ocr-agent` service.
The agent is not the OCR engine. It is responsible for process management and
communication with the central control plane.

The agent should:

- register itself with the central API;
- send heartbeat and basic capacity information;
- receive or poll for jobs assigned to that server;
- validate that configured input and output folders are visible locally;
- launch the OCR parser as a managed subprocess;
- record the subprocess PID and command;
- collect stdout and stderr;
- stream or upload structured progress events;
- stop the subprocess gracefully on request;
- preserve enough local state to recover after an agent restart;
- optionally spool events locally if the central API is temporarily unavailable.

The managed subprocess should still use the existing CLI shape, for example:

```bash
python -m ocr_parser \
  --input_dir /shared/input/task_001 \
  --output_dir /shared/output/task_001 \
  --engine paddleocr-vl \
  --page_concurrency 16
```

Stop should map to graceful termination first, allowing the current parser's
shutdown behavior to finish in-flight pages and flush outputs. Forceful kill
should only be a fallback for stuck processes.

Resume should normally restart the same command without `--force_reprocess`.
Force reprocess should restart with the explicit force flag.

## Control Database Placement

The task database belongs to the central control plane, not to the execution
servers.

Recommended deployment:

```text
control server
  - Web UI
  - API service
  - primary database

execution servers
  - ocr-agent
  - OCR subprocesses
  - local log/event spool only
```

For a small prototype, SQLite on the control server is acceptable. For the
first serious deployment, Postgres on the control server is preferable. A later
larger deployment may move Postgres to a separate database host.

Agents should not connect directly to the database. They should communicate
only with the central API. This keeps database credentials off execution
servers and makes the central API the single authority for state transitions.

The database should store task metadata and operational state, not large OCR
outputs. OCR artifacts should remain on the shared filesystem. The database
should store paths, statuses, progress counters, errors, and log/event records.

Core tables should include:

- `servers`: execution server identity, status, heartbeat time, and capabilities;
- `jobs`: input folder, output folder, engine, config, assigned server, and state;
- `job_files`: per-PDF status, page counts, progress, output path, and error;
- `job_events`: structured progress events emitted by the parser or agent;
- `job_logs`: captured stdout/stderr lines or references to log files.

## Progress Reporting Model

The central UI should not infer progress only from free-form logs. The parser
or agent should emit structured events that can be stored and rendered by the
control plane.

Useful event types include:

- `job_started`;
- `file_started`;
- `page_done`;
- `file_done`;
- `file_failed`;
- `job_stopping`;
- `job_stopped`;
- `job_done`;
- `job_failed`.

The agent may read these events from a JSONL sidecar file, parse stdout lines
with a structured prefix, or receive them from a future parser callback API.
The important requirement is that the central API receives machine-readable
progress updates for job-level and file-level UI state.

## Why Not File-Level Work Stealing First

The first version should avoid a worker-pool model where every server continuously grabs individual PDF files from a shared queue.

That model may be useful later, but it adds early complexity:

- distributed file locks or database leases;
- duplicate-processing prevention;
- task timeout recovery;
- cross-server output collision handling;
- more complex pause, cancel, and retry semantics;
- harder debugging when many machines process the same folder.

The current priority is centralized control of directory-level OCR jobs, not maximum distributed scheduling flexibility.

For very large production jobs, such as millions of PDFs processed by many
execution servers, the next-stage design should evolve toward immutable
manifest snapshots, shard queues, and leases instead of fixed directory-level
assignment. See `docs/ocr-platform-manifest-shard-architecture.md`.

## Required Platform Capabilities

The platform should eventually provide:

- create OCR job from a web UI;
- select input folder and output folder;
- select OCR engine and engine configuration;
- select execution server;
- start job remotely;
- stop job gracefully;
- resume interrupted job using existing output state;
- force reprocess when explicitly requested;
- show job-level status;
- show file-level progress;
- show logs;
- show failures and retry information;
- show basic throughput and latency metrics;
- preserve engine-specific concurrency defaults.

## Current Project Fit

The current repository already provides useful execution-layer foundations:

- modular OCR CLI;
- multi-engine support for DotsOCR, MinerU, and PaddleOCR-VL;
- engine config loading;
- input directory scanning;
- basic resume behavior;
- graceful shutdown handling;
- Prometheus metrics hooks;
- local benchmark PDF generation and benchmark runner;
- scripts for starting PaddleOCR-VL-related services.

The missing layer is the centralized control plane:

- job database;
- server registry;
- managed remote process launch;
- task status persistence;
- log collection;
- web API;
- web UI.

## Suggested Architecture Direction

First milestone:

- central web/API service;
- database-backed job table on the control server;
- server table and heartbeat state;
- task creation page;
- lightweight `ocr-agent` on each execution server;
- assignment of one directory-level job to one chosen server;
- managed subprocess launch through the agent;
- task process tracking;
- structured progress event capture;
- stdout/stderr log capture;
- stop and resume actions.

Second milestone:

- stronger agent recovery after restart;
- local event spooling when the central API is unavailable;
- cleaner log streaming;
- richer server capacity reporting;
- optional multiple job slots per execution server;
- controlled start/stop API if push-based control is needed.

Later milestone:

- optional automatic server selection;
- optional file-level distributed scheduling for very large folders;
- more advanced retry, priority, quota, and alerting.

## Scheduling Evolution

The first version should expose manual server selection:

```text
create job -> choose input folder -> choose output folder -> choose engine -> choose execution server
```

Later, the platform may add automatic server selection based on heartbeat,
capacity, configured engine support, and current load.

Only after the directory-level agent model is stable should the platform add
file-level distributed scheduling:

```text
one input folder
  -> central scheduler creates per-file child work items
  -> multiple servers lease and process different PDFs
  -> central control plane aggregates progress and failures
```

That model can improve throughput for very large folders, but it requires
leases, duplicate-processing prevention, timeout recovery, cross-server output
collision handling, and more complex stop/resume semantics. It should remain a
later optimization rather than the initial product shape.

## Current Product Position

The project should be treated as:

- OCR execution engine: partially ready;
- multi-engine validation and benchmarking: partially ready;
- centralized job platform: not yet implemented;
- first usable web-controlled task platform: next major development target.
