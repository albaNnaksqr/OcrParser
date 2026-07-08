# OCR Platform MVP Smoke Test

This smoke test covers two operating modes:

1. Local single-machine validation (UI + control + agent on the same host).
2. Real deployment mode (control/UI on one server, execution agents on separate
   OCR servers).

The control API owns the database and scheduling state. Execution agents fetch jobs
from the control API and run `ocr_parser` subprocesses on the execution server.

## 1. Install Dependencies

```bash
python3 -m pip install -r requirements.txt
```

If you are using a virtualenv, run all commands below from that environment.
The agent defaults to the current Python interpreter when building OCR subprocess
commands. You can override it with `OCR_AGENT_PYTHON`.

## 2. Start the Control Server (Database + API + UI)

On the **control server**:

```bash
mkdir -p .local

OCR_PLATFORM_DATABASE_URL=sqlite:///./.local/ocr-platform.db \
OCR_PLATFORM_HOST=0.0.0.0 \
OCR_PLATFORM_PORT=8080 \
python3 -m ocr_platform.control
```

The control service exposes:

```text
http://<CONTROL_HOST>:8080
http://<CONTROL_HOST>:8080/ui
```

Health check:

```text
curl http://<CONTROL_HOST>:8080/api/servers
```

### Control/Execution separation notes

1. Run the database and UI on control host.
2. Run one or more execution agents on dedicated OCR hosts.
3. Keep `input_dir` and `output_dir` in shared storage (NFS/SMB/object-mount) so
   every agent can access the same paths.
4. Ensure execution hosts can reach `http://<CONTROL_HOST>:8080`.

## 3. Start an Execution Agent (Worker Host)

On each OCR execution host:

```bash
OCR_AGENT_SERVER_ID=local-dev \
OCR_CONTROL_URL=http://<CONTROL_HOST>:8080 \
OCR_AGENT_WORK_DIR=/var/lib/ocr/agent \
OCR_AGENT_PYTHON="$(command -v python3)" \
python3 -m ocr_platform.agent
```

Use one unique `OCR_AGENT_SERVER_ID` per execution host (for example:
`ocr-node-a`).

Expected result: the agent registers itself as the provided `server_id` and polls for
queued jobs assigned to that server.

A practical launch script for `worker-1.example.internal` is included at repository root:

```bash
OCR_AGENT_SERVER_ID=ocr-node-a \
OCR_CONTROL_URL=http://127.0.0.1:8080 \
OCR_AGENT_WORK_DIR=/home/ocr_user/ocr-agent \
OCR_REPO_DIR=/home/ocr_user/workspace/ocrparser \
OCR_AGENT_PYTHON=/home/ocr_user/ocr-agent/venv/bin/python \
bash start_agent_mvp.sh
```

The script will print a registration verification message when startup is successful.

### Quick registration check

```bash
curl http://<CONTROL_HOST>:8080/api/servers
```

The list should include the `OCR_AGENT_SERVER_ID` you started.

## 4. Create a Job

Use the control UI at `/ui` or call API directly. Paths must be accessible from
the execution host.

The MVP UI includes three hard-coded model profiles. These profiles belong to
the control/job configuration, not to the execution server registration:

| Profile | Engine | Endpoint | Model |
| --- | --- | --- | --- |
| PaddleOCR-VL @ worker-1.example.internal | `paddleocr-vl` | `127.0.0.1:30001` | `paddleocr-vl` |
| MinerU 2.5 @ 127.0.0.1 | `mineru` | `127.0.0.1:30090` | `MinerU2.5` |
| DotsOCR 1.5 @ 127.0.0.1 | `dotsocr` | `127.0.0.1:13080` | `DotsOCR` |

DotsOCR requires an API key. Enter it in the UI when submitting a DotsOCR job;
do not hard-code real API keys in repository files.

The execution server only needs to run the agent and reach the model endpoint
selected by the job. For example, a job assigned to `ocr-node-a` can use any of
the profiles above as long as `ocr-node-a` can access the endpoint network.

```bash
curl -X POST http://<CONTROL_HOST>:8080/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "input_dir": "/shared/input/pdfs",
    "output_dir": "/shared/output/results",
    "engine": "paddleocr-vl",
    "assigned_server_id": "ocr-node-a",
    "ip": "127.0.0.1",
    "port": 30001,
    "model_name": "paddleocr-vl",
    "page_concurrency": 1,
    "extra_args": {
      "skip_blank_pages": true,
      "num_cpu_workers": 1,
      "max_retries": 1,
      "retry_delay": 1,
      "timeout": 120,
      "max_completion_tokens": 4096,
      "no_warmup": true,
      "layout_detection_url": "http://127.0.0.1:30002"
    }
  }'
```

Expected result: the job is created as `queued`, then the agent claims it and
starts one managed `ocr_parser` subprocess.

## 5. Inspect Job State

```bash
curl http://<CONTROL_HOST>:8080/api/jobs
```

Expected state flow:

- `queued`: job is waiting for the assigned agent;
- `running`: agent claimed the job and launched the OCR subprocess;
- `succeeded`: parser completed successfully;
- `failed`: parser or agent reported failure;
- `stopping`: a stop request was accepted;
- `stopped`: agent terminated the OCR subprocess after a stop request.

As parser events arrive, `files` entries should show per-PDF progress such as
`file_path`, `status`, `done_pages`, `total_pages`, `output_path`, and `error`.

For production-style monitoring and the web UI, use the bounded summary
endpoint instead of loading all per-file rows:

```bash
curl http://<CONTROL_HOST>:8080/api/jobs/summary
```

The summary response is job-level only. It includes aggregate counters such as
total/completed/failed/skipped files, completed pages, progress percentage,
throughput, ETA, last event time, and stale-state detection. It intentionally
does not embed the `files` array, so the default UI path remains usable for
million-PDF jobs.

To inspect a small sample of recent file state without loading the full job:

```bash
curl 'http://<CONTROL_HOST>:8080/api/jobs/<job_id>/recent-files?kind=failed&limit=20'
curl 'http://<CONTROL_HOST>:8080/api/jobs/<job_id>/recent-files?kind=processed&limit=20'
```

The agent also forwards stdout and stderr lines to the control API through
`/api/jobs/{job_id}/logs`. The current headless MVP stores those log rows in the
database for the future UI, but it does not yet expose a log-read endpoint. Use
the agent terminal output or inspect the control database directly if you need
to verify raw log rows during this smoke test.

## 6. Request Stop

First get the job id from `/api/jobs`, then run:

```bash
curl -X POST http://<CONTROL_HOST>:8080/api/jobs/<job_id>/request-stop
```

Expected result:

- the job status becomes `stopping`;
- the agent observes `stop_requested`;
- the agent sends `SIGTERM` to the OCR subprocess;
- if the process does not exit before the termination timeout, the agent kills it;
- final status becomes `stopped`.

## 7. Resume or Force Reprocess

For the first MVP, resume is modeled by creating or rerunning a job with the
same input and output paths without `force_reprocess`. The existing parser
resume behavior should skip already-completed work when enabled by parser
settings.

To force all files through the parser again, create the job with:

```json
{
  "force_reprocess": true
}
```

The agent will pass `--force_reprocess` to the OCR subprocess.

## 8. Useful Local Checks

Run focused automated checks:

```bash
python3 -m pytest \
  tests/test_ocr_event_writer.py \
  tests/test_cli_job_events.py \
  tests/test_document_parser_event_hooks.py \
  tests/test_control_database.py \
  tests/test_control_api.py \
  tests/test_agent_command.py \
  tests/test_agent_event_tail.py \
  tests/test_agent_stop_logic.py \
  tests/test_platform_imports.py \
  -q

python3 -m compileall -q ocr_parser ocr_platform
```

## 9. Real Data Smoke Test (worker-1.example.internal)

You can directly use the folder you mentioned on worker-1.example.internal:

```text
/home/ocr_user/workspace/sample-documents
```

This folder currently contains many PDF files and is suitable for a real end-to-end smoke test.

### A. Prepare worker-1.example.internal output path

```bash
ssh worker-1.example.internal 'mkdir -p /home/ocr_user/workspace/ocr-output/sample-documents-ocr'
```

Optional sanity check:

```bash
ssh worker-1.example.internal 'python3 - <<"PY"
import os
root = "/home/ocr_user/workspace/sample-documents"
count = 0
for _, _, files in os.walk(root):
    for name in files:
        if name.lower().endswith(".pdf"):
            count += 1
print(f"{count} pdf files")
PY'
```

### B. Start execution agent on worker-1.example.internal

```bash
ssh worker-1.example.internal 'OCR_AGENT_SERVER_ID=ocr-node-a \
  OCR_CONTROL_URL=http://<CONTROL_HOST>:8080 \
  OCR_AGENT_WORK_DIR=/home/ocr_user/ocr-agent \
  OCR_REPO_DIR=/home/ocr_user/workspace/ocrparser \
  OCR_AGENT_PYTHON=/home/ocr_user/ocr-agent/venv/bin/python \
  bash /home/ocr_user/ocr-agent/start_agent_mvp.sh'
```

Check `ocr-agent` registration:

```bash
ssh worker-1.example.internal 'tail -n 30 /home/ocr_user/ocr-agent/logs/agent.log'
```

You should see `Registered server: ocr-node-a`.

### C. Register / verify server in UI and submit a job

Open `http://<CONTROL_HOST>:8080/ui/`, then:

1. Refresh servers (`api/servers`) and confirm `ocr-node-a` appears.
2. If not visible, use the Server form to register:
   - `server_id`: `ocr-node-a`
   - `name`: `worker-1.example.internal`
   - `host`: `worker-1.example.internal`
3. Submit one job with:
   - `input_dir`: `/home/ocr_user/workspace/sample-documents`
   - `output_dir`: `/home/ocr_user/workspace/ocr-output/sample-documents-ocr`
   - `engine`: `dotsocr` (or your running engine)
   - `assigned_server_id`: `ocr-node-a`
   - `ip`: `127.0.0.1`
   - `port`: `8000`
   - `extra_args`: `{"skip_blank_pages": true, "save_page_json": true}`

### D. Observe and verify

- Job status should flow: `queued -> running -> succeeded`
- Files in `/home/ocr_user/workspace/ocr-output/sample-documents-ocr` should be generated progressively
- Use the Jobs table refresh to inspect `running/succeeded/failed` and per-file progress.

If anything goes wrong, inspect:

- Agent output: `ssh worker-1.example.internal 'tail -f /home/ocr_user/ocr-agent/logs/agent.log'`
- Control state: `curl http://<CONTROL_HOST>:8080/api/jobs`

To stop: click `Stop` in UI or call:

```bash
curl -X POST http://<CONTROL_HOST>:8080/api/jobs/<JOB_ID>/request-stop
```
