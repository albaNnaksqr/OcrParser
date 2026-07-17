from __future__ import annotations

import argparse
import ipaddress
import contextlib
import getpass
import grp
import json
import os
import pwd
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_REPO_DIR = Path("/opt/ocr-platform/ocrparser")
DEFAULT_CONTROL_ENV = Path("/etc/ocr-platform/control.env")
DEFAULT_WORKER_ENV = Path("/etc/ocr-agent/worker.env")
DEFAULT_CONTROL_SERVICE = Path("/etc/systemd/system/ocr-platform-control.service")
DEFAULT_WORKER_SERVICE = Path("/etc/systemd/system/ocr-agent-worker.service")


class InstallConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ValidationError:
    code: str
    message: str
    fix: str


@dataclass(frozen=True)
class BaseInstallConfig:
    role: str
    service_user: str
    service_group: str
    repo_dir: Path = DEFAULT_REPO_DIR
    venv_dir: Path = DEFAULT_REPO_DIR / ".venv"
    python_executable: Path | None = None
    dry_run: bool = False
    non_interactive: bool = False
    yes: bool = False
    install_dependencies: bool = False


@dataclass(frozen=True)
class ControlInstallConfig(BaseInstallConfig):
    database_url: str = ""
    env_path: Path = DEFAULT_CONTROL_ENV
    service_path: Path = DEFAULT_CONTROL_SERVICE
    host: str = "127.0.0.1"
    port: int = 8080
    require_postgres: bool = True
    require_current_migrations: bool = True
    require_api_token: bool = False
    api_token: str | None = None
    disable_saved_model_profile_keys: bool = True
    run_migrations: bool = True
    install_systemd: bool = True
    enable_service: bool = True
    restart_service: bool = True


@dataclass(frozen=True)
class WorkerInstallConfig(BaseInstallConfig):
    server_id: str = ""
    control_url: str = ""
    control_api_token: str | None = None
    env_path: Path = DEFAULT_WORKER_ENV
    service_path: Path = DEFAULT_WORKER_SERVICE
    work_dir: Path = Path("/var/lib/ocr-agent/worker-01")
    log_dir: Path = Path("/var/log/ocr-agent/worker-01")
    event_spool_dir: Path = Path("/var/lib/ocr-agent/worker-01/event-spool")
    shared_roots: list[str] = field(default_factory=list)
    manifest_dir: str | None = None
    runner: str = "tmux"
    install_systemd: bool = True
    enable_service: bool = True
    restart_service: bool = True


@dataclass(frozen=True)
class PlanAction:
    description: str
    target: str
    content: str | None = None
    command: list[str] | None = None


@dataclass(frozen=True)
class InstallPlan:
    role: str
    summary: list[str]
    warnings: list[str]
    validation_errors: list[ValidationError]
    actions: list[PlanAction]


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--service-user")
    parser.add_argument("--service-group")
    parser.add_argument("--repo-dir", type=Path, default=DEFAULT_REPO_DIR)
    parser.add_argument("--venv-dir", type=Path)
    parser.add_argument("--python-executable", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--install-deps", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install OCR platform production services.")
    parser.add_argument("--config", type=Path, help="JSON config file with installer answers.")
    subparsers = parser.add_subparsers(dest="role", required=True)

    control = subparsers.add_parser("control", help="Install the control API/UI service.")
    _add_common_args(control)
    control.add_argument("--database-url")
    control.add_argument("--env-path", type=Path)
    control.add_argument("--service-path", type=Path)
    control.add_argument("--host")
    control.add_argument("--port", type=int)
    control.add_argument("--no-require-postgres", action="store_true")
    control.add_argument("--no-require-current-migrations", action="store_true")
    control.add_argument("--enable-api-token", action="store_true")
    control.add_argument("--api-token")
    control.add_argument("--skip-migrations", action="store_true")
    control.add_argument("--skip-systemd", action="store_true")
    control.add_argument("--no-enable-service", action="store_true")
    control.add_argument("--no-restart-service", action="store_true")

    worker = subparsers.add_parser("worker", help="Install an agent worker service.")
    _add_common_args(worker)
    worker.add_argument("--server-id")
    worker.add_argument("--control-url")
    worker.add_argument("--control-api-token")
    worker.add_argument("--env-path", type=Path)
    worker.add_argument("--service-path", type=Path)
    worker.add_argument("--work-dir", type=Path)
    worker.add_argument("--log-dir", type=Path)
    worker.add_argument("--event-spool-dir", type=Path)
    worker.add_argument("--shared-root", action="append", default=[])
    worker.add_argument("--manifest-dir")
    worker.add_argument("--runner", default="tmux")
    worker.add_argument("--skip-systemd", action="store_true")
    worker.add_argument("--no-enable-service", action="store_true")
    worker.add_argument("--no-restart-service", action="store_true")
    return parser


def _load_config_file(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InstallConfigError(f"cannot read config file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InstallConfigError(f"config file {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstallConfigError("config file must contain a JSON object")
    return payload


def _value(args: argparse.Namespace, config: dict[str, Any], name: str, default: Any = None) -> Any:
    raw = getattr(args, name, None)
    if raw is not None and raw != []:
        return raw
    return config.get(name, default)


def _prompt(label: str, default: str | None = None, *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    prompt = f"{label}{suffix}: "
    if secret:
        value = getpass.getpass(prompt)
    else:
        value = input(prompt)
    value = value.strip()
    return value if value else (default or "")


def detect_primary_ip(control_url: str = "") -> str:
    targets: list[tuple[str, int]] = []
    if control_url:
        parsed = urllib.parse.urlparse(control_url if "://" in control_url else f"http://{control_url}")
        if parsed.hostname:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            targets.append((parsed.hostname, port))
    targets.append(("8.8.8.8", 80))

    for host, port in targets:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((host, port))
                ip = sock.getsockname()[0]
        except OSError:
            continue
        if ip and not ip.startswith("127."):
            return ip

    with contextlib.suppress(OSError):
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    return ""


def validate_service_identity(service_user: str, service_group: str) -> list[ValidationError]:
    try:
        user_record = pwd.getpwnam(service_user)
    except KeyError:
        return [
            ValidationError(
                code="service_user_missing",
                message=f"service user {service_user} does not exist",
                fix="Create the user or pass an existing --service-user.",
            )
        ]
    try:
        group_record = grp.getgrnam(service_group)
    except KeyError:
        return [
            ValidationError(
                code="service_group_missing",
                message=f"service group {service_group} does not exist",
                fix="Create the group or pass an existing --service-group.",
            )
        ]
    primary_group = grp.getgrgid(user_record.pw_gid).gr_name
    if primary_group != service_group and service_user not in group_record.gr_mem:
        return [
            ValidationError(
                code="service_user_not_in_group",
                message=f"service user {service_user} is not in group {service_group}",
                fix=f"Run: sudo usermod -aG {service_group} {service_user}",
            )
        ]
    return []


def can_access_path_as_user(service_user: str, path: Path, mode: str) -> bool:
    test = 'test -r "$1"'
    if mode == "rw":
        test = 'test -r "$1" && test -w "$1"'
    result = subprocess.run(
        ["sudo", "-u", service_user, "sh", "-c", test, "sh", str(path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def validate_worker_paths(
    config: WorkerInstallConfig,
    *,
    can_access_as_user=can_access_path_as_user,
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    checks = [
        ("agent_work_dir_not_writable", config.work_dir, "rw"),
        ("agent_log_dir_not_writable", config.log_dir, "rw"),
        ("agent_event_spool_dir_not_writable", config.event_spool_dir, "rw"),
    ]
    checks.extend(("shared_root_not_writable", Path(root), "rw") for root in config.shared_roots)
    if config.manifest_dir:
        checks.append(("manifest_dir_not_writable", Path(config.manifest_dir), "rw"))
    for code, path, mode in checks:
        if not can_access_as_user(config.service_user, path, mode):
            errors.append(
                ValidationError(
                    code=code,
                    message=f"{path} is not readable/writable by {config.service_user}",
                    fix=f"Grant {config.service_user}:{config.service_group} access to {path}.",
                )
            )
    return errors


def check_control_connectivity(
    control_url: str,
    api_token: str | None = None,
) -> ValidationError | None:
    url = control_url.rstrip("/") + "/api/servers"
    request = urllib.request.Request(url)
    if api_token:
        request.add_header("X-OCR-Platform-Token", api_token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status >= 400:
                return ValidationError(
                    code="control_url_unreachable",
                    message=f"control URL returned HTTP {response.status}: {url}",
                    fix="Check --control-url, firewall rules, and API token settings.",
                )
    except (OSError, urllib.error.URLError) as exc:
        return ValidationError(
            code="control_url_unreachable",
            message=f"control URL cannot be reached: {url}: {exc}",
            fix="Check --control-url, firewall rules, and whether the control service is running.",
        )
    return None


def _bool_env(value: bool) -> str:
    return "1" if value else "0"


def redact_secret(value: str | None) -> str:
    if not value:
        return ""
    suffix = value[-4:]
    return "*" * 12 + suffix


def render_control_env(config: ControlInstallConfig) -> str:
    lines = [
        "# Generated by tools/install_production.py.",
        f"OCR_PLATFORM_DATABASE_URL={config.database_url}",
        f"OCR_PLATFORM_REQUIRE_POSTGRES={_bool_env(config.require_postgres)}",
        f"OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS={_bool_env(config.require_current_migrations)}",
        f"OCR_PLATFORM_HOST={config.host}",
        f"OCR_PLATFORM_PORT={config.port}",
        f"OCR_PLATFORM_REQUIRE_API_TOKEN={_bool_env(config.require_api_token)}",
        f"OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS={_bool_env(not config.disable_saved_model_profile_keys)}",
        "OCR_JOB_STALE_AFTER_SECONDS=120",
        "OCR_SERVER_STALE_AFTER_SECONDS=120",
        "OCR_SHARD_LEASE_SECONDS=300",
        "OCR_SCAN_UNIT_CLAIM_BATCH_SIZE=100",
        "OCR_JOB_FILE_DETAIL_LIMIT=10000",
        "OCR_JOB_EVENT_DETAIL_LIMIT=50000",
        "OCR_JOB_LOG_DETAIL_LIMIT=10000",
        "OCR_JOB_FAILED_FILE_SAMPLE_LIMIT=100",
        "OCR_JOB_RECENT_ERROR_SAMPLE_LIMIT=100",
    ]
    if config.require_api_token and config.api_token:
        lines.insert(6, f"OCR_PLATFORM_API_TOKEN={config.api_token}")
    return "\n".join(lines) + "\n"


def render_worker_env(config: WorkerInstallConfig) -> str:
    lines = [
        "# Generated by tools/install_production.py.",
        f"OCR_AGENT_SERVER_ID={config.server_id}",
        f"OCR_CONTROL_URL={config.control_url}",
    ]
    if config.control_api_token:
        lines.append(f"OCR_CONTROL_API_TOKEN={config.control_api_token}")
    lines.extend(
        [
            f"OCR_REPO_DIR={config.repo_dir}",
            f"OCR_AGENT_WORK_DIR={config.work_dir}",
            f"OCR_AGENT_PYTHON={config.venv_dir / 'bin' / 'python'}",
            f"OCR_AGENT_SHARED_ROOTS={':'.join(config.shared_roots)}",
            "OCR_AGENT_POLL_INTERVAL=2",
            "OCR_AGENT_HEARTBEAT_INTERVAL=5",
            "OCR_AGENT_CONTROL_RETRY_INITIAL=1",
            "OCR_AGENT_CONTROL_RETRY_MAX=30",
            f"OCR_AGENT_EVENT_SPOOL_DIR={config.event_spool_dir}",
            "OCR_AGENT_EVENT_SPOOL_MAX_MB=1024",
            "OCR_AGENT_TERMINATION_TIMEOUT=10",
            "OCR_AGENT_STOP_POLL_INTERVAL=1",
            "OCR_MANIFEST_SCAN_PROGRESS_INTERVAL_FILES=10000",
            "OCR_AGENT_RESOURCE_GUARD_MEMORY_PERCENT=90",
            "OCR_AGENT_RESOURCE_GUARD_MIN_AVAILABLE_MEMORY_GB=4",
            "OCR_AGENT_RESOURCE_GUARD_DISK_PERCENT=95",
            "OCR_AGENT_RESOURCE_GUARD_MIN_FREE_DISK_GB=10",
            f"OCR_AGENT_RUNNER={config.runner}",
            f"OCR_AGENT_LOG_DIR={config.log_dir}",
            f"OCR_AGENT_TMUX_SESSION=ocr-agent-{config.server_id}",
        ]
    )
    return "\n".join(lines) + "\n"


def render_control_service(config: ControlInstallConfig) -> str:
    return f"""[Unit]
Description=OCR Platform Control API and UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={config.service_user}
Group={config.service_group}
SupplementaryGroups={config.service_group}
EnvironmentFile={config.env_path}
WorkingDirectory={config.repo_dir}
ExecStart={config.venv_dir / "bin" / "python"} -u -m ocr_platform.control
Restart=always
RestartSec=10
TimeoutStartSec=30
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""


def render_worker_service(config: WorkerInstallConfig) -> str:
    script = config.repo_dir / "scripts" / "ocr_agent_worker.sh"
    return f"""[Unit]
Description=OCR Platform Agent Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={config.service_user}
Group={config.service_group}
SupplementaryGroups={config.service_group}
EnvironmentFile={config.env_path}
WorkingDirectory={config.repo_dir}
ExecStart={script} run {config.env_path}
ExecStop={script} stop {config.env_path}
ExecReload={script} restart {config.env_path}
Restart=always
RestartSec=10
TimeoutStartSec=30
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""


def build_install_plan(
    config: ControlInstallConfig | WorkerInstallConfig,
    *,
    validation_errors: list[ValidationError],
) -> InstallPlan:
    warnings: list[str] = []
    actions = [
        PlanAction(
            "create Python virtual environment",
            str(config.venv_dir),
            command=["python3", "-m", "venv", str(config.venv_dir)],
        )
    ]
    if config.install_dependencies:
        actions.append(
            PlanAction(
                "install Python dependencies",
                str(config.venv_dir),
                command=[
                    str(config.venv_dir / "bin" / "python"),
                    "-m",
                    "pip",
                    "install",
                    "-e",
                    f"{config.repo_dir}[platform]",
                ],
            )
        )
    summary = [
        f"role={config.role}",
        f"service_user={config.service_user}",
        f"service_group={config.service_group}",
        f"repo_dir={config.repo_dir}",
        f"venv_dir={config.venv_dir}",
    ]
    if isinstance(config, ControlInstallConfig):
        summary.extend(
            [
                f"database_url={config.database_url}",
                f"host={config.host}",
                f"port={config.port}",
                f"api_auth={'enabled' if config.require_api_token else 'disabled'}",
            ]
        )
        if config.host == "0.0.0.0" and not config.require_api_token:
            warnings.append(
                "Control API auth is disabled. Make sure this service is protected by firewall, VPN, or a private network."
            )
        actions.append(PlanAction("write control env file", str(config.env_path), render_control_env(config)))
        if config.install_systemd:
            actions.append(
                PlanAction(
                    "write control systemd unit",
                    str(config.service_path),
                    render_control_service(config),
                )
            )
        if config.run_migrations:
            actions.append(
                PlanAction(
                    "apply control database migrations",
                    "database",
                    command=[
                        str(config.venv_dir / "bin" / "python"),
                        "tools/apply_control_migrations.py",
                        "--database-url",
                        config.database_url,
                    ],
                )
            )
    else:
        summary.extend(
            [
                f"server_id={config.server_id}",
                f"control_url={config.control_url}",
                f"shared_roots={':'.join(config.shared_roots)}",
                f"work_dir={config.work_dir}",
                f"log_dir={config.log_dir}",
            ]
        )
        actions.append(PlanAction("write worker env file", str(config.env_path), render_worker_env(config)))
        if config.install_systemd:
            actions.append(
                PlanAction(
                    "write worker systemd unit",
                    str(config.service_path),
                    render_worker_service(config),
                )
            )
    if config.install_systemd:
        actions.append(
            PlanAction(
                "reload systemd daemon",
                "systemd",
                command=["systemctl", "daemon-reload"],
            )
        )
        unit = Path(config.service_path).name
        if config.enable_service:
            actions.append(
                PlanAction("enable systemd service", unit, command=["systemctl", "enable", unit])
            )
        if config.restart_service:
            actions.append(
                PlanAction("restart systemd service", unit, command=["systemctl", "restart", unit])
            )
    return InstallPlan(
        role=config.role,
        summary=summary,
        warnings=warnings,
        validation_errors=validation_errors,
        actions=actions,
    )


def format_plan(plan: InstallPlan) -> str:
    lines = [f"Install plan: {plan.role}", "", "Summary:"]
    lines.extend(f"- {item}" for item in plan.summary)
    if plan.warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in plan.warnings)
    if plan.validation_errors:
        lines.extend(["", "Validation errors:"])
        lines.extend(f"- {error.code}: {error.message} | fix: {error.fix}" for error in plan.validation_errors)
    lines.extend(["", "Actions:"])
    for action in plan.actions:
        if action.command:
            lines.append(f"- {action.description}: {' '.join(action.command)}")
        else:
            lines.append(f"- {action.description}: {action.target}")
            if action.content and (
                "OCR_PLATFORM_API_TOKEN=" in action.content
                or "OCR_CONTROL_API_TOKEN=" in action.content
            ):
                lines.append(_redact_text(action.content))
    return "\n".join(lines) + "\n"


def _redact_text(text: str) -> str:
    redacted = []
    for line in text.splitlines():
        if line.startswith("OCR_PLATFORM_API_TOKEN=") or line.startswith("OCR_CONTROL_API_TOKEN="):
            key, value = line.split("=", 1)
            redacted.append(f"{key}={redact_secret(value)}")
        else:
            redacted.append(line)
    return "\n".join(redacted)


def collect_validation_errors(
    config: ControlInstallConfig | WorkerInstallConfig,
) -> list[ValidationError]:
    errors = validate_service_identity(config.service_user, config.service_group)
    if isinstance(config, ControlInstallConfig):
        try:
            loopback = config.host == "localhost" or ipaddress.ip_address(config.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback and (not config.require_api_token or not config.api_token):
            errors.append(
                ValidationError(
                    "non_loopback_control_requires_token",
                    "A non-loopback Control host requires API token authentication.",
                    "Pass --enable-api-token and provide --api-token, or bind to 127.0.0.1.",
                )
            )
    else:
        errors.extend(validate_worker_paths(config))
        connectivity = check_control_connectivity(config.control_url, config.control_api_token)
        if connectivity is not None:
            errors.append(connectivity)
    return errors


def confirm_plan(plan: InstallPlan, *, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    answer = input("Apply this install plan? Type 'yes' to continue: ").strip().lower()
    return answer == "yes"


def apply_plan(plan: InstallPlan) -> None:
    for action in plan.actions:
        if action.content is not None:
            target = Path(action.target)
            target.parent.mkdir(parents=True, exist_ok=True)
            # target.suffix returns '' for bare dotfiles like .env (no stem),
            # so check both the suffix and the name itself.
            is_secret = target.suffix == ".env" or target.name == ".env"
            if is_secret:
                tmp_name = None
                try:
                    fd, tmp_name = tempfile.mkstemp(
                        prefix=f".{target.name}.",
                        suffix=".tmp",
                        dir=str(target.parent),
                        text=True,
                    )
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(action.content)
                    os.replace(tmp_name, target)
                except Exception:
                    if tmp_name is not None:
                        Path(tmp_name).unlink(missing_ok=True)
                    raise
            else:
                target.write_text(action.content, encoding="utf-8")
        elif action.command is not None:
            subprocess.run(action.command, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = config_from_args(args)
        validation_errors = collect_validation_errors(config)
        plan = build_install_plan(config, validation_errors=validation_errors)
    except InstallConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(format_plan(plan))
    if plan.validation_errors:
        return 1
    if config.dry_run:
        return 0
    if config.non_interactive and not config.yes:
        print("error: --non-interactive requires --yes to apply changes", file=sys.stderr)
        return 2
    if not confirm_plan(plan, assume_yes=config.yes):
        print("Install cancelled.")
        return 1
    apply_plan(plan)
    return 0


def config_from_args(args: argparse.Namespace) -> ControlInstallConfig | WorkerInstallConfig:
    file_config = _load_config_file(args.config)
    service_user = _value(args, file_config, "service_user")
    service_group = _value(args, file_config, "service_group")
    interactive = not args.non_interactive
    if interactive and not service_user:
        service_user = _prompt("Service user")
    if interactive and not service_group:
        service_group = _prompt("Service group")
    if not service_user:
        raise InstallConfigError("--service-user is required")
    if not service_group:
        raise InstallConfigError("--service-group is required")
    repo_dir = Path(_value(args, file_config, "repo_dir", DEFAULT_REPO_DIR))
    venv_dir_raw = _value(args, file_config, "venv_dir")
    venv_dir = Path(venv_dir_raw) if venv_dir_raw else repo_dir / ".venv"
    python_executable_raw = _value(args, file_config, "python_executable")
    python_executable = Path(python_executable_raw) if python_executable_raw else None
    common = {
        "role": args.role,
        "service_user": service_user,
        "service_group": service_group,
        "repo_dir": repo_dir,
        "venv_dir": venv_dir,
        "python_executable": python_executable,
        "dry_run": args.dry_run,
        "non_interactive": args.non_interactive,
        "yes": args.yes,
        "install_dependencies": args.install_deps,
    }
    if args.role == "control":
        database_url = _value(args, file_config, "database_url", "")
        if interactive and not database_url:
            database_url = _prompt("PostgreSQL database URL")
        if not database_url:
            raise InstallConfigError("--database-url is required for control installs")
        return ControlInstallConfig(
            **common,
            database_url=database_url,
            env_path=Path(_value(args, file_config, "env_path", DEFAULT_CONTROL_ENV)),
            service_path=Path(_value(args, file_config, "service_path", DEFAULT_CONTROL_SERVICE)),
            host=_value(args, file_config, "host", "127.0.0.1"),
            port=int(_value(args, file_config, "port", 8080)),
            require_postgres=not args.no_require_postgres,
            require_current_migrations=not args.no_require_current_migrations,
            require_api_token=args.enable_api_token,
            api_token=_value(args, file_config, "api_token"),
            run_migrations=not args.skip_migrations,
            install_systemd=not args.skip_systemd,
            enable_service=not args.no_enable_service,
            restart_service=not args.no_restart_service,
        )
    control_url = _value(args, file_config, "control_url", "")
    server_id = _value(args, file_config, "server_id", "")
    shared_roots = list(_value(args, file_config, "shared_roots", args.shared_root or []))
    if interactive and not control_url:
        control_url = _prompt("Control URL")
    default_server_id = detect_primary_ip(control_url)
    if interactive and not server_id:
        server_id = _prompt("Worker server id", default_server_id)
    if not server_id:
        server_id = default_server_id
    if interactive and not shared_roots:
        raw_roots = _prompt("Shared roots, colon separated")
        shared_roots = [item for item in raw_roots.split(":") if item]
    if args.non_interactive and not shared_roots:
        raise InstallConfigError("--shared-root is required in non-interactive worker installs")
    if not server_id:
        raise InstallConfigError("--server-id is required for worker installs")
    if not control_url:
        raise InstallConfigError("--control-url is required for worker installs")
    return WorkerInstallConfig(
        **common,
        server_id=server_id,
        control_url=control_url,
        control_api_token=_value(args, file_config, "control_api_token"),
        env_path=Path(_value(args, file_config, "env_path", DEFAULT_WORKER_ENV)),
        service_path=Path(_value(args, file_config, "service_path", DEFAULT_WORKER_SERVICE)),
        work_dir=Path(_value(args, file_config, "work_dir", f"/var/lib/ocr-agent/{server_id}")),
        log_dir=Path(_value(args, file_config, "log_dir", f"/var/log/ocr-agent/{server_id}")),
        event_spool_dir=Path(
            _value(args, file_config, "event_spool_dir", f"/var/lib/ocr-agent/{server_id}/event-spool")
        ),
        shared_roots=shared_roots,
        manifest_dir=_value(args, file_config, "manifest_dir"),
        runner=_value(args, file_config, "runner", "tmux"),
        install_systemd=not args.skip_systemd,
        enable_service=not args.no_enable_service,
        restart_service=not args.no_restart_service,
    )


if __name__ == "__main__":
    raise SystemExit(main())
