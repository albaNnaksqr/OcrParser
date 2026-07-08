# OCR Agent Worker Service

This document describes the standard way to run an OCR Platform execution worker.
The control UI/API stays on the control host. Each execution host runs one or
more `ocr_platform.agent` workers that heartbeat to the control URL, report shared
path access, claim shards, and stream job events/logs back to control.

For the full control-host plus execution-host deployment flow, see
`docs/ocr-platform-deployment.md` or the Chinese version at
`docs/ocr-platform-deployment.zh-CN.md`.

## Files

- `configs/ocr-agent-worker.env.example`: copy per host and edit values.
- `scripts/ocr_agent_worker.sh`: lifecycle wrapper for `start`, `stop`,
  `restart`, `status`, `logs`, `doctor`, and foreground `run`.
- `services/ocr-agent-worker.service.example`: optional single-worker systemd unit.
- `services/ocr-agent-worker@.service.example`: optional instance template for
  hosts that intentionally run multiple worker processes.
- `services/ocr-agent-worker.logrotate.example`: logrotate template for worker
  file logs under `/var/log/ocr-agent/<worker-id>/`.

## Runtime Group And Shared Root

The agent should run as a dedicated `ocr-agent` service user, but shared
filesystem writes should be authorized through a common runtime group rather
than through the human login user that created the directories. Use the same
UID/GID mapping on every host, or manage the accounts through LDAP/SSSD:

```bash
sudo groupadd --system --gid 2400 ocr-runtime
sudo usermod -aG ocr-runtime ocr-agent
sudo usermod -aG ocr-runtime ocr-platform
sudo usermod -aG ocr-runtime "$USER"
```

`/shared/ocr-data` is a placeholder for the real shared filesystem mount, such as
`/shared/ocr-data`. Job `input_dir` and `output_dir` may live anywhere under that
shared filesystem. Reserve a platform-owned subtree only for manifests and
job-level shared runtime files, and make it group-writable with setgid:

```bash
sudo mkdir -p /shared/ocr-data/ocr-platform/{manifests,jobs}
sudo chown -R root:ocr-runtime /shared/ocr-data/ocr-platform
sudo chmod 2775 /shared/ocr-data/ocr-platform
sudo find /shared/ocr-data/ocr-platform -type d -exec chmod 2775 {} +
```

Before starting an agent, verify the mount and service-account access from that
host:

```bash
findmnt /shared/ocr-data
sudo -u ocr-agent test -r /shared/ocr-data/ocr-platform
sudo -u ocr-agent test -w /shared/ocr-data/ocr-platform
sudo -u ocr-platform test -r /shared/ocr-data/ocr-platform
sudo -u ocr-platform test -w /shared/ocr-data/ocr-platform
```

Use the real shared filesystem mount as the worker shared root, and put job
manifests under the platform-owned subtree:

```text
OCR_AGENT_SHARED_ROOTS=/shared/ocr-data
input_dir=/shared/ocr-data/project-a/pdfs
output_dir=/shared/ocr-data/project-a/output
manifest_root=/shared/ocr-data/ocr-platform/manifests
```

If `findmnt` shows that `/shared/ocr-data` is not the shared filesystem on a
worker host, fix the mount first. A plain local directory with the same path is
not a valid shared root.

## Per-Host Environment

Create an env file on each execution host:

```bash
mkdir -p /etc/ocr-agent
cp configs/ocr-agent-worker.env.example /etc/ocr-agent/worker.env
```

Edit these fields:

```bash
OCR_AGENT_SERVER_ID=ocr-worker-01
OCR_CONTROL_URL=http://ocr-control.internal:8080
OCR_REPO_DIR=/opt/ocr-platform/ocrparser
OCR_AGENT_WORK_DIR=/var/lib/ocr-agent/worker-01
OCR_AGENT_PYTHON=/opt/ocr-platform/ocrparser/.venv/bin/python
OCR_AGENT_SHARED_ROOTS=/shared/ocr-data
OCR_AGENT_CONTROL_RETRY_INITIAL=1
OCR_AGENT_CONTROL_RETRY_MAX=30
OCR_AGENT_EVENT_SPOOL_DIR=/var/lib/ocr-agent/worker-01/event-spool
OCR_AGENT_EVENT_SPOOL_MAX_MB=1024
```

Use a unique `OCR_AGENT_SERVER_ID` for every worker process. On a 10-machine
pool, use names such as `ocr-node-01` through `ocr-node-10`.

`OCR_AGENT_EVENT_SPOOL_MAX_MB` bounds each pending event/log spool file when
the control API is unavailable. The agent keeps the newest records, records
dropped oldest records in `*.dropped.json`, and reports the dropped counts in
heartbeat capabilities. Use `0` only when the spool directory sits on storage
that can absorb an extended control outage.

`OCR_AGENT_SHARED_ROOTS` is colon-separated. The agent reports each root in its
heartbeat so control can decide whether the worker is eligible for a folder job.

`OCR_AGENT_CONTROL_RETRY_INITIAL` and `OCR_AGENT_CONTROL_RETRY_MAX` control
agent-side reconnect backoff when the control API is temporarily unreachable.
The agent should wait and retry instead of exiting during a control restart or
short network interruption.

`OCR_AGENT_EVENT_SPOOL_DIR` is the local durable queue for job events and logs
that could not be posted because the control API was temporarily unavailable or
returned a 5xx response. Put it on persistent local disk, normally under
`OCR_AGENT_WORK_DIR`. The agent replays the queue after a successful heartbeat.
Pending records are capped by `OCR_AGENT_EVENT_SPOOL_MAX_MB`; oldest overflow
records are counted in `events.dropped.json` or `logs.dropped.json` and reported
in heartbeat capabilities. If a replay hits a permanent 4xx response, the agent
moves that record to `events.failed.jsonl` / `logs.failed.jsonl` and continues
with later records, so one bad stale record cannot block fresh progress updates.
Inspect failed and dropped spool files during incident review.

## Resource Guard And Runtime Control

The agent resource guard checks memory and disk pressure before claiming new
jobs, scan units, or shards. It also writes a per-job
`execution-control.json` under `OCR_AGENT_WORK_DIR/jobs/<job-id>/` and passes it
to the parser with `--execution_control_file`. While a shard is running, the
agent refreshes that file every poll interval:

- `paused: true` tells the parser to stop starting new model API calls; in-flight
  calls finish normally.
- `api_concurrency_limit: 1` is used while local pressure is blocked.
- When pressure clears, `paused` returns to `false` and the API limit is restored
  to the job's configured `api_concurrency_start`, `api_concurrency_max`, or
  `page_concurrency` fallback.

Tune the thresholds per host:

```bash
OCR_AGENT_RESOURCE_GUARD_MEMORY_PERCENT=90
OCR_AGENT_RESOURCE_GUARD_MIN_AVAILABLE_MEMORY_GB=4
OCR_AGENT_RESOURCE_GUARD_DISK_PERCENT=95
OCR_AGENT_RESOURCE_GUARD_MIN_FREE_DISK_GB=10
```

This is a cooperative pause, not a hard kill. It gives the parser room to drain
active requests and continue once the host recovers. Use stop requests when an
operator needs to terminate the shard. The agent mirrors execution-control
changes directly to the current shard row while preserving progress counters,
so the UI can show pause and restore state before the parser emits its next file
event. The parser also emits the current `execution_control` state with runtime
metrics, and the control UI shows it on current shards and shard inspector rows
as execution paused/running, API limit, and reason.

## Control Recovery Timeouts

The control API reads these optional environment variables at startup:

```bash
OCR_JOB_STALE_AFTER_SECONDS=120
OCR_SERVER_STALE_AFTER_SECONDS=120
OCR_SHARD_LEASE_SECONDS=300
```

`OCR_SHARD_LEASE_SECONDS` controls how long a running shard stays reserved after
its worker stops renewing the lease. Lower values make failure tests and small
test pools recover faster. Higher values reduce the risk of reclaiming a shard
whose worker is only slow or temporarily disconnected.

For short recovery drills, use 30-60 seconds temporarily. For production runs,
start around 300 seconds and adjust after observing normal shard runtimes.
Restart the control API after changing these values.

## Manual Lifecycle

Run a health check:

```bash
scripts/ocr_agent_worker.sh doctor /etc/ocr-agent/worker.env
```

Start:

```bash
scripts/ocr_agent_worker.sh start /etc/ocr-agent/worker.env
```

Check status:

```bash
scripts/ocr_agent_worker.sh status /etc/ocr-agent/worker.env
```

Show logs:

```bash
scripts/ocr_agent_worker.sh logs /etc/ocr-agent/worker.env
```

Stop:

```bash
scripts/ocr_agent_worker.sh stop /etc/ocr-agent/worker.env
```

The default runner is `tmux`. Set `OCR_AGENT_RUNNER=nohup` if tmux is not
available.

`run` is intended for service managers such as systemd. It runs the agent in the
foreground and lets the service manager handle restarts.

If a worker process is already stopped, the control API cannot wake it up by
HTTP because there is no remote process left to receive the command. Production
deployments should run the agent under a host-local supervisor such as systemd.
An SSH-based restart button in the control UI is possible later, but it would be
a separate privileged remote-execution feature with its own credentials and
audit trail.

## systemd

Install the example unit after editing paths and user:

```bash
sudo cp services/ocr-agent-worker.service.example /etc/systemd/system/ocr-agent-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now ocr-agent-worker
sudo systemctl status ocr-agent-worker
```

For a host that intentionally runs multiple worker processes, install the
instance template and give each worker its own env file:

```bash
sudo cp services/ocr-agent-worker@.service.example /etc/systemd/system/ocr-agent-worker@.service
sudo cp configs/ocr-agent-worker.env.example /etc/ocr-agent/worker-01.env
sudo cp configs/ocr-agent-worker.env.example /etc/ocr-agent/worker-02.env
sudoedit /etc/ocr-agent/worker-01.env /etc/ocr-agent/worker-02.env
sudo systemctl daemon-reload
sudo systemctl enable --now ocr-agent-worker@worker-01 ocr-agent-worker@worker-02
sudo systemctl status 'ocr-agent-worker@*'
```

Each env file must use a unique `OCR_AGENT_SERVER_ID`, `OCR_AGENT_WORK_DIR`,
`OCR_AGENT_EVENT_SPOOL_DIR`, `OCR_AGENT_LOG_DIR`, and `OCR_AGENT_TMUX_SESSION`
so instances do not share state or log files.

Install log rotation for file-backed worker logs:

```bash
sudo cp services/ocr-agent-worker.logrotate.example /etc/logrotate.d/ocr-agent-worker
sudo logrotate -d /etc/logrotate.d/ocr-agent-worker
```

The template rotates `/var/log/ocr-agent/*/*.log`, so it covers both single
worker logs such as `/var/log/ocr-agent/worker-01/agent.log` and multi-worker
layouts such as `/var/log/ocr-agent/worker-02/agent.log`. `copytruncate` is used
because tmux/nohup workers keep the log file open. systemd foreground runs also
write to journald; keep the host's journald retention policy configured
separately.

## Production Example

One production worker env should look like:

```bash
OCR_AGENT_SERVER_ID=ocr-worker-01
OCR_CONTROL_URL=http://ocr-control.internal:8080
OCR_REPO_DIR=/opt/ocr-platform/ocrparser
OCR_AGENT_WORK_DIR=/var/lib/ocr-agent/worker-01
OCR_AGENT_PYTHON=/opt/ocr-platform/ocrparser/.venv/bin/python
OCR_AGENT_SHARED_ROOTS=/shared/ocr-data
OCR_AGENT_POLL_INTERVAL=2
OCR_AGENT_HEARTBEAT_INTERVAL=5
OCR_AGENT_CONTROL_RETRY_INITIAL=1
OCR_AGENT_CONTROL_RETRY_MAX=30
OCR_AGENT_EVENT_SPOOL_DIR=/var/lib/ocr-agent/worker-01/event-spool
OCR_AGENT_EVENT_SPOOL_MAX_MB=1024
OCR_AGENT_TERMINATION_TIMEOUT=10
OCR_AGENT_LOG_DIR=/var/log/ocr-agent/worker-01
```

Before production jobs, confirm the UI shows the worker online and the shared
path check is green for the target input folder.
