#!/usr/bin/env python3
"""Run Control -> Agent -> Shard -> Parser -> Artifact against the mock OCR API."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def _wait_for_port(host: str, port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {host}:{port}")


def _wait_for_agent_registration(
    *,
    control_url: str,
    token: str,
    server_id: str,
    timeout: float = 20.0,
) -> None:
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(
        control_url.rstrip("/") + "/api/servers",
        headers={"Authorization": f"Bearer {token}"},
    )
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=1.0) as response:
                servers = json.loads(response.read().decode("utf-8"))
            if any(str(server.get("id")) == server_id for server in servers):
                return
        except (OSError, urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for agent registration: {server_id}")


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _tail(path: Path, lines: int = 80) -> str:
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def _fetch_job_logs(output: str, token: str) -> dict[str, object] | None:
    match = re.search(r"^JOB_ID\s+(\S+)", output, re.MULTILINE)
    if match is None:
        return None
    request = urllib.request.Request(
        f"http://127.0.0.1:38080/api/jobs/{match.group(1)}/logs/page?limit=200",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def run(root: Path, *, parser_python: str) -> int:
    token = "mock-e2e-token"
    processes: list[tuple[str, subprocess.Popen[str], Path]] = []
    with tempfile.TemporaryDirectory(prefix="ocrparser-mock-e2e-") as temp:
        temp_root = Path(temp)
        shared_root = temp_root / "shared"
        env = os.environ.copy()
        env.update(
            {
                "OCR_PLATFORM_HOST": "127.0.0.1",
                "OCR_PLATFORM_PORT": "38080",
                "OCR_PLATFORM_API_TOKEN": token,
                "OCR_PLATFORM_DATABASE_URL": f"sqlite:///{temp_root / 'control.db'}",
            }
        )

        commands = [
            (
                "mock-ocr",
                [sys.executable, str(root / "tools/mock_ocr_service.py"), "--port", "18000", "--quiet"],
            ),
            ("control", [sys.executable, "-m", "ocr_platform.control"]),
        ]
        try:
            for name, command in commands:
                log_path = temp_root / f"{name}.log"
                handle = log_path.open("w", encoding="utf-8")
                process = subprocess.Popen(
                    command,
                    cwd=root,
                    env=env,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                handle.close()
                processes.append((name, process, log_path))

            _wait_for_port("127.0.0.1", 18000)
            _wait_for_port("127.0.0.1", 38080)

            agent_log = temp_root / "agent.log"
            handle = agent_log.open("w", encoding="utf-8")
            agent = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "ocr_platform.agent",
                    "--server_id",
                    "local-worker-01",
                    "--control_url",
                    "http://127.0.0.1:38080",
                    "--control_api_token",
                    token,
                    "--work_dir",
                    str(temp_root / "agent"),
                    "--python_executable",
                    parser_python,
                    "--shared_root",
                    str(shared_root),
                    "--poll_interval_seconds",
                    "0.2",
                    "--heartbeat_interval_seconds",
                    "0.5",
                    "--disable_resource_guard",
                ],
                cwd=root,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            handle.close()
            processes.append(("agent", agent, agent_log))

            _wait_for_agent_registration(
                control_url="http://127.0.0.1:38080",
                token=token,
                server_id="local-worker-01",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "tools/run_distributed_walkthrough.py"),
                    "--shared-root",
                    str(shared_root),
                    "--api-token",
                    token,
                    "--interval",
                    "0.2",
                    "--polls",
                    "150",
                    "--disable-process-pool",
                ],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
            print(result.stdout, end="")
            if result.returncode != 0:
                job_logs = _fetch_job_logs(result.stdout, token)
                if job_logs is not None:
                    print("\n--- job logs ---\n" + json.dumps(job_logs, indent=2, ensure_ascii=False))
                for name, _, log_path in processes:
                    print(f"\n--- {name} log tail ---\n{_tail(log_path)}")
                return result.returncode

            artifacts = [path for path in (shared_root / "output").rglob("*") if path.is_file()]
            if not any(path.suffix == ".md" for path in artifacts):
                print("mock walkthrough produced no Markdown artifact", file=sys.stderr)
                return 1
            print(f"Verified {len(artifacts)} output artifact(s).")
            return 0
        except Exception:
            for name, _, log_path in processes:
                print(f"\n--- {name} log tail ---\n{_tail(log_path)}")
            raise
        finally:
            for _, process, _ in reversed(processes):
                _stop(process)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--parser-python",
        default=sys.executable,
        help="Python executable used by the agent for the parser subprocess.",
    )
    args = parser.parse_args()
    return run(Path(__file__).resolve().parents[1], parser_python=args.parser_python)


if __name__ == "__main__":
    raise SystemExit(main())
