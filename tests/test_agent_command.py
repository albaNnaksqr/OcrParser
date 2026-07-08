import asyncio
import json
import sys

import httpx

from ocr_platform.agent.client import ControlClient
from ocr_platform.agent import __main__ as agent_main
from ocr_platform.agent.config import AgentConfig, parse_args
from ocr_platform.agent.paths import check_shared_paths
from ocr_platform.agent import runner
from ocr_platform.agent.runner import build_ocr_command


def test_control_client_sends_api_token_header():
    client = ControlClient(
        "http://control:8080",
        "server-a",
        api_token="control-secret",
    )

    try:
        assert client._client.headers["X-OCR-Platform-Token"] == "control-secret"
    finally:
        asyncio.run(client.close())


def test_agent_main_job_exception_payload_includes_failure_category():
    payload = agent_main._job_exception_failure_payload(TimeoutError("model request timed out"))

    assert payload == {
        "error": "model request timed out",
        "error_message": "model request timed out",
        "failure_category": "api_timeout",
    }


def test_build_ocr_command_includes_platform_fields(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        poll_interval_seconds=1.0,
        python_executable=sys.executable,
    )
    job = {
        "id": "job-1",
        "input_dir": "/shared/in",
        "output_dir": "/shared/out",
        "engine": "dotsocr",
        "engine_config": "/shared/engines.yaml",
        "ip": "127.0.0.1",
        "port": 8000,
        "model_name": "model",
        "page_concurrency": 4,
        "force_reprocess": True,
        "extra_args": {
            "save_page_json": True,
            "skip_blank_pages": True,
            "write_debug": False,
        },
    }

    command, event_file = build_ocr_command(job, config)

    assert command[:3] == [sys.executable, "-m", "ocr_parser"]
    assert "--input_dir" in command
    assert "/shared/in" in command
    assert "--output_dir" in command
    assert "/shared/out" in command
    assert "--job_id" in command
    assert "job-1" in command
    assert "--job_event_file" in command
    assert str(event_file).endswith("job-1/events.jsonl")
    assert "--force_reprocess" in command
    assert "--save_page_json" in command
    assert "--skip_blank_pages" in command
    assert "--write_debug" not in command
    assert json.loads((tmp_path / "jobs" / "job-1" / "command.json").read_text()) == command


def test_build_ocr_command_wires_resource_execution_control_file(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        poll_interval_seconds=1.0,
        python_executable=sys.executable,
    )
    job = {
        "id": "job-1",
        "input_dir": "/shared/in",
        "output_dir": "/shared/out",
        "engine": "dotsocr",
        "extra_args": {"api_concurrency_start": 80, "api_concurrency_max": 80},
    }

    command, _ = build_ocr_command(job, config)

    control_path = tmp_path / "jobs" / "job-1" / "execution-control.json"
    assert "--execution_control_file" in command
    assert str(control_path) in command
    assert json.loads(control_path.read_text(encoding="utf-8")) == {
        "paused": False,
        "api_concurrency_limit": 80,
        "reason": "initial",
    }


def test_build_ocr_command_keeps_api_key_out_of_argv_and_command_file(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    job = {
        "id": "job-1",
        "input_dir": "/shared/in",
        "output_dir": "/shared/out",
        "engine": "dotsocr",
        "extra_args": {"api_key": "job-secret", "file_concurrency": 2},
    }

    command, _ = build_ocr_command(job, config)

    assert "--api_key" not in command
    assert "job-secret" not in command
    assert "--file_concurrency" in command
    command_file = tmp_path / "jobs" / "job-1" / "command.json"
    assert "job-secret" not in command_file.read_text(encoding="utf-8")


def test_build_ocr_command_keeps_secret_like_extra_args_out_of_argv_and_command_file(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    job = {
        "id": "job-1",
        "input_dir": "/shared/in",
        "output_dir": "/shared/out",
        "engine": "dotsocr",
        "extra_args": {
            "access_token": "token-secret",
            "authorization": "Bearer auth-secret",
            "client_secret": "client-secret",
            "password": "password-secret",
            "file_concurrency": 2,
        },
    }

    command, _ = build_ocr_command(job, config)
    command_file = tmp_path / "jobs" / "job-1" / "command.json"
    command_json = command_file.read_text(encoding="utf-8")

    assert "--file_concurrency" in command
    for leaked in [
        "token-secret",
        "auth-secret",
        "client-secret",
        "password-secret",
        "--access_token",
        "--authorization",
        "--client_secret",
        "--password",
    ]:
        assert leaked not in command
        assert leaked not in command_json


def test_ocr_subprocess_env_injects_api_key_only_when_present(monkeypatch):
    monkeypatch.setenv("API_KEY", "ambient-secret")

    env = runner._ocr_subprocess_env({"extra_args": {"api_key": "job-secret"}})

    assert env is not None
    assert env["API_KEY"] == "job-secret"
    no_key_env = runner._ocr_subprocess_env({"extra_args": {"file_concurrency": 2}})
    assert no_key_env is not None
    assert "API_KEY" not in no_key_env


def test_resource_execution_control_payload_pauses_and_restores_limit():
    job = {
        "page_concurrency": 24,
        "extra_args": {"api_concurrency_start": 80, "api_concurrency_max": 80},
    }

    blocked = runner.resource_execution_control_payload(
        job,
        {
            "constrained": True,
            "level": "blocked",
            "reasons": ["memory percent 95.0% >= 90.0%"],
        },
    )
    ready = runner.resource_execution_control_payload(
        job,
        {"constrained": False, "level": "ready", "reasons": []},
    )

    assert blocked == {
        "paused": True,
        "api_concurrency_limit": 1,
        "reason": "memory percent 95.0% >= 90.0%",
    }
    assert ready == {
        "paused": False,
        "api_concurrency_limit": 80,
        "reason": "ready",
    }


def test_resource_execution_control_watcher_updates_control_file(tmp_path, monkeypatch):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        poll_interval_seconds=0.5,
    )
    job = {
        "id": "job-1",
        "page_concurrency": 24,
        "extra_args": {"api_concurrency_start": 80},
    }
    control_path = tmp_path / "jobs" / "job-1" / "execution-control.json"
    process = type("Process", (), {"returncode": None})()
    pressure_states = [
        {"constrained": True, "level": "blocked", "reasons": ["memory pressure"]},
        {"constrained": False, "level": "ready", "reasons": []},
    ]
    sleeps = []

    def fake_resource_pressure(config):
        return pressure_states.pop(0)

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            process.returncode = 0

    monkeypatch.setattr(runner, "resource_pressure", fake_resource_pressure)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)

    asyncio.run(runner.resource_execution_control_watcher(job, config, control_path, process))

    assert sleeps == [0.5, 0.5]
    assert json.loads(control_path.read_text(encoding="utf-8")) == {
        "paused": False,
        "api_concurrency_limit": 80,
        "reason": "ready",
    }


def test_resource_execution_control_watcher_updates_current_shard_state(tmp_path, monkeypatch):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        poll_interval_seconds=0.5,
    )
    job = {
        "id": "job-1",
        "page_concurrency": 24,
        "extra_args": {"api_concurrency_start": 80},
        "shard": {
            "id": 7,
            "assigned_server_id": "server-a",
            "attempt_count": 2,
        },
    }
    control_path = tmp_path / "jobs" / "job-1" / "execution-control.json"
    process = type("Process", (), {"returncode": None})()
    pressure_states = [
        {"constrained": True, "level": "blocked", "reasons": ["memory pressure"]},
        {"constrained": False, "level": "ready", "reasons": []},
    ]
    sleeps = []

    class Client:
        def __init__(self):
            self.updates = []

        async def update_shard(self, shard_id, payload):
            self.updates.append((shard_id, payload))
            return {"id": shard_id, **payload}

    def fake_resource_pressure(config):
        return pressure_states.pop(0)

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            process.returncode = 0

    monkeypatch.setattr(runner, "resource_pressure", fake_resource_pressure)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)
    client = Client()

    asyncio.run(
        runner.resource_execution_control_watcher(
            job,
            config,
            control_path,
            process,
            client=client,
        )
    )

    assert client.updates == [
        (
            7,
            {
                "status": "running",
                "assigned_server_id": "server-a",
                "attempt_count": 2,
                "execution_paused": True,
                "api_concurrency_limit": 1,
                "execution_control_reason": "memory pressure",
            },
        ),
        (
            7,
            {
                "status": "running",
                "assigned_server_id": "server-a",
                "attempt_count": 2,
                "execution_paused": False,
                "api_concurrency_limit": 80,
                "execution_control_reason": "ready",
            },
        ),
    ]


def test_parse_args_allows_python_executable_override():
    config = parse_args(
        [
            "--server_id",
            "server-a",
            "--control_url",
            "http://control:8080/",
            "--python_executable",
            "/opt/venv/bin/python",
            "--heartbeat_interval_seconds",
            "7",
            "--control_retry_initial_seconds",
            "2",
            "--control_retry_max_seconds",
            "9",
            "--process_termination_timeout_seconds",
            "4",
            "--stop_poll_interval_seconds",
            "0.5",
            "--shared_root",
            "/shared",
            "--shared_root",
            "/mnt/pdf",
        ]
    )

    assert config.control_url == "http://control:8080"
    assert config.python_executable == "/opt/venv/bin/python"
    assert config.heartbeat_interval_seconds == 7
    assert config.control_retry_initial_seconds == 2
    assert config.control_retry_max_seconds == 9
    assert config.process_termination_timeout_seconds == 4
    assert config.stop_poll_interval_seconds == 0.5
    assert config.shared_roots == ["/shared", "/mnt/pdf"]


def test_parse_args_reads_shared_roots_from_env(monkeypatch):
    monkeypatch.setenv("OCR_AGENT_SHARED_ROOTS", "/shared:/mnt/pdf")

    config = parse_args([])

    assert config.shared_roots == ["/shared", "/mnt/pdf"]


def test_parse_args_reads_control_api_token_from_env(monkeypatch):
    monkeypatch.setenv("OCR_CONTROL_API_TOKEN", "control-secret")

    config = parse_args([])

    assert config.control_api_token == "control-secret"


def test_parse_args_allows_resource_guard_overrides():
    config = parse_args(
        [
            "--disable_resource_guard",
            "--resource_guard_memory_percent",
            "88",
            "--resource_guard_min_available_memory_gb",
            "6",
            "--resource_guard_disk_percent",
            "92",
            "--resource_guard_min_free_disk_gb",
            "20",
        ]
    )

    assert config.resource_guard_enabled is False
    assert config.resource_guard_memory_percent == 88
    assert config.resource_guard_min_available_memory_bytes == 6 * 1024**3
    assert config.resource_guard_disk_percent == 92
    assert config.resource_guard_min_free_disk_bytes == 20 * 1024**3


def test_parse_args_defaults_event_spool_under_work_dir(tmp_path):
    work_dir = tmp_path / "work"

    config = parse_args(["--work_dir", str(work_dir)])

    assert config.event_spool_dir == str(work_dir / "event-spool")


def test_parse_args_can_disable_event_spool(tmp_path):
    config = parse_args(["--work_dir", str(tmp_path / "work"), "--disable_event_spool"])

    assert config.event_spool_dir is None


def test_parse_args_configures_event_spool_max_bytes(tmp_path):
    config = parse_args(
        [
            "--work_dir",
            str(tmp_path / "work"),
            "--event_spool_max_mb",
            "64",
        ]
    )

    assert config.event_spool_max_bytes == 64 * 1024**2


def test_heartbeat_capabilities_reports_event_spool_backlog(tmp_path):
    spool_dir = tmp_path / "event-spool"
    spool_dir.mkdir()
    (spool_dir / "events.jsonl").write_text(
        json.dumps({"id": "event-1"}) + "\n\n" + json.dumps({"id": "event-2"}) + "\n",
        encoding="utf-8",
    )
    (spool_dir / "logs.jsonl").write_text(
        json.dumps({"id": "log-1"}) + "\n",
        encoding="utf-8",
    )
    (spool_dir / "events.failed.jsonl").write_text(
        json.dumps({"id": "failed-event-1"}) + "\n",
        encoding="utf-8",
    )
    (spool_dir / "logs.failed.jsonl").write_text(
        json.dumps({"id": "failed-log-1"}) + "\n",
        encoding="utf-8",
    )
    (spool_dir / "events.dropped.json").write_text(
        json.dumps({"dropped": 3}),
        encoding="utf-8",
    )
    (spool_dir / "logs.dropped.json").write_text(
        json.dumps({"dropped": 4}),
        encoding="utf-8",
    )
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        event_spool_dir=str(spool_dir),
        event_spool_max_bytes=64 * 1024**2,
        resource_guard_enabled=False,
    )

    capabilities = agent_main._heartbeat_capabilities(config)

    assert capabilities["event_spool"] == {
        "dir": str(spool_dir),
        "pending_events": 2,
        "pending_logs": 1,
        "failed_events": 1,
        "failed_logs": 1,
        "pending_event_bytes": (spool_dir / "events.jsonl").stat().st_size,
        "pending_log_bytes": (spool_dir / "logs.jsonl").stat().st_size,
        "failed_event_bytes": (spool_dir / "events.failed.jsonl").stat().st_size,
        "failed_log_bytes": (spool_dir / "logs.failed.jsonl").stat().st_size,
        "dropped_events": 3,
        "dropped_logs": 4,
        "max_pending_bytes": 64 * 1024**2,
    }


def test_heartbeat_capabilities_reports_pending_shard_update_backlog(tmp_path):
    work_dir = tmp_path / "work"
    first_dir = work_dir / "jobs" / "job-1" / "pending-shard-updates"
    second_dir = work_dir / "jobs" / "job-2" / "pending-shard-updates"
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    (first_dir / "shard-11.json").write_text(
        '{"job_id":"job-1","shard_id":11,"server_id":"server-a","payload":{"status":"running"}}',
        encoding="utf-8",
    )
    (second_dir / "shard-12.json").write_text(
        '{"job_id":"job-2","shard_id":12,"server_id":"server-a","payload":{"status":"succeeded"}}',
        encoding="utf-8",
    )
    (second_dir / "shard-13.json.failed").write_text(
        '{"job_id":"job-2","shard_id":13,"server_id":"server-a","replay_error":{"message":"bad"}}',
        encoding="utf-8",
    )
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(work_dir),
        resource_guard_enabled=False,
    )

    capabilities = agent_main._heartbeat_capabilities(config)

    assert capabilities["pending_shard_updates"] == {
        "pending": 2,
        "failed": 1,
    }


def test_heartbeat_once_replays_spooled_events_after_successful_heartbeat(tmp_path):
    class ReplayClient:
        def __init__(self):
            self.heartbeats = 0
            self.heartbeat_payloads = []
            self.replays = 0
            self.log_replays = 0

        async def heartbeat(self, **payload):
            self.heartbeats += 1
            self.heartbeat_payloads.append(payload)
            return {"ok": True}

        async def replay_spooled_events(self, limit=None):
            self.replays += 1
            return 2

        async def replay_spooled_logs(self, limit=None):
            self.log_replays += 1
            return 1

    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
    )
    client = ReplayClient()

    asyncio.run(agent_main._heartbeat_once(client, config, "idle"))

    assert client.heartbeats == 2
    assert [payload["status"] for payload in client.heartbeat_payloads] == ["idle", "idle"]
    assert client.replays == 1
    assert client.log_replays == 1


def test_heartbeat_once_replays_pending_shard_updates_after_successful_heartbeat(tmp_path, monkeypatch):
    class ReplayClient:
        def __init__(self):
            self.heartbeats = 0
            self.heartbeat_payloads = []
            self.event_replays = 0

        async def heartbeat(self, **payload):
            self.heartbeats += 1
            self.heartbeat_payloads.append(payload)
            return {"ok": True}

        async def replay_spooled_events(self, limit=None):
            self.event_replays += 1
            return 0

    replayed = []

    async def fake_replay_pending_shard_updates(config, client):
        replayed.append((config.server_id, client))
        return 1

    monkeypatch.setattr(
        agent_main,
        "replay_pending_shard_updates",
        fake_replay_pending_shard_updates,
    )
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
    )
    client = ReplayClient()

    asyncio.run(agent_main._heartbeat_once(client, config, "idle"))

    assert client.heartbeats == 2
    assert [payload["status"] for payload in client.heartbeat_payloads] == ["idle", "idle"]
    assert client.event_replays == 1
    assert replayed == [("server-a", client)]


def test_check_shared_paths_reports_accessibility(tmp_path):
    existing = tmp_path / "shared"
    existing.mkdir()
    missing = tmp_path / "missing"

    results = check_shared_paths([str(existing), str(missing)])

    by_path = {item["path"]: item for item in results}
    assert by_path[str(existing)]["exists"] is True
    assert by_path[str(existing)]["is_dir"] is True
    assert by_path[str(existing)]["readable"] is True
    assert by_path[str(existing)]["checked_at"]
    assert by_path[str(missing)]["exists"] is False
    assert by_path[str(missing)]["is_dir"] is False
    assert by_path[str(missing)]["readable"] is False


def test_agent_heartbeat_capabilities_include_shared_path_checks(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        shared_roots=[str(shared)],
    )

    payload = agent_main._heartbeat_capabilities(config)

    assert payload["shared_paths"][0]["path"] == str(shared)
    assert payload["shared_paths"][0]["exists"] is True
    assert payload["shared_paths"][0]["readable"] is True


def test_agent_heartbeat_capabilities_include_system_resources(tmp_path):
    shared = tmp_path / "shared"
    work = tmp_path / "work"
    shared.mkdir()
    work.mkdir()
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(work),
        shared_roots=[str(shared)],
    )

    payload = agent_main._heartbeat_capabilities(config)

    resources = payload["system_resources"]
    assert resources["cpu"]["logical_count"] >= 1
    assert resources["memory"]["percent"] >= 0
    assert {item["path"] for item in resources["disks"]} == {str(work), str(shared)}
    assert payload["resource_pressure"]["constrained"] is False


def test_next_job_skips_claim_when_resource_constrained(tmp_path, monkeypatch):
    class GuardedJobClient:
        def __init__(self):
            self.claim_count = 0

        async def next_job(self):
            self.claim_count += 1
            return {"id": "job-1"}

    monkeypatch.setattr(
        agent_main,
        "_resource_pressure",
        lambda config: {"constrained": True, "reasons": ["disk free below threshold"]},
    )
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
    )
    client = GuardedJobClient()

    job = asyncio.run(agent_main._next_job_if_resources_allow(client, config))

    assert job is None
    assert client.claim_count == 0


def test_register_defaults_name_and_host_to_server_id(monkeypatch):
    captured = {}

    def handler(request):
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"id": captured["payload"]["id"]})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport)

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    client = ControlClient("http://control:8080/", "server-a")

    async def exercise():
        try:
            return await client.register()
        finally:
            await client.close()

    result = asyncio.run(exercise())

    assert result == {"id": "server-a"}
    assert captured["payload"]["name"] == "server-a"
    assert captured["payload"]["host"] == "server-a"


def test_heartbeat_posts_worker_runtime_status(monkeypatch):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "server-a", "status": "busy"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport)

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    client = ControlClient("http://control:8080", "server-a")

    async def exercise():
        try:
            return await client.heartbeat(
                status="busy",
                current_job_id="job-1",
                capabilities={"shared_roots": ["/shared"]},
            )
        finally:
            await client.close()

    result = asyncio.run(exercise())

    assert result == {"id": "server-a", "status": "busy"}
    assert captured["url"] == "http://control:8080/api/servers/server-a/heartbeat"
    assert captured["payload"] == {
        "status": "busy",
        "current_job_id": "job-1",
        "capabilities": {"shared_roots": ["/shared"]},
    }


def test_heartbeat_capabilities_include_worker_runtime_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("OCR_AGENT_GIT_REF", "feature/ref")
    monkeypatch.setenv("OCR_AGENT_SCRIPT_VERSION", "worker-script-v1")
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        poll_interval_seconds=3,
        heartbeat_interval_seconds=7,
        shared_roots=[str(tmp_path)],
        python_executable="/opt/ocr/.venv/bin/python",
        repo_dir=str(tmp_path / "repo"),
    )

    payload = agent_main._heartbeat_capabilities(config)

    assert payload["agent"] == "ocr-platform-mvp"
    assert payload["git_ref"] == "feature/ref"
    assert payload["script_version"] == "worker-script-v1"
    assert payload["python_path"] == "/opt/ocr/.venv/bin/python"
    assert payload["repo_dir"] == str(tmp_path / "repo")
    assert payload["work_dir"] == str(tmp_path / "work")
    assert payload["shared_roots"] == [str(tmp_path)]
    assert payload["poll_interval_seconds"] == 3
    assert payload["heartbeat_interval_seconds"] == 7
    assert payload["shared_paths"][0]["path"] == str(tmp_path)


def test_next_job_returns_none_for_json_null(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"null")

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport)

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    client = ControlClient("http://control:8080", "server-a")

    async def exercise():
        try:
            return await client.next_job()
        finally:
            await client.close()

    result = asyncio.run(exercise())

    assert result is None


def test_control_call_retries_transient_control_errors():
    attempts = 0
    sleeps = []

    async def flaky_call():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("control down")
        return "ok"

    async def fake_sleep(delay):
        sleeps.append(delay)

    result = asyncio.run(
        agent_main._call_control_with_backoff(
            flaky_call,
            operation="next_job",
            initial_delay=1,
            max_delay=5,
            sleep=fake_sleep,
        )
    )

    assert result == "ok"
    assert attempts == 3
    assert sleeps == [1, 2]
