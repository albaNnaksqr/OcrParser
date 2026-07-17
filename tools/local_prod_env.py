from __future__ import annotations

import argparse
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = ROOT / ".local" / "production"


@dataclass(frozen=True)
class LocalProdConfig:
    root: Path = ROOT
    state_dir: Path = DEFAULT_STATE_DIR
    postgres_image: str = "postgres:16-alpine"
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 15432
    postgres_db: str = "ocr_platform"
    postgres_user: str = "ocr_platform"
    postgres_password: str = "ocr_platform_local"
    control_host: str = "127.0.0.1"
    control_port: int = 38080
    api_token: str = "local-dev-token"
    with_worker: bool = False
    with_mock_ocr: bool = False
    mock_ocr_port: int = 18000
    mock_ocr_model: str = "mock-ocr"
    worker_id: str = "local-worker-01"
    worker_count: int = 1
    shared_roots: list[str] = field(default_factory=list)

    @property
    def database_url(self) -> str:
        user = quote(self.postgres_user)
        password = quote(self.postgres_password)
        db = quote(self.postgres_db)
        return (
            f"postgresql+psycopg://{user}:{password}@"
            f"{self.postgres_host}:{self.postgres_port}/{db}"
        )

    @property
    def compose_file(self) -> Path:
        return self.state_dir / "compose.yml"

    @property
    def control_url(self) -> str:
        return f"http://{self.control_host}:{self.control_port}"

    @property
    def control_pid_file(self) -> Path:
        return self.state_dir / "control.pid"

    @property
    def worker_pid_file(self) -> Path:
        return self.worker_pid_file_for(0)

    @property
    def mock_ocr_pid_file(self) -> Path:
        return self.state_dir / "mock-ocr.pid"

    @property
    def worker_work_dir(self) -> Path:
        return self.worker_work_dir_for(0)

    @property
    def worker_spool_dir(self) -> Path:
        return self.worker_spool_dir_for(0)

    @property
    def postgres_data_dir(self) -> Path:
        return self.state_dir / "postgres-data"

    @property
    def control_env_file(self) -> Path:
        return self.state_dir / "control.env"

    @property
    def worker_env_file(self) -> Path:
        return self.worker_env_file_for(0)

    @property
    def control_stdout_log(self) -> Path:
        return self.state_dir / "logs" / "control.out.log"

    @property
    def control_stderr_log(self) -> Path:
        return self.state_dir / "logs" / "control.err.log"

    @property
    def worker_stdout_log(self) -> Path:
        return self.worker_stdout_log_for(0)

    @property
    def worker_stderr_log(self) -> Path:
        return self.worker_stderr_log_for(0)

    def worker_id_for(self, index: int) -> str:
        if not 0 <= index < self.worker_count:
            raise IndexError(f"worker index {index} is outside worker_count={self.worker_count}")
        if index == 0:
            return self.worker_id
        if self.worker_id.endswith("-01"):
            return f"{self.worker_id[:-2]}{index + 1:02d}"
        return f"{self.worker_id}-{index + 1:02d}"

    def worker_pid_file_for(self, index: int) -> Path:
        suffix = "" if index == 0 else f"-{index + 1:02d}"
        return self.state_dir / f"worker{suffix}.pid"

    def worker_work_dir_for(self, index: int) -> Path:
        return self.state_dir / "worker" / self.worker_id_for(index)

    def worker_spool_dir_for(self, index: int) -> Path:
        return self.worker_work_dir_for(index) / "event-spool"

    def worker_env_file_for(self, index: int) -> Path:
        suffix = "" if index == 0 else f"-{index + 1:02d}"
        return self.state_dir / f"worker{suffix}.env"

    def worker_stdout_log_for(self, index: int) -> Path:
        suffix = "" if index == 0 else f"-{index + 1:02d}"
        return self.state_dir / "logs" / f"worker{suffix}.out.log"

    def worker_stderr_log_for(self, index: int) -> Path:
        suffix = "" if index == 0 else f"-{index + 1:02d}"
        return self.state_dir / "logs" / f"worker{suffix}.err.log"

    @property
    def mock_ocr_stdout_log(self) -> Path:
        return self.state_dir / "logs" / "mock-ocr.out.log"

    @property
    def mock_ocr_stderr_log(self) -> Path:
        return self.state_dir / "logs" / "mock-ocr.err.log"


@dataclass(frozen=True)
class PlanStep:
    label: str
    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    message: str = ""

    def render(self) -> str:
        parts = [self.label]
        if self.message:
            parts.append(self.message)
        if self.argv:
            env_prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in self.env.items())
            command = shlex.join(self.argv)
            parts.append(f"{env_prefix} {command}".strip())
        return ": ".join(parts)


def build_control_env(config: LocalProdConfig) -> dict[str, str]:
    return {
        "OCR_PLATFORM_DATABASE_URL": config.database_url,
        "OCR_PLATFORM_REQUIRE_POSTGRES": "1",
        "OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS": "1",
        "OCR_PLATFORM_HOST": config.control_host,
        "OCR_PLATFORM_PORT": str(config.control_port),
        "OCR_PLATFORM_API_TOKEN": config.api_token,
        "OCR_PLATFORM_REQUIRE_API_TOKEN": "1",
        "OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS": "0",
        "OCR_PLATFORM_ENABLE_REMOTE_ADMIN": "0",
    }


def build_worker_env(config: LocalProdConfig, *, worker_index: int = 0) -> dict[str, str]:
    return {
        "OCR_CONTROL_URL": config.control_url,
        "OCR_CONTROL_API_TOKEN": config.api_token,
        "OCR_AGENT_SERVER_ID": config.worker_id_for(worker_index),
        "OCR_AGENT_WORK_DIR": str(config.worker_work_dir_for(worker_index)),
        "OCR_AGENT_EVENT_SPOOL_DIR": str(config.worker_spool_dir_for(worker_index)),
        "OCR_AGENT_SHARED_ROOTS": os.pathsep.join(config.shared_roots),
        "OCR_REPO_DIR": str(config.root),
    }


def repo_python_env(config: LocalProdConfig) -> dict[str, str]:
    return {"PYTHONPATH": str(config.root)}


def merge_env(*envs: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for env in envs:
        merged.update(env)
    return merged


def render_env_file(env: dict[str, str]) -> str:
    return "".join(f"{key}={shlex.quote(str(value))}\n" for key, value in env.items())


def write_env_file(path: Path, env: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_env_file(env), encoding="utf-8")
    path.chmod(0o600)


def mask_database_url(database_url: str) -> str:
    marker = "://"
    if marker not in database_url or "@" not in database_url:
        return database_url
    scheme, rest = database_url.split(marker, 1)
    credentials, host = rest.split("@", 1)
    if ":" not in credentials:
        return database_url
    user = credentials.split(":", 1)[0]
    return f"{scheme}{marker}{user}:***@{host}"


def build_runtime_summary(config: LocalProdConfig) -> list[str]:
    lines = [
        f"Database URL: {mask_database_url(config.database_url)}",
        f"PostgreSQL data: {config.postgres_data_dir}",
        f"Control URL: {config.control_url}/ui/",
        f"Control env: {config.control_env_file}",
        f"Control logs: {config.control_stdout_log}, {config.control_stderr_log}",
    ]
    if config.with_worker:
        for index in range(config.worker_count):
            label = "Worker" if index == 0 else f"Worker {config.worker_id_for(index)}"
            lines.extend(
                [
                    f"{label} env: {config.worker_env_file_for(index)}",
                    f"{label} logs: "
                    f"{config.worker_stdout_log_for(index)}, {config.worker_stderr_log_for(index)}",
                ]
            )
    if config.with_mock_ocr:
        lines.extend(
            [
                f"Mock OCR API: http://127.0.0.1:{config.mock_ocr_port}/v1 "
                f"(model={config.mock_ocr_model})",
                f"Mock OCR logs: {config.mock_ocr_stdout_log}, {config.mock_ocr_stderr_log}",
            ]
        )
    lines.append("Stop: python3 tools/local_prod_env.py down")
    return lines


def build_compose_yaml(config: LocalProdConfig) -> str:
    return f"""services:
  postgres:
    image: {config.postgres_image}
    container_name: ocr-platform-local-postgres
    environment:
      POSTGRES_DB: {config.postgres_db}
      POSTGRES_USER: {config.postgres_user}
      POSTGRES_PASSWORD: {config.postgres_password}
    ports:
      - "{config.postgres_host}:{config.postgres_port}:5432"
    volumes:
      - ./postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U {config.postgres_user} -d {config.postgres_db}"]
      interval: 2s
      timeout: 5s
      retries: 30
"""


def build_up_plan(
    config: LocalProdConfig,
    *,
    python_executable: str = sys.executable,
    compose_command: Sequence[str] = ("docker", "compose"),
) -> list[PlanStep]:
    compose = [*compose_command, "-f", str(config.compose_file)]
    steps = [
        PlanStep("write postgres compose", message=str(config.compose_file)),
        PlanStep("start postgres", [*compose, "up", "-d", "postgres"]),
        PlanStep(
            "apply control migrations",
            [
                python_executable,
                "-m",
                "ocr_platform.control.migrate_cli",
                "apply",
                "--database-url",
                config.database_url,
            ],
            env=repo_python_env(config),
        ),
        PlanStep(
            "start local control",
            [python_executable, "-u", "-m", "ocr_platform.control"],
            env=merge_env(repo_python_env(config), build_control_env(config)),
        ),
    ]
    if config.with_mock_ocr:
        steps.append(
            PlanStep(
                "start mock OCR service",
                [
                    python_executable,
                    "-u",
                    str(config.root / "tools" / "mock_ocr_service.py"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(config.mock_ocr_port),
                    "--model-name",
                    config.mock_ocr_model,
                    "--quiet",
                ],
                env=repo_python_env(config),
            )
        )
    if config.with_worker:
        for index in range(config.worker_count):
            worker_argv = [
                python_executable,
                "-u",
                "-m",
                "ocr_platform.agent",
                "--server_id",
                config.worker_id_for(index),
                "--control_url",
                config.control_url,
                "--control_api_token",
                config.api_token,
                "--work_dir",
                str(config.worker_work_dir_for(index)),
                "--event_spool_dir",
                str(config.worker_spool_dir_for(index)),
                "--repo_dir",
                str(config.root),
            ]
            for shared_root in config.shared_roots:
                worker_argv.extend(["--shared_root", shared_root])
            steps.append(
                PlanStep(
                    f"start local worker {config.worker_id_for(index)}",
                    worker_argv,
                    env=merge_env(
                        repo_python_env(config),
                        build_worker_env(config, worker_index=index),
                    ),
                )
            )
    return steps


def build_down_plan(
    config: LocalProdConfig,
    *,
    volumes: bool = False,
    compose_command: Sequence[str] = ("docker", "compose"),
) -> list[PlanStep]:
    compose = [*compose_command, "-f", str(config.compose_file)]
    down_command = [*compose, "down"]
    if volumes:
        down_command.append("-v")
    return [
        PlanStep("stop local worker", message=str(config.worker_pid_file)),
        PlanStep("stop mock OCR service", message=str(config.mock_ocr_pid_file)),
        PlanStep("stop local control", message=str(config.control_pid_file)),
        PlanStep("stop postgres", down_command),
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a local production-like OCR Platform environment."
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help="Directory for compose file, logs, pid files, and local Postgres data.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    up = subparsers.add_parser("up", help="Start PostgreSQL, apply migrations, and start control.")
    _add_runtime_args(up)
    up.add_argument("--with-worker", action="store_true", help="Also start local agent workers.")
    up.add_argument(
        "--worker-count",
        type=int,
        default=1,
        help="Number of isolated agents to start when --with-worker is set (default: 1).",
    )
    up.add_argument(
        "--with-mock-ocr",
        action="store_true",
        help="Also start an OpenAI-compatible mock OCR endpoint for UI walkthroughs.",
    )
    up.add_argument("--mock-ocr-port", type=int, default=18000)
    up.add_argument("--mock-ocr-model", default="mock-ocr")
    up.add_argument("--shared-root", action="append", default=[], help="Shared root reported by worker.")
    up.add_argument("--dry-run", action="store_true", help="Print the plan without starting services.")

    down = subparsers.add_parser("down", help="Stop local services and Postgres.")
    down.add_argument("--volumes", action="store_true", help="Also delete the local Postgres volume.")
    down.add_argument("--dry-run", action="store_true", help="Print the stop plan without executing it.")

    status = subparsers.add_parser("status", help="Print local service status.")
    _add_runtime_args(status)
    status.add_argument("--json", action="store_true", help="Reserved for future structured status output.")
    return parser


def _add_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--postgres-port", type=int, default=15432)
    parser.add_argument("--control-port", type=int, default=38080)
    parser.add_argument("--api-token", default="local-dev-token")
    parser.add_argument("--postgres-image", default="postgres:16-alpine")
    parser.add_argument("--postgres-password", default="ocr_platform_local")


def config_from_args(args: argparse.Namespace, *, root: Path = ROOT) -> LocalProdConfig:
    return LocalProdConfig(
        root=root,
        state_dir=args.state_dir,
        postgres_port=getattr(args, "postgres_port", 15432),
        postgres_image=getattr(args, "postgres_image", "postgres:16-alpine"),
        postgres_password=getattr(args, "postgres_password", "ocr_platform_local"),
        control_port=getattr(args, "control_port", 38080),
        api_token=getattr(args, "api_token", "local-dev-token"),
        with_worker=getattr(args, "with_worker", False),
        with_mock_ocr=getattr(args, "with_mock_ocr", False),
        mock_ocr_port=getattr(args, "mock_ocr_port", 18000),
        mock_ocr_model=getattr(args, "mock_ocr_model", "mock-ocr"),
        worker_count=getattr(args, "worker_count", 1),
        shared_roots=list(getattr(args, "shared_root", []) or []),
    )


def write_compose_file(config: LocalProdConfig) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.compose_file.write_text(build_compose_yaml(config), encoding="utf-8")


def resolve_compose_command() -> list[str]:
    docker = shutil.which("docker")
    if docker:
        return [docker, "compose"]
    docker_compose = shutil.which("docker-compose")
    if docker_compose:
        return [docker_compose]
    raise RuntimeError("docker compose was not found. Install Docker Desktop or Colima first.")


def run_step(step: PlanStep, *, cwd: Path) -> None:
    if not step.argv:
        return
    env = os.environ.copy()
    env.update(step.env)
    subprocess.run(step.argv, cwd=cwd, env=env, check=True)


def wait_for_postgres(
    config: LocalProdConfig,
    compose_command: Sequence[str],
    *,
    timeout_seconds: float = 60.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    command = [
        *compose_command,
        "-f",
        str(config.compose_file),
        "exec",
        "-T",
        "postgres",
        "pg_isready",
        "-U",
        config.postgres_user,
        "-d",
        config.postgres_db,
    ]
    while time.monotonic() < deadline:
        result = subprocess.run(command, cwd=config.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("PostgreSQL did not become ready before timeout.")


def start_background_process(
    label: str,
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    pid_file: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> int:
    if pid_file.exists() and _pid_is_running(int(pid_file.read_text(encoding="utf-8").strip() or "0")):
        raise RuntimeError(f"{label} already appears to be running with pid {pid_file.read_text().strip()}")
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    merged_env = os.environ.copy()
    merged_env.update(env)
    with stdout_path.open("ab") as stdout_file, stderr_path.open("ab") as stderr_file:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=merged_env,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
    pid_file.write_text(str(process.pid), encoding="utf-8")
    return process.pid


def stop_pid_file(pid_file: Path, *, timeout_seconds: float = 10.0) -> None:
    if not pid_file.exists():
        return
    text = pid_file.read_text(encoding="utf-8").strip()
    if not text:
        pid_file.unlink(missing_ok=True)
        return
    pid = int(text)
    if not _pid_is_running(pid):
        pid_file.unlink(missing_ok=True)
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            pid_file.unlink(missing_ok=True)
            return
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)
    pid_file.unlink(missing_ok=True)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _http_get(url: str, *, token: str | None = None, timeout: float = 2.0) -> tuple[int | None, str]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(500).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(500).decode("utf-8", errors="replace")
    except OSError as exc:
        return None, str(exc)


def _port_open(host: str, port: int, *, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def print_plan(plan: Sequence[PlanStep]) -> None:
    for step in plan:
        print(step.render())


def command_up(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    if config.worker_count < 1:
        raise RuntimeError("--worker-count must be at least 1")
    plan = build_up_plan(config, compose_command=("docker", "compose"))
    if args.dry_run:
        print_plan(plan)
        return 0

    write_compose_file(config)
    write_env_file(config.control_env_file, build_control_env(config))
    compose_command = resolve_compose_command()
    run_step(PlanStep("start postgres", [*compose_command, "-f", str(config.compose_file), "up", "-d", "postgres"]), cwd=config.root)
    wait_for_postgres(config, compose_command)
    run_step(
        PlanStep(
            "apply control migrations",
            [
                sys.executable,
                "-m",
                "ocr_platform.control.migrate_cli",
                "apply",
                "--database-url",
                config.database_url,
            ],
            env=repo_python_env(config),
        ),
        cwd=config.root,
    )
    if _port_open(config.control_host, config.control_port):
        raise RuntimeError(f"control port {config.control_host}:{config.control_port} is already in use.")
    if config.with_mock_ocr and _port_open("127.0.0.1", config.mock_ocr_port):
        raise RuntimeError(f"mock OCR port 127.0.0.1:{config.mock_ocr_port} is already in use.")
    start_background_process(
        "control",
        [sys.executable, "-u", "-m", "ocr_platform.control"],
        cwd=config.root,
        env=merge_env(repo_python_env(config), build_control_env(config)),
        pid_file=config.control_pid_file,
        stdout_path=config.control_stdout_log,
        stderr_path=config.control_stderr_log,
    )
    if config.with_mock_ocr:
        start_background_process(
            "mock OCR",
            [
                sys.executable,
                "-u",
                str(config.root / "tools" / "mock_ocr_service.py"),
                "--host",
                "127.0.0.1",
                "--port",
                str(config.mock_ocr_port),
                "--model-name",
                config.mock_ocr_model,
                "--quiet",
            ],
            cwd=config.root,
            env=repo_python_env(config),
            pid_file=config.mock_ocr_pid_file,
            stdout_path=config.mock_ocr_stdout_log,
            stderr_path=config.mock_ocr_stderr_log,
        )
    if config.with_worker:
        worker_steps = [step for step in build_up_plan(config) if step.label.startswith("start local worker")]
        for index, worker_step in enumerate(worker_steps):
            config.worker_work_dir_for(index).mkdir(parents=True, exist_ok=True)
            config.worker_spool_dir_for(index).mkdir(parents=True, exist_ok=True)
            write_env_file(
                config.worker_env_file_for(index),
                build_worker_env(config, worker_index=index),
            )
            start_background_process(
                config.worker_id_for(index),
                worker_step.argv,
                cwd=config.root,
                env=merge_env(
                    repo_python_env(config),
                    build_worker_env(config, worker_index=index),
                ),
                pid_file=config.worker_pid_file_for(index),
                stdout_path=config.worker_stdout_log_for(index),
                stderr_path=config.worker_stderr_log_for(index),
            )
    print(f"Control UI: {config.control_url}/ui/")
    print(f"API token: {config.api_token}")
    for line in build_runtime_summary(config):
        print(line)
    return 0


def command_down(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    plan = build_down_plan(config, volumes=args.volumes)
    if args.dry_run:
        print_plan(plan)
        return 0
    for pid_file in sorted(config.state_dir.glob("worker*.pid")):
        stop_pid_file(pid_file)
    stop_pid_file(config.mock_ocr_pid_file)
    stop_pid_file(config.control_pid_file)
    if config.compose_file.exists():
        compose_command = resolve_compose_command()
        command = [*compose_command, "-f", str(config.compose_file), "down"]
        if args.volumes:
            command.append("-v")
        subprocess.run(command, cwd=config.root, check=True)
    return 0


def command_status(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    for line in build_runtime_summary(config):
        print(line)
    print(f"PostgreSQL port: {'open' if _port_open(config.postgres_host, config.postgres_port) else 'closed'}")
    process_paths = [
        (config.control_pid_file, "control"),
        (config.mock_ocr_pid_file, "mock-ocr"),
    ]
    worker_pid_files = sorted(config.state_dir.glob("worker*.pid"))
    process_paths.extend((path, path.stem) for path in worker_pid_files)
    if not worker_pid_files:
        process_paths.append((config.worker_pid_file, "worker"))
    for path, label in process_paths:
        pid = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        running = _pid_is_running(int(pid)) if pid.isdigit() else False
        print(f"{label}: {'running' if running else 'stopped'}{f' pid={pid}' if pid else ''}")
    health_status, health_body = _http_get(f"{config.control_url}/healthz")
    ready_status, ready_body = _http_get(f"{config.control_url}/readyz")
    diag_status, diag_body = _http_get(f"{config.control_url}/api/system/diagnostics", token=config.api_token)
    print(f"/healthz: {health_status} {health_body[:160]}")
    print(f"/readyz: {ready_status} {ready_body[:160]}")
    print(f"/api/system/diagnostics: {diag_status} {diag_body[:160]}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "up":
            return command_up(args)
        if args.command == "down":
            return command_down(args)
        if args.command == "status":
            return command_status(args)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
