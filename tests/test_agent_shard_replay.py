import asyncio


import json


import sys


from datetime import timedelta


from pathlib import Path


import httpx


import pytest


from fastapi.testclient import TestClient


from sqlalchemy import event


from sqlalchemy.sql.dml import Update


from ocr_platform.agent import runner


from ocr_platform.agent.client import ControlClient


from ocr_platform.agent.config import AgentConfig


from ocr_platform.agent.runner import build_ocr_command


from ocr_platform.control.app import create_app


from ocr_platform.control.database import create_session_factory, init_db


from ocr_platform.control.domains.common import POOL_SERVER_ID


from ocr_platform.control.domains.manifests.commands import (
    claim_next_pending_shard,
)


from ocr_platform.control.models import Job, ScanUnit, WorkShard, utcnow


from ocr_platform.manifest.models import ManifestItem


class StaticShardClient:
    def __init__(self):
        self.claims = [
            {"id": 1, "shard_path": "/manifest/shard-1.jsonl", "file_count": 2},
            {"id": 2, "shard_path": "/manifest/shard-2.jsonl", "file_count": 3},
            None,
        ]
        self.claim_calls = []
        self.updates = []
        self.events = []

    async def claim_shard(self, job_id, server_id):
        self.claim_calls.append((job_id, server_id))
        return self.claims.pop(0)

    async def update_shard(self, shard_id, payload):
        self.updates.append((shard_id, payload))
        return {"id": shard_id, **payload}

    async def get_job(self, job_id):
        return {"id": job_id, "stop_requested": False, "status": "running"}

    async def get_job_summary(self, job_id):
        return {
            "id": job_id,
            "pending_shards": 0,
            "running_shards": 0,
            "failed_shards": 0,
            "stopped_shards": 0,
        }

    async def post_event(self, job_id, event):
        self.events.append((job_id, event))

    async def post_log(self, job_id, stream, line):
        pass


class FinishedProcess:
    returncode = 0


class RemoteSnapshotClient(StaticShardClient):
    def __init__(self):
        super().__init__()
        self.registered_manifests = []

    async def register_manifest(self, job_id, payload):
        self.registered_manifests.append((job_id, payload))
        return payload


class DistributedScanClient(RemoteSnapshotClient):
    def __init__(self, job):
        super().__init__()
        self.job = job
        self.completed_scan_units = []
        self.failed_scan_units = []

    async def get_job(self, job_id):
        return self.job

    async def complete_scan_unit(self, scan_unit_id, payload):
        self.completed_scan_units.append((scan_unit_id, payload))
        return {"id": scan_unit_id, **payload}

    async def fail_scan_unit(self, scan_unit_id, error_message, **kwargs):
        self.failed_scan_units.append((scan_unit_id, error_message, kwargs))
        return {"id": scan_unit_id, "error_message": error_message, **kwargs}


class StopAfterFirstShardClient(StaticShardClient):
    def __init__(self):
        super().__init__()
        self.claims = [
            {"id": 1, "shard_path": "/manifest/shard-1.jsonl", "file_count": 2},
            {"id": 2, "shard_path": "/manifest/shard-2.jsonl", "file_count": 3},
        ]
        self.get_job_calls = 0

    async def get_job(self, job_id):
        self.get_job_calls += 1
        if self.get_job_calls >= 2:
            return {"id": job_id, "stop_requested": True, "status": "stopping"}
        return {"id": job_id, "stop_requested": False, "status": "running"}


class ClaimNoneStoppingClient(StaticShardClient):
    def __init__(self):
        super().__init__()
        self.claims = [None]
        self.get_job_calls = 0

    async def get_job(self, job_id):
        self.get_job_calls += 1
        if self.get_job_calls == 1:
            return {"id": job_id, "stop_requested": False, "status": "running"}
        return {"id": job_id, "stop_requested": False, "status": "stopping"}


def make_static_job_client(tmp_path, *, file_count=2, max_shard_attempts=3):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(file_count):
        pdf = input_root / f"{index}.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    app = create_app(session_factory=session_factory)
    api = TestClient(app)
    api.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = api.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
            "max_shard_attempts": max_shard_attempts,
        },
    ).json()
    return api, session_factory, engine, job


def test_pending_terminal_update_rejects_later_running_overwrite(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    runner._write_pending_shard_update(
        config,
        job_id="job-1",
        shard_id=11,
        payload={"status": "running", "processed_files": 0},
    )
    terminal_ref = runner._write_pending_shard_update(
        config,
        job_id="job-1",
        shard_id=11,
        payload={"status": "succeeded", "processed_files": 1},
    )
    ignored_ref = runner._write_pending_shard_update(
        config,
        job_id="job-1",
        shard_id=11,
        payload={"status": "running", "processed_files": 0},
    )
    record = json.loads(terminal_ref.path.read_text(encoding="utf-8"))

    assert record["payload"] == {"status": "succeeded", "processed_files": 1}
    assert record["generation"] == terminal_ref.generation
    assert ignored_ref.generation == terminal_ref.generation


def test_running_replay_does_not_delete_terminal_written_while_request_inflight(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    runner._write_pending_shard_update(
        config,
        job_id="job-1",
        shard_id=11,
        payload={"status": "running", "processed_files": 1},
    )
    async def exercise():
        request_started = asyncio.Event()
        release_request = asyncio.Event()

        class DelayedSuccessClient(StaticShardClient):
            async def update_shard(self, shard_id, payload):
                self.updates.append((shard_id, payload))
                request_started.set()
                await release_request.wait()
                return {"id": shard_id, **payload}

        client = DelayedSuccessClient()
        replay_task = asyncio.create_task(
            runner.replay_pending_shard_updates(config, client)
        )
        await request_started.wait()
        terminal_ref = runner._write_pending_shard_update(
            config,
            job_id="job-1",
            shard_id=11,
            payload={"status": "succeeded", "processed_files": 1},
        )
        release_request.set()
        replayed = await replay_task
        return replayed, terminal_ref

    replayed, terminal_ref = asyncio.run(exercise())
    record = json.loads(terminal_ref.path.read_text(encoding="utf-8"))

    assert replayed == 1
    assert record["generation"] == terminal_ref.generation
    assert record["payload"]["status"] == "succeeded"


def test_direct_success_does_not_delete_newer_terminal_generation(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    async def exercise():
        request_started = asyncio.Event()
        release_request = asyncio.Event()

        class DelayedSuccessClient(StaticShardClient):
            async def update_shard(self, shard_id, payload):
                request_started.set()
                await release_request.wait()
                return {"id": shard_id, **payload}

        update_task = asyncio.create_task(
            runner._update_shard_with_transient_retry(
                DelayedSuccessClient(),
                11,
                {"status": "succeeded", "processed_files": 1},
                config,
                job_id="job-1",
            )
        )
        await request_started.wait()
        newer_ref = runner._write_pending_shard_update(
            config,
            job_id="job-1",
            shard_id=11,
            payload={"status": "stopped", "processed_files": 1},
        )
        release_request.set()
        await update_task
        return newer_ref

    newer_ref = asyncio.run(exercise())
    record = json.loads(newer_ref.path.read_text(encoding="utf-8"))

    assert record["generation"] == newer_ref.generation
    assert record["payload"]["status"] == "stopped"


def test_replay_pending_shard_updates_removes_file_after_success(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    pending_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json"
    pending_path.parent.mkdir(parents=True)
    pending_path.write_text(
        (
            '{"job_id":"job-1","shard_id":11,"server_id":"server-a",'
            '"payload":{"status":"succeeded","processed_files":1}}'
        ),
        encoding="utf-8",
    )
    client = StaticShardClient()

    replayed = asyncio.run(runner.replay_pending_shard_updates(config, client))

    assert replayed == 1
    assert client.updates == [(11, {"status": "succeeded", "processed_files": 1})]
    assert not pending_path.exists()


def test_replay_pending_shard_updates_keeps_file_on_transient_failure(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    pending_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json"
    pending_path.parent.mkdir(parents=True)
    pending_path.write_text(
        (
            '{"job_id":"job-1","shard_id":11,"server_id":"server-a",'
            '"payload":{"status":"succeeded","processed_files":1}}'
        ),
        encoding="utf-8",
    )

    class StillUnavailableClient(StaticShardClient):
        async def update_shard(self, shard_id, payload):
            self.updates.append((shard_id, payload))
            raise httpx.ConnectError("control temporarily unavailable")

    client = StillUnavailableClient()

    replayed = asyncio.run(runner.replay_pending_shard_updates(config, client))

    assert replayed == 0
    assert client.updates == [(11, {"status": "succeeded", "processed_files": 1})]
    assert pending_path.exists()


def test_replay_pending_shard_updates_skips_records_for_other_servers(tmp_path):
    config = AgentConfig(
        server_id="server-b",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    pending_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json"
    pending_path.parent.mkdir(parents=True)
    pending_path.write_text(
        (
            '{"job_id":"job-1","shard_id":11,"server_id":"server-a",'
            '"payload":{"status":"succeeded","processed_files":1}}'
        ),
        encoding="utf-8",
    )
    client = StaticShardClient()

    replayed = asyncio.run(runner.replay_pending_shard_updates(config, client))

    assert replayed == 0
    assert client.updates == []
    assert pending_path.exists()


def test_replay_pending_shard_updates_quarantines_malformed_file_and_continues(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    malformed_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-10.json"
    good_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json"
    malformed_path.parent.mkdir(parents=True)
    malformed_path.write_text('{"job_id":"job-1","shard_id":', encoding="utf-8")
    good_path.write_text(
        (
            '{"job_id":"job-1","shard_id":11,"server_id":"server-a",'
            '"payload":{"status":"succeeded","processed_files":1}}'
        ),
        encoding="utf-8",
    )
    client = StaticShardClient()

    replayed = asyncio.run(runner.replay_pending_shard_updates(config, client))

    failed_path = malformed_path.with_suffix(".json.failed")
    assert replayed == 1
    assert client.updates == [(11, {"status": "succeeded", "processed_files": 1})]
    assert not malformed_path.exists()
    assert failed_path.exists()
    failed_record = json.loads(failed_path.read_text(encoding="utf-8"))
    assert failed_record["job_id"] == "job-1"
    assert failed_record["shard_id"] == 10
    assert failed_record["raw_content"] == '{"job_id":"job-1","shard_id":'
    assert failed_record["replay_error"]["error_type"] == "JSONDecodeError"
    assert not good_path.exists()


def test_replay_pending_shard_updates_quarantines_non_transient_failure_and_continues(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    first_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json"
    second_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-12.json"
    first_path.parent.mkdir(parents=True)
    first_path.write_text(
        (
            '{"job_id":"job-1","shard_id":11,"server_id":"server-a",'
            '"payload":{"status":"succeeded","processed_files":1}}'
        ),
        encoding="utf-8",
    )
    second_path.write_text(
        (
            '{"job_id":"job-1","shard_id":12,"server_id":"server-a",'
            '"payload":{"status":"succeeded","processed_files":2}}'
        ),
        encoding="utf-8",
    )
    request = httpx.Request("POST", "http://control/api/shards/11")
    response = httpx.Response(409, request=request, json={"detail": "stale attempt"})

    class OneConflictClient(StaticShardClient):
        async def update_shard(self, shard_id, payload):
            self.updates.append((shard_id, payload))
            if shard_id == 11:
                raise httpx.HTTPStatusError("stale attempt", request=request, response=response)
            return {"id": shard_id, **payload}

    client = OneConflictClient()

    replayed = asyncio.run(runner.replay_pending_shard_updates(config, client))

    failed_path = first_path.with_suffix(".json.failed")
    assert replayed == 1
    assert client.updates == [
        (11, {"status": "succeeded", "processed_files": 1}),
        (12, {"status": "succeeded", "processed_files": 2}),
    ]
    assert not first_path.exists()
    assert failed_path.exists()
    failed_record = json.loads(failed_path.read_text(encoding="utf-8"))
    assert failed_record["replay_error"]["error_type"] == "HTTPStatusError"
    assert "stale attempt" in failed_record["replay_error"]["message"]
    assert not second_path.exists()


def test_replay_quarantine_does_not_delete_newer_terminal_generation(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    runner._write_pending_shard_update(
        config,
        job_id="job-1",
        shard_id=11,
        payload={"status": "running", "processed_files": 1},
    )
    request = httpx.Request("POST", "http://control/api/shards/11")
    response = httpx.Response(409, request=request, json={"detail": "stale attempt"})

    async def exercise():
        request_started = asyncio.Event()
        release_request = asyncio.Event()

        class DelayedConflictClient(StaticShardClient):
            async def update_shard(self, shard_id, payload):
                request_started.set()
                await release_request.wait()
                raise httpx.HTTPStatusError(
                    "stale attempt",
                    request=request,
                    response=response,
                )

        replay_task = asyncio.create_task(
            runner.replay_pending_shard_updates(config, DelayedConflictClient())
        )
        await request_started.wait()
        terminal_ref = runner._write_pending_shard_update(
            config,
            job_id="job-1",
            shard_id=11,
            payload={"status": "succeeded", "processed_files": 1},
        )
        release_request.set()
        replayed = await replay_task
        return replayed, terminal_ref

    replayed, terminal_ref = asyncio.run(exercise())
    record = json.loads(terminal_ref.path.read_text(encoding="utf-8"))

    assert replayed == 0
    assert record["generation"] == terminal_ref.generation
    assert record["payload"]["status"] == "succeeded"
    assert not terminal_ref.path.with_suffix(".json.failed").exists()


def test_terminal_shard_update_retry_does_not_hide_non_transient_control_errors(tmp_path):
    request = httpx.Request("POST", "http://control/api/shards/11")
    response = httpx.Response(409, request=request, json={"detail": "stale attempt"})

    class ConflictClient(StaticShardClient):
        async def update_shard(self, shard_id, payload):
            self.updates.append((shard_id, payload))
            raise httpx.HTTPStatusError("stale attempt", request=request, response=response)

    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = ConflictClient()

    async def exercise():
        with pytest.raises(httpx.HTTPStatusError):
            await runner._update_shard_with_transient_retry(
                client,
                11,
                {"status": "succeeded", "processed_files": 1},
                config,
            )

    asyncio.run(exercise())

    assert len(client.updates) == 1


def test_terminal_shard_update_quarantines_pending_file_on_non_transient_error(tmp_path):
    request = httpx.Request("POST", "http://control/api/shards/11")
    response = httpx.Response(409, request=request, json={"detail": "stale attempt"})

    class ConflictClient(StaticShardClient):
        async def update_shard(self, shard_id, payload):
            self.updates.append((shard_id, payload))
            raise httpx.HTTPStatusError("stale attempt", request=request, response=response)

    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = ConflictClient()

    async def exercise():
        with pytest.raises(httpx.HTTPStatusError):
            await runner._update_shard_with_transient_retry(
                client,
                11,
                {"status": "succeeded", "processed_files": 1},
                config,
                job_id="job-1",
            )

    asyncio.run(exercise())

    pending_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json"
    failed_path = pending_path.with_suffix(".json.failed")
    assert not pending_path.exists()
    failed_record = json.loads(failed_path.read_text(encoding="utf-8"))
    assert failed_record["payload"] == {"status": "succeeded", "processed_files": 1}
    assert failed_record["replay_error"]["error_type"] == "HTTPStatusError"
    assert "stale attempt" in failed_record["replay_error"]["message"]


def test_run_static_sharded_job_pauses_shard_claim_when_resource_constrained(
    tmp_path, monkeypatch
):
    async def fake_run_job(job, config, client):
        return 0

    pressure_states = [
        {"constrained": True, "reasons": ["memory percent 95.0% >= 90.0%"]},
        {"constrained": False, "reasons": []},
        {"constrained": False, "reasons": []},
    ]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    def fake_resource_pressure(config):
        return pressure_states.pop(0)

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    monkeypatch.setattr(runner, "resource_pressure", fake_resource_pressure)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
        poll_interval_seconds=7,
    )
    client = StaticShardClient()
    client.claims = [
        {"id": 1, "shard_path": "/manifest/shard-1.jsonl", "file_count": 1},
        None,
    ]
    job = {
        "id": "job-1",
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
        "has_static_shards": True,
    }

    result = asyncio.run(runner.run_static_sharded_job(job, config, client))

    assert result == 0
    assert sleeps == [7]
    assert client.claim_calls == [
        ("job-1", "server-a"),
        ("job-1", "server-a"),
    ]
    assert client.events[0] == (
        "job-1",
        {
            "type": "resource_pressure",
            "payload": {
                "stage": "before_shard_claim",
                "server_id": "server-a",
                "pressure": {
                    "constrained": True,
                    "reasons": ["memory percent 95.0% >= 90.0%"],
                },
            },
        },
    )
    assert len(client.updates) == 1


def test_run_static_sharded_job_clears_remote_snapshot_mode_for_child_shards(
    tmp_path, monkeypatch
):
    seen_jobs = []

    async def fake_run_job(job, config, client):
        seen_jobs.append(job)
        return 0

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = StaticShardClient()
    job = {
        "id": "job-1",
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
        "input_mode": "remote_folder_snapshot",
        "has_static_shards": True,
    }

    result = asyncio.run(runner.run_static_sharded_job(job, config, client))

    assert result == 0
    assert [item["input_mode"] for item in seen_jobs] == ["folder_snapshot", "folder_snapshot"]
    assert [item["shard"]["id"] for item in seen_jobs] == [1, 2]


def test_run_static_sharded_job_emits_stopped_when_claim_none_parent_stopping(
    tmp_path, monkeypatch
):
    async def fake_run_job(job, config, client):
        raise AssertionError("no shard should be dispatched")

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = ClaimNoneStoppingClient()

    result = asyncio.run(
        runner.run_static_sharded_job(
            {
                "id": "job-1",
                "input_dir": "/shared/input",
                "output_dir": "/shared/output",
                "engine": "dotsocr",
                "has_static_shards": True,
            },
            config,
            client,
        )
    )

    assert result == 1
    assert client.claim_calls == [("job-1", "server-a")]
    assert client.events == [
        ("job-1", {"type": "job_stopped", "payload": {"static_shards_final": True}})
    ]


def test_run_static_sharded_job_marks_current_shard_stopped_and_stops_claiming(
    tmp_path, monkeypatch
):
    async def fake_run_job(job, config, client):
        return -15

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = StopAfterFirstShardClient()

    result = asyncio.run(
        runner.run_static_sharded_job(
            {
                "id": "job-1",
                "input_dir": "/shared/input",
                "output_dir": "/shared/output",
                "engine": "dotsocr",
                "has_static_shards": True,
            },
            config,
            client,
        )
    )

    assert result == -15
    assert client.claim_calls == [("job-1", "server-a")]
    assert client.updates == [
        (
            1,
            {
                "status": "stopped",
                "processed_files": 0,
                "failed_files": 0,
                "skipped_files": 0,
                "completed_pages": 0,
                "failure_category": "operator_stopped",
            },
        )
    ]
    assert client.events == [
        (
            "job-1",
            {"type": "job_stopped", "payload": {"static_shards_final": True, "return_code": -15}},
        )
    ]


def test_run_static_sharded_job_marks_child_job_failed_exit_zero_as_failed(
    tmp_path, monkeypatch
):
    def fake_build_ocr_command(job, config):
        event_file = tmp_path / "events.jsonl"
        command = [
            sys.executable,
            "-c",
            (
                "import json, pathlib, sys\n"
                "path = pathlib.Path(sys.argv[1])\n"
                "path.write_text(json.dumps({"
                "'type': 'job_failed', "
                "'payload': {'error': 'one file failed'}"
                "}) + '\\n', encoding='utf-8')\n"
                "raise SystemExit(0)\n"
            ),
            str(event_file),
        ]
        return command, event_file

    monkeypatch.setattr(runner, "build_ocr_command", fake_build_ocr_command)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = StaticShardClient()
    client.claims = [
        {"id": 1, "shard_path": "/manifest/shard-1.jsonl", "file_count": 1},
        None,
    ]

    result = asyncio.run(
        runner.run_static_sharded_job(
            {
                "id": "job-1",
                "input_dir": "/shared/input",
                "output_dir": "/shared/output",
                "engine": "dotsocr",
                "has_static_shards": True,
            },
            config,
            client,
        )
    )

    assert result == 1
    assert client.updates[-1] == (
        1,
        {
            "status": "failed",
            "processed_files": 0,
            "failed_files": 0,
            "skipped_files": 0,
            "completed_pages": 0,
            "failure_category": "parser_failed",
            "error_message": "one file failed",
        },
    )
    assert client.events == [
        ("job-1", {"type": "job_failed", "payload": {"error": "one file failed"}}),
        (
            "job-1",
            {
                "type": "job_failed",
                "payload": {
                    "static_shards_final": True,
                    "return_code": 1,
                    "failure_category": "parser_failed",
                    "error_message": "one file failed",
                },
            },
        ),
    ]
