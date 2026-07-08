#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/ocr_agent_worker.sh start|stop|restart|status|logs|doctor|run [env-file]

Environment is loaded from:
  1. explicit env-file argument
  2. OCR_AGENT_ENV_FILE
  3. ./configs/ocr-agent-worker.env
  4. /etc/ocr-agent/worker.env

This script standardizes running python -m ocr_platform.agent on execution hosts.
USAGE
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

info() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMMAND="${1:-}"
ENV_ARG="${2:-}"

if [[ -z "$COMMAND" || "$COMMAND" == "-h" || "$COMMAND" == "--help" ]]; then
  usage
  exit 0
fi

case "$COMMAND" in
  start|stop|restart|status|logs|doctor|run) ;;
  *) usage; die "unknown command: $COMMAND" ;;
esac

load_env() {
  local env_file=""
  if [[ -n "$ENV_ARG" ]]; then
    env_file="$ENV_ARG"
  elif [[ -n "${OCR_AGENT_ENV_FILE:-}" ]]; then
    env_file="$OCR_AGENT_ENV_FILE"
  elif [[ -f "$DEFAULT_REPO_DIR/configs/ocr-agent-worker.env" ]]; then
    env_file="$DEFAULT_REPO_DIR/configs/ocr-agent-worker.env"
  elif [[ -f /etc/ocr-agent/worker.env ]]; then
    env_file=/etc/ocr-agent/worker.env
  fi

  if [[ -n "$env_file" ]]; then
    [[ -f "$env_file" ]] || die "env file not found: $env_file"
    # shellcheck disable=SC1090
    set -a
    source "$env_file"
    set +a
    OCR_AGENT_ENV_FILE_LOADED="$env_file"
  else
    OCR_AGENT_ENV_FILE_LOADED=""
  fi
}

load_env

SERVER_ID="${OCR_AGENT_SERVER_ID:-$(hostname -s)}"
CONTROL_URL="${OCR_CONTROL_URL:-http://127.0.0.1:8080}"
REPO_DIR="${OCR_REPO_DIR:-$DEFAULT_REPO_DIR}"
WORK_DIR="${OCR_AGENT_WORK_DIR:-$REPO_DIR/.local/ocr-agent/$SERVER_ID}"
PYTHON="${OCR_AGENT_PYTHON:-$REPO_DIR/.venv/bin/python}"
POLL_SECONDS="${OCR_AGENT_POLL_INTERVAL_SECONDS:-${OCR_AGENT_POLL_INTERVAL:-5}}"
HEARTBEAT_SECONDS="${OCR_AGENT_HEARTBEAT_INTERVAL_SECONDS:-${OCR_AGENT_HEARTBEAT_INTERVAL:-10}}"
CONTROL_RETRY_INITIAL="${OCR_AGENT_CONTROL_RETRY_INITIAL:-1}"
CONTROL_RETRY_MAX="${OCR_AGENT_CONTROL_RETRY_MAX:-30}"
TERMINATION_TIMEOUT="${OCR_AGENT_TERMINATION_TIMEOUT:-5}"
STOP_POLL_INTERVAL="${OCR_AGENT_STOP_POLL_INTERVAL:-1}"
SHARED_ROOTS="${OCR_AGENT_SHARED_ROOTS:-}"
RUNNER="${OCR_AGENT_RUNNER:-tmux}"
LOG_DIR="${OCR_AGENT_LOG_DIR:-$WORK_DIR/logs}"
PID_FILE="$LOG_DIR/agent.pid"
LOG_FILE="$LOG_DIR/agent.log"
TMUX_SESSION="${OCR_AGENT_TMUX_SESSION:-ocr-agent-$SERVER_ID}"
GIT_REF="${OCR_AGENT_GIT_REF:-}"

mkdir -p "$LOG_DIR" "$WORK_DIR"

build_agent_command() {
  AGENT_COMMAND=(
    "$PYTHON" -u -m ocr_platform.agent
    --server_id "$SERVER_ID"
    --control_url "$CONTROL_URL"
    --work_dir "$WORK_DIR"
    --repo_dir "$REPO_DIR"
    --poll_interval_seconds "$POLL_SECONDS"
    --heartbeat_interval_seconds "$HEARTBEAT_SECONDS"
    --control_retry_initial_seconds "$CONTROL_RETRY_INITIAL"
    --control_retry_max_seconds "$CONTROL_RETRY_MAX"
    --process_termination_timeout_seconds "$TERMINATION_TIMEOUT"
    --stop_poll_interval_seconds "$STOP_POLL_INTERVAL"
    --python_executable "$PYTHON"
  )
  [[ -n "$GIT_REF" ]] && AGENT_COMMAND+=(--git_ref "$GIT_REF")
  [[ -n "${OCR_AGENT_SCRIPT_VERSION:-}" ]] && AGENT_COMMAND+=(--script_version "$OCR_AGENT_SCRIPT_VERSION")

  if [[ -n "$SHARED_ROOTS" ]]; then
    IFS=':' read -r -a ROOT_ARRAY <<< "$SHARED_ROOTS"
    for root in "${ROOT_ARRAY[@]}"; do
      [[ -n "$root" ]] && AGENT_COMMAND+=(--shared_root "$root")
    done
  fi
}

validate_config() {
  [[ -d "$REPO_DIR" ]] || die "repo dir not found: $REPO_DIR"
  [[ -f "$REPO_DIR/ocr_platform/agent/__main__.py" ]] || die "ocr_platform agent package not found under $REPO_DIR"
  [[ -x "$PYTHON" ]] || die "python executable not found or not executable: $PYTHON"
  if [[ -n "$SHARED_ROOTS" ]]; then
    IFS=':' read -r -a ROOT_ARRAY <<< "$SHARED_ROOTS"
    for root in "${ROOT_ARRAY[@]}"; do
      [[ -n "$root" ]] || continue
      [[ -d "$root" ]] || die "shared root is not a directory: $root"
      [[ -r "$root" ]] || die "shared root is not readable: $root"
    done
  fi
}

is_running_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

is_running_tmux() {
  command -v tmux >/dev/null 2>&1 || return 1
  tmux has-session -t "$TMUX_SESSION" 2>/dev/null
}

stop_worker() {
  local stopped=0
  if is_running_tmux; then
    info "Stopping tmux session $TMUX_SESSION"
    tmux kill-session -t "$TMUX_SESSION" || true
    stopped=1
  fi
  if is_running_pid; then
    local pid
    pid="$(cat "$PID_FILE")"
    info "Stopping pid $pid"
    kill "$pid" 2>/dev/null || true
    stopped=1
  fi
  pkill -f "ocr_platform.agent --server_id $SERVER_ID" 2>/dev/null || true
  rm -f "$PID_FILE"
  [[ "$stopped" == "1" ]] && info "Stopped $SERVER_ID" || info "No running worker found for $SERVER_ID"
}

start_worker() {
  validate_config
  build_agent_command
  stop_worker >/dev/null 2>&1 || true
  mkdir -p "$LOG_DIR" "$WORK_DIR"
  info "Starting worker server_id=$SERVER_ID control_url=$CONTROL_URL runner=$RUNNER"
  (
    cd "$REPO_DIR"
    printf '%q ' "${AGENT_COMMAND[@]}" > "$LOG_DIR/agent.command"
    printf '\n' >> "$LOG_DIR/agent.command"
  )

  if [[ "$RUNNER" == "tmux" ]]; then
    command -v tmux >/dev/null 2>&1 || die "OCR_AGENT_RUNNER=tmux but tmux is not installed"
    tmux new -d -s "$TMUX_SESSION" "cd '$REPO_DIR' && exec ${AGENT_COMMAND[*]} >> '$LOG_FILE' 2>&1"
    info "Started tmux session=$TMUX_SESSION log=$LOG_FILE"
  elif [[ "$RUNNER" == "nohup" ]]; then
    (
      cd "$REPO_DIR"
      nohup "${AGENT_COMMAND[@]}" >> "$LOG_FILE" 2>&1 < /dev/null &
      echo "$!" > "$PID_FILE"
    )
    info "Started pid=$(cat "$PID_FILE") log=$LOG_FILE"
  else
    die "unsupported OCR_AGENT_RUNNER: $RUNNER"
  fi
  sleep 1
  status_worker
}

run_worker() {
  validate_config
  build_agent_command
  info "Running worker in foreground server_id=$SERVER_ID control_url=$CONTROL_URL"
  cd "$REPO_DIR"
  exec "${AGENT_COMMAND[@]}"
}

status_worker() {
  echo "server_id=$SERVER_ID"
  echo "control_url=$CONTROL_URL"
  echo "repo_dir=$REPO_DIR"
  echo "work_dir=$WORK_DIR"
  echo "python=$PYTHON"
  echo "shared_roots=${SHARED_ROOTS:-<none>}"
  echo "control_retry=${CONTROL_RETRY_INITIAL}s..${CONTROL_RETRY_MAX}s"
  echo "stop_poll_interval=${STOP_POLL_INTERVAL}s"
  echo "termination_timeout=${TERMINATION_TIMEOUT}s"
  echo "env_file=${OCR_AGENT_ENV_FILE_LOADED:-<none>}"
  [[ -n "$GIT_REF" ]] && echo "git_ref=$GIT_REF"
  [[ -n "${OCR_AGENT_SCRIPT_VERSION:-}" ]] && echo "script_version=$OCR_AGENT_SCRIPT_VERSION"
  if is_running_tmux; then
    echo "process=running tmux_session=$TMUX_SESSION"
  elif is_running_pid; then
    echo "process=running pid=$(cat "$PID_FILE")"
  else
    echo "process=stopped"
  fi
}

doctor_worker() {
  validate_config
  build_agent_command
  status_worker
  (
    cd "$REPO_DIR"
    "$PYTHON" - <<'PY'
import importlib
for name in ("ocr_platform.agent", "ocr_parser", "httpx"):
    importlib.import_module(name)
print("python_imports=ok")
PY
  )
  "$PYTHON" - "$CONTROL_URL" "$SERVER_ID" <<'PY'
import json
import sys
from urllib.request import urlopen

control_url, server_id = sys.argv[1], sys.argv[2]
with urlopen(f"{control_url.rstrip('/')}/api/servers", timeout=10) as response:
    rows = json.load(response)
print(f"control_api=ok servers={len(rows)} server_id={server_id}")
PY
  echo "agent_command=$(printf '%q ' "${AGENT_COMMAND[@]}")"
}

logs_worker() {
  if [[ "$RUNNER" == "tmux" ]] && is_running_tmux; then
    tmux capture-pane -t "$TMUX_SESSION" -p -S -200 || true
  fi
  [[ -f "$LOG_FILE" ]] && tail -200 "$LOG_FILE" || true
}

case "$COMMAND" in
  start) start_worker ;;
  stop) stop_worker ;;
  restart) stop_worker; start_worker ;;
  status) status_worker ;;
  logs) logs_worker ;;
  doctor) doctor_worker ;;
  run) run_worker ;;
esac
