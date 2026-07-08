from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SAFE_TARGET_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


@dataclass(frozen=True)
class RemoteWorkerResult:
    action: str
    host: str
    command: list[str]
    return_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.return_code == 0


@dataclass(frozen=True)
class RemoteWorkerScaleResult(RemoteWorkerResult):
    plan_items: list[dict[str, Any]]


@dataclass(frozen=True)
class RemoteWorkerTarget:
    id: str
    host: str
    hostname: str
    ssh_user: str
    server_id: str
    service_user: str = "ocr-agent"
    service_group: str = "ocr-agent"
    repo_dir: str = "/opt/ocr-platform/ocrparser"
    control_url: str = ""
    shared_roots: tuple[str, ...] = ("/shared/ocr-data",)


SCALE_ACTIONS = {
    "create_env",
    "update_env",
    "start_service",
    "stop_service",
    "disable_service",
    "wait_heartbeat",
    "skip",
}
SCALE_STATUSES = {"pending", "ok", "warning", "failed", "skipped"}


def parse_scale_plan_items(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            items.append(
                {
                    "action": "skip",
                    "status": "warning",
                    "instance": None,
                    "server_id": None,
                    "message": f"unparsed remote output: {line}",
                }
            )
            continue
        if not isinstance(payload, dict):
            items.append(
                {
                    "action": "skip",
                    "status": "warning",
                    "instance": None,
                    "server_id": None,
                    "message": f"unparsed remote output: {line}",
                }
            )
            continue
        action = str(payload.get("action") or "")
        status = str(payload.get("status") or "")
        if action not in SCALE_ACTIONS or status not in SCALE_STATUSES:
            items.append(
                {
                    "action": "skip",
                    "status": "warning",
                    "instance": None,
                    "server_id": None,
                    "message": f"unrecognized scale item: {line}",
                }
            )
            continue
        items.append(
            {
                "action": action,
                "status": status,
                "instance": payload.get("instance"),
                "server_id": payload.get("server_id"),
                "message": str(payload.get("message") or ""),
            }
        )
    return items


class RemoteWorkerExecutor:
    def _run_ssh(self, request, remote_command: str, *, action: str) -> RemoteWorkerResult:
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={request.connect_timeout_seconds}",
            request.ssh_target(),
            remote_command,
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=request.timeout_seconds,
        )
        return RemoteWorkerResult(
            action=action,
            host=request.host,
            command=command,
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def preflight(self, request) -> RemoteWorkerResult:
        roots = request.shared_roots or ["/shared/ocr-data"]
        root_checks = "\n".join(_shared_root_check(root) for root in roots)
        script = f"""
set -u
echo "host=$(hostname -f 2>/dev/null || hostname) user=$(id -un)"
echo "-- identity"
id {shlex.quote(request.service_user)} 2>&1 || true
getent group {shlex.quote(request.service_group)} 2>&1 || true
echo "-- tools"
command -v python3 2>&1 || true
command -v systemctl 2>&1 || true
command -v sudo 2>&1 || true
echo "-- repo"
test -d {shlex.quote(str(request.repo_dir))} && echo "repo ok {shlex.quote(str(request.repo_dir))}" || echo "repo missing {shlex.quote(str(request.repo_dir))}"
{root_checks}
""".strip()
        return self._run_ssh(request, _shell_command(script), action="preflight")

    def _install_command(self, request, *, apply: bool) -> str:
        args = [
            "sudo" if apply else "python3",
        ]
        if apply:
            args.extend(
                [
                    "-n",
                    "python3",
                ]
            )
        args.extend(
            [
                str(Path(request.repo_dir) / "tools" / "install_production.py"),
                "worker",
                "--non-interactive",
                "--repo-dir",
                request.repo_dir,
                "--service-user",
                request.service_user,
                "--service-group",
                request.service_group,
                "--server-id",
                request.server_id,
                "--control-url",
                request.control_url,
                "--runner",
                request.runner,
            ]
        )
        if apply:
            args.append("--yes")
        else:
            args.append("--dry-run")
        for root in request.shared_roots:
            args.extend(["--shared-root", root])
        return f"cd {shlex.quote(str(request.repo_dir))} && {shlex.join(args)}"

    def install_dry_run(self, request) -> RemoteWorkerResult:
        return self._run_ssh(request, self._install_command(request, apply=False), action="install_dry_run")

    def install_apply(self, request) -> RemoteWorkerResult:
        return self._run_ssh(request, self._install_command(request, apply=True), action="install_apply")

    def service_action(self, request) -> RemoteWorkerResult:
        action = request.action
        if action == "disable":
            args = ["sudo", "systemctl", "disable", "--now", request.service_name]
        else:
            args = ["sudo", "systemctl", action, request.service_name]
        return self._run_ssh(request, shlex.join(args), action=action)

    def _scale_command(self, request, *, apply: bool) -> str:
        script = _scale_apply_script(request) if apply else _scale_plan_script(request)
        runner = "sudo -n python3" if apply else "python3"
        return f"cd {shlex.quote(str(request.repo_dir))} && {runner} - <<'PY'\n{script}\nPY"

    def _scale_result(self, request, *, apply: bool) -> RemoteWorkerScaleResult:
        action = "scale_apply" if apply else "scale_plan"
        result = self._run_ssh(request, self._scale_command(request, apply=apply), action=action)
        items = parse_scale_plan_items(result.stdout)
        if not items and (result.stdout.strip() or result.stderr.strip()):
            items = [
                {
                    "action": "skip",
                    "status": "warning",
                    "instance": None,
                    "server_id": None,
                    "message": "remote command produced no structured scale items",
                }
            ]
        return RemoteWorkerScaleResult(
            action=result.action,
            host=result.host,
            command=result.command,
            return_code=result.return_code,
            stdout=result.stdout,
            stderr=result.stderr,
            plan_items=items,
        )

    def scale_plan(self, request) -> RemoteWorkerScaleResult:
        return self._scale_result(request, apply=False)

    def scale_apply(self, request) -> RemoteWorkerScaleResult:
        return self._scale_result(request, apply=True)


def default_ssh_user() -> str:
    return os.environ.get("OCR_PLATFORM_REMOTE_WORKER_SSH_USER") or os.environ.get("USER") or ""


def load_remote_worker_targets() -> list[RemoteWorkerTarget]:
    config_path = os.environ.get("OCR_PLATFORM_REMOTE_WORKER_CONFIG", "").strip()
    if config_path:
        return _load_remote_worker_targets_json(Path(config_path))
    ssh_config_path = Path(
        os.environ.get("OCR_PLATFORM_REMOTE_WORKER_SSH_CONFIG", "").strip()
        or Path.home() / ".ssh" / "config"
    )
    prefix = os.environ.get("OCR_PLATFORM_REMOTE_WORKER_HOST_PREFIX", "ocr-prod-")
    return _load_remote_worker_targets_ssh_config(ssh_config_path, host_prefix=prefix)


def _load_remote_worker_targets_json(path: Path) -> list[RemoteWorkerTarget]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("targets", [])
    if not isinstance(payload, list):
        return []
    targets = []
    for item in payload:
        if isinstance(item, dict):
            target = _target_from_mapping(item)
            if target is not None:
                targets.append(target)
    return targets


def _target_from_mapping(item: dict[str, Any]) -> RemoteWorkerTarget | None:
    host = str(item.get("host") or item.get("id") or "").strip()
    hostname = str(item.get("hostname") or item.get("host_name") or host).strip()
    if not _is_safe_remote_token(host):
        return None
    ssh_user = str(item.get("ssh_user") or item.get("user") or default_ssh_user()).strip()
    if ssh_user and not _is_safe_remote_token(ssh_user):
        return None
    server_id = str(item.get("server_id") or _server_id_from_hostname(hostname, host)).strip()
    shared_roots = item.get("shared_roots") or item.get("shared_root") or ["/shared/ocr-data"]
    if isinstance(shared_roots, str):
        shared_roots = [part for part in shared_roots.split(":") if part]
    if not isinstance(shared_roots, list):
        shared_roots = ["/shared/ocr-data"]
    return RemoteWorkerTarget(
        id=str(item.get("id") or host),
        host=host,
        hostname=hostname,
        ssh_user=ssh_user,
        server_id=server_id,
        service_user=str(item.get("service_user") or ssh_user or "ocr-agent"),
        service_group=str(item.get("service_group") or item.get("service_user") or ssh_user or "ocr-agent"),
        repo_dir=str(item.get("repo_dir") or "/opt/ocr-platform/ocrparser"),
        control_url=str(item.get("control_url") or ""),
        shared_roots=tuple(str(root).strip() for root in shared_roots if str(root).strip()),
    )


def _load_remote_worker_targets_ssh_config(path: Path, *, host_prefix: str) -> list[RemoteWorkerTarget]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    targets: list[RemoteWorkerTarget] = []
    current: dict[str, str] | None = None
    for raw_line in lines + ["Host __end__"]:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        key = parts[0].lower()
        value = parts[1].strip() if len(parts) > 1 else ""
        if key == "host":
            if current is not None:
                target = _target_from_ssh_host(current, host_prefix=host_prefix)
                if target is not None:
                    targets.append(target)
            current = {"host": value.split()[0] if value else ""}
        elif current is not None and key in {"hostname", "user"}:
            current[key] = value
    return targets


def _target_from_ssh_host(item: dict[str, str], *, host_prefix: str) -> RemoteWorkerTarget | None:
    host = item.get("host", "").strip()
    if not _is_safe_remote_token(host) or "*" in host or "?" in host:
        return None
    if host_prefix and not host.startswith(host_prefix):
        return None
    hostname = item.get("hostname") or host
    ssh_user = item.get("user") or default_ssh_user()
    if ssh_user and not _is_safe_remote_token(ssh_user):
        return None
    return RemoteWorkerTarget(
        id=host,
        host=host,
        hostname=hostname,
        ssh_user=ssh_user,
        server_id=_server_id_from_hostname(hostname, host),
        service_user=ssh_user or "ocr-agent",
        service_group=ssh_user or "ocr-agent",
    )


def _server_id_from_hostname(hostname: str, fallback: str) -> str:
    return hostname if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", hostname) else fallback


def _is_safe_remote_token(value: str) -> bool:
    return bool(value and _SAFE_TARGET_RE.fullmatch(value))


def validate_ssh_token(value: str, *, field_name: str) -> str:
    if not value or not _SAFE_TARGET_RE.fullmatch(value):
        raise ValueError(f"{field_name} contains unsupported characters")
    return value


def _shell_command(script: str) -> str:
    return f"sh -lc {shlex.quote(script)}"


def _shared_root_check(root: str) -> str:
    quoted = shlex.quote(root)
    return f"""
echo "-- shared root {quoted}"
findmnt {quoted} -o TARGET,SOURCE,FSTYPE,OPTIONS -n 2>&1 || true
findmnt -T {quoted} -o TARGET,SOURCE,FSTYPE,OPTIONS -n 2>&1 || true
df -hT {quoted} 2>&1 || true
stat -c "%n|%F|mode=%a|owner=%U|group=%G" {quoted} 2>&1 || true
""".strip()


def _scale_constants(request, *, include_commands: bool = False) -> dict[str, Any]:
    target_count = int(request.target_count)
    desired = []
    for index in range(1, target_count + 1):
        item = {
            "index": index,
            "instance": f"worker-{index:02d}",
            "server_id": f"{request.server_id_prefix}-{index:02d}",
            "env_path": f"/etc/ocr-agent/worker-{index:02d}.env",
            "work_dir": f"/var/lib/ocr-agent/worker-{index:02d}",
            "log_dir": f"/var/log/ocr-agent/worker-{index:02d}",
            "event_spool_dir": f"/var/lib/ocr-agent/worker-{index:02d}/event-spool",
            "tmux_session": f"ocr-agent-worker-{index:02d}",
        }
        if include_commands:
            item["start_command"] = f"systemctl enable --now ocr-agent-worker@worker-{index:02d}"
        desired.append(item)
    return {
        "repo_dir": str(request.repo_dir),
        "target_count": target_count,
        "seed_server_id": request.seed_server_id or "",
        "server_id_prefix": request.server_id_prefix,
        "shared_roots": list(request.shared_roots or []),
        "desired": desired,
    }


def _scale_plan_script(request) -> str:
    constants = json.dumps(_scale_constants(request, include_commands=False), sort_keys=True)
    return f"""
# scale_plan
import glob
import json
import os
import re

CONFIG = {constants!r}

def emit(action, status, instance=None, server_id=None, message=""):
    print(json.dumps({{
        "action": action,
        "status": status,
        "instance": instance,
        "server_id": server_id,
        "message": message,
    }}, sort_keys=True))

repo_dir = CONFIG["repo_dir"]
target_count = int(CONFIG["target_count"])
desired = CONFIG["desired"]

if os.path.isdir(repo_dir):
    emit("skip", "ok", message=f"repo exists: {{repo_dir}}")
else:
    emit("skip", "warning", message=f"repo missing: {{repo_dir}}")

unit_path = "/etc/systemd/system/ocr-agent-worker@.service"
template_path = os.path.join(repo_dir, "services", "ocr-agent-worker@.service.example")
if os.path.exists(unit_path):
    emit("skip", "ok", message=f"systemd template exists: {{unit_path}}")
elif os.path.exists(template_path):
    emit("skip", "pending", message=f"install systemd template from {{template_path}}")
else:
    emit("skip", "warning", message=f"systemd template missing: {{unit_path}}")

existing = set()
for path in glob.glob("/etc/ocr-agent/worker-*.env"):
    name = os.path.basename(path).removesuffix(".env")
    if re.fullmatch(r"worker-\\d\\d", name):
        existing.add(name)

for item in desired:
    instance = item["instance"]
    server_id = item["server_id"]
    env_path = item["env_path"]
    if instance in existing:
        emit("update_env", "skipped", instance, server_id, f"env already exists: {{env_path}}")
    else:
        emit("create_env", "pending", instance, server_id, f"create {{env_path}}")
    emit("start_service", "pending", instance, server_id, f"start instance service ocr-agent-worker@{{instance}}")
    emit("wait_heartbeat", "pending", instance, server_id, "wait for control heartbeat")

for instance in sorted(existing):
    try:
        index = int(instance.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        continue
    if index > target_count:
        emit("stop_service", "pending", instance, None, f"stop instance service ocr-agent-worker@{{instance}}")
        emit("disable_service", "pending", instance, None, f"disable instance service ocr-agent-worker@{{instance}}")
""".strip()


def _scale_apply_script(request) -> str:
    constants = json.dumps(_scale_constants(request, include_commands=True), sort_keys=True)
    return f"""
import glob
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

CONFIG = {constants!r}

def emit(action, status, instance=None, server_id=None, message=""):
    print(json.dumps({{
        "action": action,
        "status": status,
        "instance": instance,
        "server_id": server_id,
        "message": message,
    }}, sort_keys=True), flush=True)

def run(command, action, instance=None, server_id=None):
    completed = subprocess.run(command, shell=True, check=False, capture_output=True, text=True)
    status = "ok" if completed.returncode == 0 else "failed"
    message = command if completed.returncode == 0 else (completed.stderr.strip() or completed.stdout.strip() or command)
    emit(action, status, instance, server_id, message)
    return completed.returncode

def read_seed_env():
    candidates = [Path("/etc/ocr-agent/worker.env")]
    candidates.extend(sorted(Path("/etc/ocr-agent").glob("worker-*.env")))
    candidates.append(Path(CONFIG["repo_dir"]) / "configs" / "ocr-agent-worker.env.example")
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8").splitlines()
    return []

def set_env(lines, key, value):
    prefix = key + "="
    replaced = False
    output = []
    for line in lines:
        if line.startswith(prefix):
            output.append(prefix + value)
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(prefix + value)
    return output

def write_instance_env(item):
    path = Path(item["env_path"])
    existed = path.exists()
    lines = read_seed_env()
    replacements = {{
        "OCR_AGENT_SERVER_ID": item["server_id"],
        "OCR_REPO_DIR": CONFIG["repo_dir"],
        "OCR_AGENT_WORK_DIR": item["work_dir"],
        "OCR_AGENT_LOG_DIR": item["log_dir"],
        "OCR_AGENT_EVENT_SPOOL_DIR": item["event_spool_dir"],
        "OCR_AGENT_TMUX_SESSION": item["tmux_session"],
        "OCR_AGENT_SHARED_ROOTS": ":".join(CONFIG["shared_roots"]),
    }}
    for key, value in replacements.items():
        lines = set_env(lines, key, str(value))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\\n".join(lines).rstrip() + "\\n", encoding="utf-8")
    os.chmod(path, 0o640)
    action = "update_env" if existed else "create_env"
    emit(action, "ok", item["instance"], item["server_id"], f"wrote {{path}}")

repo_dir = Path(CONFIG["repo_dir"])
unit_path = Path("/etc/systemd/system/ocr-agent-worker@.service")
template_path = repo_dir / "services" / "ocr-agent-worker@.service.example"
if template_path.exists():
    shutil.copyfile(template_path, unit_path)
    emit("skip", "ok", message=f"installed systemd template: {{unit_path}}")
    run("systemctl daemon-reload", "skip")
else:
    emit("skip", "warning", message=f"systemd template missing: {{template_path}}")

for item in CONFIG["desired"]:
    write_instance_env(item)
    run(item["start_command"], "start_service", item["instance"], item["server_id"])
    emit("wait_heartbeat", "pending", item["instance"], item["server_id"], "wait for control heartbeat")

target_count = int(CONFIG["target_count"])
for path in sorted(Path("/etc/ocr-agent").glob("worker-*.env")):
    instance = path.name.removesuffix(".env")
    if not re.fullmatch(r"worker-\\d\\d", instance):
        continue
    try:
        index = int(instance.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        continue
    if index > target_count:
        run(f"systemctl stop ocr-agent-worker@{{instance}}", "stop_service", instance)
        run(f"systemctl disable ocr-agent-worker@{{instance}}", "disable_service", instance)
""".strip()
