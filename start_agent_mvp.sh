#!/usr/bin/env bash
set -euo pipefail

SERVER_ID="${OCR_AGENT_SERVER_ID:-ocr-node-a}"
CONTROL_URL="${OCR_CONTROL_URL:-http://127.0.0.1:8080}"
WORK_DIR="${OCR_AGENT_WORK_DIR:-/home/ocr_user/ocr-agent}"
REPO_DIR="${OCR_REPO_DIR:-/home/ocr_user/workspace/ocrparser}"
PYTHON="${OCR_AGENT_PYTHON:-$WORK_DIR/venv/bin/python}"
POLL_SECONDS="${OCR_AGENT_POLL_INTERVAL_SECONDS:-${OCR_AGENT_POLL_INTERVAL:-5}}"
LOG_FILE="$WORK_DIR/logs/agent.log"
PID_FILE="$WORK_DIR/logs/agent.pid"

if [[ ! -d "$WORK_DIR" ]]; then
  echo "ERROR: WORK_DIR not found: $WORK_DIR" >&2
  exit 1
fi
if [[ ! -d "$REPO_DIR" ]]; then
  echo "ERROR: REPO_DIR not found: $REPO_DIR" >&2
  exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: executable python not found: $PYTHON" >&2
  exit 1
fi
if [[ ! -f "$REPO_DIR/ocr_platform/agent/__main__.py" ]]; then
  echo "ERROR: cannot find ocr_platform agent package under $REPO_DIR" >&2
  exit 1
fi

mkdir -p "$WORK_DIR/logs"
echo "Stopping old agent for server_id=$SERVER_ID"
pkill -f "ocr_platform.agent --server_id $SERVER_ID" 2>/dev/null || true

cd "$REPO_DIR"
nohup "$PYTHON" -u -m ocr_platform.agent \
  --server_id "$SERVER_ID" \
  --control_url "$CONTROL_URL" \
  --work_dir "$WORK_DIR" \
  --poll_interval_seconds "$POLL_SECONDS" \
  --python_executable "$PYTHON" \
  > "$LOG_FILE" 2>&1 < /dev/null &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
echo "Started agent pid=$NEW_PID (log: $LOG_FILE)"

sleep 1
if ! kill -0 "$NEW_PID" 2>/dev/null; then
  echo "ERROR: agent exited. See $LOG_FILE" >&2
  exit 1
fi

echo "Verifying registration on control: $CONTROL_URL"
if command -v python3 >/dev/null 2>&1; then
  if ! python3 - "$CONTROL_URL" "$SERVER_ID" <<'PY'
import json
import sys
from urllib.request import urlopen

control_url, server_id = sys.argv[1], sys.argv[2]
try:
    with urlopen(f"{control_url}/api/servers", timeout=10) as response:
        rows = json.load(response)
    exists = any(row.get("id") == server_id for row in rows or [])
    if not exists:
        raise SystemExit(f"server_id '{server_id}' not found in /api/servers")
except Exception as error:
    raise SystemExit(str(error))
print(f"Registered server: {server_id}")
PY
  then
    echo "Registration check failed. Check log: $LOG_FILE"
    exit 1
  fi
else
  echo "python3 not available for registration check; skip"
fi

echo "Done."
