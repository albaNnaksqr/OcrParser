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


def test_static_shard_claim_and_update_routes(tmp_path):
    api, _, _, job = make_static_job_client(tmp_path)

    claim_response = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    )
    assert claim_response.status_code == 200
    claimed = claim_response.json()
    assert claimed["shard_index"] == 1
    assert claimed["status"] == "running"
    assert claimed["assigned_server_id"] == "server-a"

    update_response = api.post(
        f"/api/shards/{claimed['id']}",
        json={"status": "succeeded", "processed_files": 1},
    )
    assert update_response.status_code == 200
    assert update_response.json()["status"] == "succeeded"
    assert update_response.json()["processed_files"] == 1

    next_claim = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    assert next_claim["shard_index"] == 2


@pytest.mark.parametrize("terminal_status", ["succeeded", "failed", "stopped"])
def test_late_updates_do_not_regress_terminal_shard(tmp_path, terminal_status):
    api, _, _, job = make_static_job_client(
        tmp_path,
        file_count=1,
        max_shard_attempts=1,
    )
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    terminal = api.post(
        f"/api/shards/{claimed['id']}",
        json={
            "status": terminal_status,
            "processed_files": 1,
            "assigned_server_id": "server-a",
            "attempt_count": claimed["attempt_count"],
        },
    )
    duplicate_terminal = api.post(
        f"/api/shards/{claimed['id']}",
        json={
            "status": terminal_status,
            "processed_files": 0,
            "assigned_server_id": "server-a",
            "attempt_count": claimed["attempt_count"],
        },
    )

    late_progress = api.post(
        f"/api/shards/{claimed['id']}",
        json={
            "status": "running",
            "processed_files": 0,
            "assigned_server_id": "server-a",
            "attempt_count": claimed["attempt_count"],
        },
    )
    wrong_server = api.post(
        f"/api/shards/{claimed['id']}",
        json={
            "status": "running",
            "assigned_server_id": "server-b",
            "attempt_count": claimed["attempt_count"],
        },
    )
    stale_attempt = api.post(
        f"/api/shards/{claimed['id']}",
        json={
            "status": "running",
            "assigned_server_id": "server-a",
            "attempt_count": claimed["attempt_count"] - 1,
        },
    )

    assert terminal.status_code == 200
    assert terminal.json()["status"] == terminal_status
    assert duplicate_terminal.status_code == 200
    assert duplicate_terminal.json()["status"] == terminal_status
    assert duplicate_terminal.json()["processed_files"] == 1
    assert late_progress.status_code == 200
    assert late_progress.json()["status"] == terminal_status
    assert late_progress.json()["processed_files"] == 1
    assert wrong_server.status_code == 409
    assert "different server" in wrong_server.json()["detail"]
    assert stale_attempt.status_code == 409
    assert "stale attempt" in stale_attempt.json()["detail"]


def test_spooled_running_replay_after_terminal_success_stays_terminal(tmp_path):
    api, _, _, job = make_static_job_client(tmp_path, file_count=1)
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
    )
    event_file = tmp_path / "events.jsonl"
    event_file.write_text(
        json.dumps(
            {
                "type": "file_done",
                "payload": {"file_path": "0.pdf", "status": "succeeded"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class Finished:
        returncode = 0

    class UnavailableShardUpdateClient:
        async def post_event(self, job_id, event):
            return None

        async def update_shard(self, shard_id, payload):
            request = httpx.Request("POST", f"http://control/api/shards/{shard_id}")
            raise httpx.ConnectError("control unavailable", request=request)

    asyncio.run(
        runner._forward_events_until_done(
            event_file,
            job["id"],
            UnavailableShardUpdateClient(),
            Finished(),
            shard_id=claimed["id"],
            shard_update_context=claimed,
            config=config,
        )
    )
    pending_path = (
        tmp_path
        / "work"
        / "jobs"
        / job["id"]
        / "pending-shard-updates"
        / f"shard-{claimed['id']}.json"
    )
    pending_record = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending_record["payload"]["status"] == "running"

    succeeded = api.post(
        f"/api/shards/{claimed['id']}",
        json={
            "status": "succeeded",
            "processed_files": 1,
            "assigned_server_id": "server-a",
            "attempt_count": claimed["attempt_count"],
        },
    )
    assert succeeded.status_code == 200

    class ApiShardClient:
        async def update_shard(self, shard_id, payload):
            response = api.post(f"/api/shards/{shard_id}", json=payload)
            response.raise_for_status()
            return response.json()

    replayed = asyncio.run(runner.replay_pending_shard_updates(config, ApiShardClient()))
    current = api.get(f"/api/jobs/{job['id']}/shards").json()["items"][0]

    assert replayed == 1
    assert not pending_path.exists()
    assert current["status"] == "succeeded"
    assert current["processed_files"] == 1


def test_short_control_outage_after_shard_exit_reaches_terminal_state(
    tmp_path,
    monkeypatch,
):
    api, session_factory, _, created_job = make_static_job_client(
        tmp_path,
        file_count=1,
    )
    claimed_job = api.post("/api/agents/server-a/next-job").json()
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
        control_retry_initial_seconds=0.1,
        control_retry_max_seconds=0.2,
    )
    events = []

    async def fake_run_job(shard_job, config, client):
        shard = shard_job["shard"]
        runner._write_pending_shard_update(
            config,
            job_id=shard_job["id"],
            shard_id=shard["id"],
            payload={
                "status": "running",
                "processed_files": 1,
                "assigned_server_id": shard["assigned_server_id"],
                "attempt_count": shard["attempt_count"],
            },
        )
        shard_job["_shard_progress"] = {"processed_files": 1}
        return 0

    class RecoveringApiClient:
        def __init__(self):
            self.get_job_calls = 0

        async def get_job(self, job_id):
            self.get_job_calls += 1
            if self.get_job_calls == 2:
                request = httpx.Request("GET", f"http://control/api/jobs/{job_id}")
                raise httpx.ConnectError("control temporarily unavailable", request=request)
            return api.get(f"/api/jobs/{job_id}").json()

        async def claim_shard(self, job_id, server_id):
            return api.post(
                f"/api/jobs/{job_id}/shards/claim",
                params={"server_id": server_id},
            ).json()

        async def update_shard(self, shard_id, payload):
            response = api.post(f"/api/shards/{shard_id}", json=payload)
            response.raise_for_status()
            return response.json()

        async def get_job_summary(self, job_id):
            return api.get(f"/api/jobs/{job_id}/summary").json()

        async def post_event(self, job_id, event):
            events.append(event)
            response = api.post(f"/api/jobs/{job_id}/events", json=event)
            response.raise_for_status()
            return response.json()

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    result = asyncio.run(
        runner.run_static_sharded_job(
            claimed_job,
            config,
            RecoveringApiClient(),
        )
    )

    pending_path = (
        tmp_path
        / "work"
        / "jobs"
        / created_job["id"]
        / "pending-shard-updates"
        / "shard-1.json"
    )
    with session_factory() as session:
        shard = session.query(WorkShard).filter_by(job_id=created_job["id"]).one()
        parent = session.get(Job, created_job["id"])
        assert shard.status == "succeeded"
        assert parent.status == "succeeded"

    assert result == 0
    assert not pending_path.exists()
    assert not any(event["type"] == "job_failed" for event in events)
    assert events == [
        {"type": "job_done", "payload": {"static_shards_final": True}}
    ]


def test_next_job_returns_running_assigned_job_with_more_static_shards(tmp_path):
    api, _, _, job = make_static_job_client(tmp_path)

    first_job = api.post("/api/agents/server-a/next-job")
    first_shard = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    api.post(
        f"/api/shards/{first_shard['id']}",
        json={"status": "succeeded", "processed_files": 1},
    )

    resumed_job = api.post("/api/agents/server-a/next-job")
    second_shard = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()

    assert first_job.status_code == 200
    assert first_job.json()["id"] == job["id"]
    assert resumed_job.status_code == 200
    assert resumed_job.json()["id"] == job["id"]
    assert second_shard["shard_index"] == 2


def test_existing_manifest_resumes_until_ten_shards_finish_once(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    input_root.mkdir()
    manifest_path = tmp_path / "input.jsonl"
    manifest_lines = []
    for index in range(10):
        pdf = input_root / f"{index}.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        manifest_lines.append(
            ManifestItem(
                input_path=str(pdf),
                relative_path=pdf.name,
                size_bytes=pdf.stat().st_size,
                mtime_ns=pdf.stat().st_mtime_ns,
            ).to_json_line()
        )
    manifest_path.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    api = TestClient(create_app(session_factory=session_factory))
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
            "input_mode": "existing_manifest",
            "manifest_path": str(manifest_path),
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
        },
    ).json()
    artifacts = []

    async def fake_run_job(shard_job, config, client):
        artifact = tmp_path / "output" / f"shard-{shard_job['shard']['id']}.md"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(str(shard_job["shard"]["id"]), encoding="utf-8")
        artifacts.append(artifact)
        shard_job["_shard_progress"] = {"processed_files": 1}
        return 0

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
    )
    final_events = []

    class SingleClaimApiClient:
        def __init__(self):
            self.claimed = False

        async def get_job(self, job_id):
            return api.get(f"/api/jobs/{job_id}").json()

        async def claim_shard(self, job_id, server_id):
            if self.claimed:
                return None
            self.claimed = True
            return api.post(
                f"/api/jobs/{job_id}/shards/claim",
                params={"server_id": server_id},
            ).json()

        async def update_shard(self, shard_id, payload):
            response = api.post(f"/api/shards/{shard_id}", json=payload)
            response.raise_for_status()
            return response.json()

        async def get_job_summary(self, job_id):
            return api.get(f"/api/jobs/{job_id}/summary").json()

        async def post_event(self, job_id, event):
            if event["payload"].get("static_shards_final"):
                final_events.append(event)
            response = api.post(f"/api/jobs/{job_id}/events", json=event)
            response.raise_for_status()
            return response.json()

    for _ in range(10):
        resumed = api.post("/api/agents/server-a/next-job")
        assert resumed.status_code == 200
        assert resumed.json()["id"] == job["id"]
        assert asyncio.run(
            runner.run_static_sharded_job(resumed.json(), config, SingleClaimApiClient())
        ) == 0

    with session_factory() as session:
        shards = session.query(WorkShard).filter_by(job_id=job["id"]).all()
        parent = session.get(Job, job["id"])
        assert len(shards) == 10
        assert all(shard.status == "succeeded" for shard in shards)
        assert all(shard.attempt_count == 1 for shard in shards)
        assert parent.status == "succeeded"

    assert len(artifacts) == 10
    assert len(set(artifacts)) == 10
    assert len(list((tmp_path / "output").glob("shard-*.md"))) == 10
    assert final_events == [
        {"type": "job_done", "payload": {"static_shards_final": True}}
    ]


def test_eligible_agent_claims_unassigned_pool_static_job(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    app = create_app(session_factory=session_factory)
    api = TestClient(app)
    api.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    api.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "shared_paths": [
                    {
                        "path": str(tmp_path),
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                    }
                ]
            },
        },
    )
    job = api.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
        },
    ).json()

    claimed = api.post("/api/agents/server-a/next-job").json()

    assert claimed["id"] == job["id"]
    assert claimed["status"] == "running"
    assert claimed["assigned_server_id"] is None
    assert claimed["has_static_shards"] is True
    with session_factory() as session:
        parent = session.get(Job, job["id"])
        assert parent.assigned_server_id == POOL_SERVER_ID


def test_ineligible_agent_does_not_claim_pool_static_job(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    app = create_app(session_factory=session_factory)
    api = TestClient(app)
    api.post(
        "/api/servers/register",
        json={"id": "server-b", "name": "Server B", "host": "localhost"},
    )
    api.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
        },
    )

    response = api.post("/api/agents/server-b/next-job")

    assert response.status_code == 200
    assert response.json() is None


def test_pool_static_job_can_be_seen_by_second_eligible_agent_while_running(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(2):
        (input_root / f"{index}.pdf").write_bytes(b"%PDF-1.4\n")
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    app = create_app(session_factory=session_factory)
    api = TestClient(app)
    for server_id in ["server-a", "server-b"]:
        api.post(
            "/api/servers/register",
            json={"id": server_id, "name": server_id, "host": "localhost"},
        )
        api.post(
            f"/api/servers/{server_id}/heartbeat",
            json={
                "status": "idle",
                "capabilities": {
                    "shared_paths": [
                        {
                            "path": str(tmp_path),
                            "exists": True,
                            "is_dir": True,
                            "readable": True,
                        }
                    ]
                },
            },
        )
    job = api.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
        },
    ).json()
    first = api.post("/api/agents/server-a/next-job").json()

    second = api.post("/api/agents/server-b/next-job").json()

    assert first["id"] == job["id"]
    assert second["id"] == job["id"]


def test_static_sharded_job_does_not_finalize_when_other_shards_are_running(
    tmp_path, monkeypatch
):
    async def fake_run_job(job, config, client):
        raise AssertionError("no shard should be dispatched")

    class RunningShardClient(StaticShardClient):
        def __init__(self):
            super().__init__()
            self.claims = [None]

        async def get_job_summary(self, job_id):
            return {
                "id": job_id,
                "pending_shards": 0,
                "running_shards": 1,
                "failed_shards": 0,
                "stopped_shards": 0,
            }

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = RunningShardClient()

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

    assert result == 0
    assert client.events == []


def test_static_shard_claim_sets_lease_deadline(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)

    claim_response = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    )

    assert claim_response.status_code == 200
    claimed = claim_response.json()
    assert claimed["status"] == "running"
    assert claimed["lease_expires_at"] is not None
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        assert shard.lease_expires_at is not None


def test_expired_running_shard_can_be_reclaimed_by_another_server(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    first_claim = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    api.post(
        "/api/servers/register",
        json={"id": "server-b", "name": "Server B", "host": "localhost"},
    )
    with session_factory() as session:
        shard = session.get(WorkShard, first_claim["id"])
        shard.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    second_claim = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-b"},
    )

    assert second_claim.status_code == 200
    reclaimed = second_claim.json()
    assert reclaimed["id"] == first_claim["id"]
    assert reclaimed["assigned_server_id"] == "server-b"
    assert reclaimed["attempt_count"] == 2
    assert reclaimed["lease_expires_at"] is not None
    with session_factory() as session:
        shard = session.get(WorkShard, first_claim["id"])
        assert shard.assigned_server_id == "server-b"
        assert shard.attempt_count == 2


def test_late_running_update_does_not_revive_stale_attempt(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        shard.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    summary = api.get(f"/api/jobs/{job['id']}/summary")
    attempts_before = api.get(
        f"/api/jobs/{job['id']}/shards/{claimed['id']}/attempts"
    ).json()
    late_running = api.post(
        f"/api/shards/{claimed['id']}",
        json={
            "status": "running",
            "processed_files": 1,
            "assigned_server_id": "server-a",
            "attempt_count": claimed["attempt_count"],
        },
    )
    attempts_after = api.get(
        f"/api/jobs/{job['id']}/shards/{claimed['id']}/attempts"
    ).json()

    assert summary.status_code == 200
    assert late_running.status_code == 200
    assert late_running.json()["status"] == "stale"
    assert late_running.json()["lease_expires_at"] is None
    assert attempts_after == attempts_before
    assert attempts_after[0]["status"] == "stale"
    assert attempts_after[0]["finished_at"] is not None

    terminal = api.post(
        f"/api/shards/{claimed['id']}",
        json={
            "status": "succeeded",
            "processed_files": 1,
            "assigned_server_id": "server-a",
            "attempt_count": claimed["attempt_count"],
        },
    )
    assert terminal.status_code == 200
    assert terminal.json()["status"] == "succeeded"


def test_late_running_update_does_not_revive_retrying_attempt(tmp_path):
    api, _, _, job = make_static_job_client(tmp_path, file_count=1)
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    retrying = api.post(
        f"/api/shards/{claimed['id']}",
        json={
            "status": "failed",
            "assigned_server_id": "server-a",
            "attempt_count": claimed["attempt_count"],
            "error_message": "transient parser failure",
        },
    )
    late_running = api.post(
        f"/api/shards/{claimed['id']}",
        json={
            "status": "running",
            "assigned_server_id": "server-a",
            "attempt_count": claimed["attempt_count"],
        },
    )

    assert retrying.status_code == 200
    assert retrying.json()["status"] == "retrying"
    assert late_running.status_code == 200
    assert late_running.json()["status"] == "retrying"
    assert late_running.json()["error_message"] == "transient parser failure"

    terminal = api.post(
        f"/api/shards/{claimed['id']}",
        json={
            "status": "succeeded",
            "processed_files": 1,
            "assigned_server_id": "server-a",
            "attempt_count": claimed["attempt_count"],
        },
    )
    assert terminal.status_code == 200
    assert terminal.json()["status"] == "succeeded"


def test_reclaimed_expired_shard_closes_stale_attempt_history(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    first_claim = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    api.post(
        "/api/servers/register",
        json={"id": "server-b", "name": "Server B", "host": "localhost"},
    )
    with session_factory() as session:
        shard = session.get(WorkShard, first_claim["id"])
        shard.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    second_claim = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-b"},
    )

    assert second_claim.status_code == 200
    attempts = api.get(
        f"/api/jobs/{job['id']}/shards/{first_claim['id']}/attempts"
    ).json()
    assert [
        (attempt["attempt_number"], attempt["server_id"], attempt["status"])
        for attempt in attempts
    ] == [
        (1, "server-a", "stale"),
        (2, "server-b", "running"),
    ]
    assert attempts[0]["finished_at"] is not None
    assert attempts[0]["failure_category"] == "lease_expired"
    assert attempts[1]["finished_at"] is None


def test_server_heartbeat_renews_running_shard_lease(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    old_deadline = utcnow() + timedelta(seconds=5)
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        shard.lease_expires_at = old_deadline
        session.commit()

    heartbeat = api.post(
        "/api/servers/server-a/heartbeat",
        json={"status": "busy", "current_job_id": job["id"]},
    )

    assert heartbeat.status_code == 200
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        assert shard.lease_expires_at > old_deadline


def test_idle_or_jobless_heartbeat_does_not_renew_work_leases(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    old_deadline = utcnow() + timedelta(seconds=30)
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        shard.lease_expires_at = old_deadline
        scan_unit = ScanUnit(
            job_id=job["id"],
            path="/shared/input/scan-a",
            status="running",
            assigned_server_id="server-a",
            attempt_count=1,
            lease_expires_at=old_deadline,
        )
        session.add(scan_unit)
        session.commit()
        scan_unit_id = scan_unit.id

    idle = api.post(
        "/api/servers/server-a/heartbeat",
        json={"status": "idle", "current_job_id": job["id"]},
    )
    jobless = api.post(
        "/api/servers/server-a/heartbeat",
        json={"status": "busy", "current_job_id": None},
    )

    assert idle.status_code == 200
    assert jobless.status_code == 200
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        scan_unit = session.get(ScanUnit, scan_unit_id)
        assert shard.lease_expires_at == old_deadline
        assert scan_unit.lease_expires_at == old_deadline


def test_busy_heartbeat_renews_only_matching_live_job_leases(tmp_path):
    api, session_factory, _, job_a = make_static_job_client(tmp_path, file_count=2)
    input_b = tmp_path / "input-b"
    input_b.mkdir()
    (input_b / "0.pdf").write_bytes(b"%PDF-1.4\n")
    job_b = api.post(
        "/api/jobs",
        json={
            "input_dir": str(input_b),
            "output_dir": str(tmp_path / "output-b"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests-b"),
            "target_files_per_shard": 1,
        },
    ).json()
    live_a = api.post(
        f"/api/jobs/{job_a['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    expired_a = api.post(
        f"/api/jobs/{job_a['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    live_b = api.post(
        f"/api/jobs/{job_b['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    live_deadline = utcnow() + timedelta(seconds=30)
    expired_deadline = utcnow() - timedelta(seconds=1)
    with session_factory() as session:
        session.get(WorkShard, live_a["id"]).lease_expires_at = live_deadline
        session.get(WorkShard, expired_a["id"]).lease_expires_at = expired_deadline
        session.get(WorkShard, live_b["id"]).lease_expires_at = live_deadline
        live_scan_a = ScanUnit(
            job_id=job_a["id"],
            path="/shared/input/live-a",
            status="running",
            assigned_server_id="server-a",
            attempt_count=1,
            lease_expires_at=live_deadline,
        )
        expired_scan_a = ScanUnit(
            job_id=job_a["id"],
            path="/shared/input/expired-a",
            status="running",
            assigned_server_id="server-a",
            attempt_count=1,
            lease_expires_at=expired_deadline,
        )
        live_scan_b = ScanUnit(
            job_id=job_b["id"],
            path="/shared/input/live-b",
            status="running",
            assigned_server_id="server-a",
            attempt_count=1,
            lease_expires_at=live_deadline,
        )
        session.add_all([live_scan_a, expired_scan_a, live_scan_b])
        session.commit()
        scan_ids = (live_scan_a.id, expired_scan_a.id, live_scan_b.id)

    heartbeat = api.post(
        "/api/servers/server-a/heartbeat",
        json={"status": "busy", "current_job_id": job_a["id"]},
    )

    assert heartbeat.status_code == 200
    with session_factory() as session:
        renewed_shard = session.get(WorkShard, live_a["id"])
        expired_shard = session.get(WorkShard, expired_a["id"])
        other_job_shard = session.get(WorkShard, live_b["id"])
        renewed_scan = session.get(ScanUnit, scan_ids[0])
        expired_scan = session.get(ScanUnit, scan_ids[1])
        other_job_scan = session.get(ScanUnit, scan_ids[2])
        assert renewed_shard.lease_expires_at > live_deadline
        assert renewed_scan.lease_expires_at == renewed_shard.lease_expires_at
        assert expired_shard.lease_expires_at == expired_deadline
        assert expired_scan.lease_expires_at == expired_deadline
        assert other_job_shard.lease_expires_at == live_deadline
        assert other_job_scan.lease_expires_at == live_deadline


def test_same_server_restart_reclaims_expired_attempt_and_completes_all_shards(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=10)
    claimed_job = api.post("/api/agents/server-a/next-job").json()
    assert claimed_job["id"] == job["id"]

    for _ in range(9):
        shard = api.post(
            f"/api/jobs/{job['id']}/shards/claim",
            params={"server_id": "server-a"},
        ).json()
        completed = api.post(
            f"/api/shards/{shard['id']}",
            json={
                "status": "succeeded",
                "processed_files": 1,
                "assigned_server_id": "server-a",
                "attempt_count": shard["attempt_count"],
            },
        )
        assert completed.status_code == 200

    interrupted = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    with session_factory() as session:
        shard = session.get(WorkShard, interrupted["id"])
        shard.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    restarted_idle = api.post(
        "/api/servers/server-a/heartbeat",
        json={"status": "idle", "current_job_id": None},
    )
    resumed_job = api.post("/api/agents/server-a/next-job").json()
    pre_claim_busy = api.post(
        "/api/servers/server-a/heartbeat",
        json={"status": "busy", "current_job_id": job["id"]},
    )
    reclaimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()

    assert restarted_idle.status_code == 200
    assert resumed_job["id"] == job["id"]
    assert pre_claim_busy.status_code == 200
    assert reclaimed["id"] == interrupted["id"]
    assert reclaimed["attempt_count"] == 2

    completed = api.post(
        f"/api/shards/{reclaimed['id']}",
        json={
            "status": "succeeded",
            "processed_files": 1,
            "assigned_server_id": "server-a",
            "attempt_count": reclaimed["attempt_count"],
        },
    )
    finalized = api.post(
        f"/api/jobs/{job['id']}/events",
        json={"type": "job_done", "payload": {"static_shards_final": True}},
    )
    summary = api.get(f"/api/jobs/{job['id']}/summary").json()

    assert completed.status_code == 200
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "succeeded"
    assert summary["succeeded_shards"] == 10
    assert summary["running_shards"] == 0


def test_healthy_same_server_attempt_is_not_reclaimed_by_idle_restart(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    api.post("/api/agents/server-a/next-job")
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    old_deadline = utcnow() + timedelta(seconds=30)
    with session_factory() as session:
        session.get(WorkShard, claimed["id"]).lease_expires_at = old_deadline
        session.commit()

    active_heartbeat = api.post(
        "/api/servers/server-a/heartbeat",
        json={"status": "busy", "current_job_id": job["id"]},
    )
    with session_factory() as session:
        renewed_deadline = session.get(WorkShard, claimed["id"]).lease_expires_at
    restarted_idle = api.post(
        "/api/servers/server-a/heartbeat",
        json={"status": "idle", "current_job_id": None},
    )
    next_job = api.post("/api/agents/server-a/next-job")
    duplicate_claim = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    )

    assert active_heartbeat.status_code == 200
    assert renewed_deadline > old_deadline
    assert restarted_idle.status_code == 200
    assert next_job.status_code == 200
    assert next_job.json() is None
    assert duplicate_claim.status_code == 200
    assert duplicate_claim.json() is None
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        assert shard.status == "running"
        assert shard.attempt_count == 1
        assert shard.lease_expires_at == renewed_deadline


def test_child_terminal_events_do_not_finalize_static_parent_until_final_event(tmp_path):
    api, _, _, job = make_static_job_client(tmp_path, file_count=1)
    api.post("/api/agents/server-a/next-job")

    child_done = api.post(
        f"/api/jobs/{job['id']}/events",
        json={"type": "job_done", "payload": {}},
    )
    assert child_done.status_code == 200
    assert child_done.json()["status"] == "running"

    final_done = api.post(
        f"/api/jobs/{job['id']}/events",
        json={"type": "job_done", "payload": {"static_shards_final": True}},
    )
    assert final_done.status_code == 200
    assert final_done.json()["status"] == "succeeded"


@pytest.mark.parametrize("status", ["stopping", "stopped", "failed", "succeeded"])
def test_claim_next_pending_shard_refuses_stopped_or_terminal_parent(tmp_path, status):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    with session_factory() as session:
        parent = session.get(Job, job["id"])
        parent.status = status
        session.commit()

    response = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    )

    assert response.status_code == 200
    assert response.json() is None
    with session_factory() as session:
        shard = session.query(WorkShard).filter_by(job_id=job["id"]).one()
        assert shard.status == "pending"
        assert shard.assigned_server_id is None


def test_claim_next_pending_shard_refuses_stop_requested_parent(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    with session_factory() as session:
        parent = session.get(Job, job["id"])
        parent.status = "running"
        parent.stop_requested = True
        session.commit()

    response = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    )

    assert response.status_code == 200
    assert response.json() is None


def test_claim_next_pending_shard_does_not_claim_if_parent_stops_before_update(
    tmp_path,
):
    _, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    update_seen = False

    with session_factory() as session:
        original_execute = session.execute

        def execute_with_stop_race(statement, *args, **kwargs):
            nonlocal update_seen
            if (
                not update_seen
                and isinstance(statement, Update)
                and statement.table.name == WorkShard.__tablename__
            ):
                update_seen = True
                with session_factory() as stop_session:
                    parent = stop_session.get(Job, job["id"])
                    parent.status = "stopping"
                    parent.stop_requested = True
                    stop_session.commit()
            return original_execute(statement, *args, **kwargs)

        session.execute = execute_with_stop_race

        claimed = claim_next_pending_shard(
            session,
            job["id"],
            "server-a",
        )

    assert update_seen is True
    assert claimed is None
    with session_factory() as session:
        shard = session.query(WorkShard).filter_by(job_id=job["id"]).one()
        parent = session.get(Job, job["id"])
        assert parent.status == "stopping"
        assert parent.stop_requested is True
        assert shard.status == "pending"
        assert shard.assigned_server_id is None
        assert shard.attempt_count == 0


def test_request_stop_stops_unclaimed_shards_and_finalizes_after_running_shard_stops(
    tmp_path,
):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=2)
    with session_factory() as session:
        parent = session.get(Job, job["id"])
        parent.status = "running"
        session.commit()
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()

    stop_response = api.post(f"/api/jobs/{job['id']}/request-stop")

    assert stop_response.status_code == 200
    assert stop_response.json()["status"] == "stopping"
    with session_factory() as session:
        shards = session.query(WorkShard).filter_by(job_id=job["id"]).order_by(WorkShard.shard_index).all()
        assert [shard.status for shard in shards] == ["running", "stopped"]

    update_response = api.post(
        f"/api/shards/{claimed['id']}",
        json={"status": "stopped", "processed_files": 0, "failure_category": "operator_stopped"},
    )

    assert update_response.status_code == 200
    summary = api.get(f"/api/jobs/{job['id']}/summary").json()
    assert summary["status"] == "stopped"
    assert summary["failure_category"] == "operator_stopped"
    assert summary["running_shards"] == 0
    assert summary["stopped_shards"] == 2


def test_stopping_job_finalizes_when_running_shard_lease_expires(
    tmp_path,
):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    with session_factory() as session:
        parent = session.get(Job, job["id"])
        parent.status = "running"
        session.commit()
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    api.post(f"/api/jobs/{job['id']}/request-stop")
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        shard.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    summary = api.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["status"] == "stopped"
    assert summary["failure_category"] == "operator_stopped"
    assert summary["running_shards"] == 0
    assert summary["stopped_shards"] == 1


def test_stopping_job_lease_expiry_closes_operator_stopped_attempt_history(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    with session_factory() as session:
        parent = session.get(Job, job["id"])
        parent.status = "running"
        session.commit()
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    api.post(f"/api/jobs/{job['id']}/request-stop")
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        shard.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    summary = api.get(f"/api/jobs/{job['id']}/summary")

    assert summary.status_code == 200
    attempts = api.get(
        f"/api/jobs/{job['id']}/shards/{claimed['id']}/attempts"
    ).json()
    assert [
        (attempt["attempt_number"], attempt["server_id"], attempt["status"])
        for attempt in attempts
    ] == [(1, "server-a", "stopped")]
    assert attempts[0]["failure_category"] == "operator_stopped"
    assert attempts[0]["finished_at"] is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "exploded", "processed_files": 1},
        {"status": "succeeded", "processed_files": -1},
    ],
)
def test_invalid_shard_update_is_rejected_and_not_persisted(tmp_path, payload):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()

    response = api.post(f"/api/shards/{claimed['id']}", json=payload)

    assert response.status_code in {400, 422}
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        assert shard.status == "running"
        assert shard.processed_files == 0


def test_has_static_shards_response_uses_count_not_relationship_load(tmp_path):
    api, _, engine, static_job = make_static_job_client(tmp_path, file_count=2)
    directory_job = api.post(
        "/api/jobs",
        json={
            "input_dir": str(tmp_path / "directory-input"),
            "output_dir": str(tmp_path / "directory-output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    try:
        static_response = api.get(f"/api/jobs/{static_job['id']}").json()
        directory_response = api.get(f"/api/jobs/{directory_job['id']}").json()
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert static_response["has_static_shards"] is True
    assert directory_response["has_static_shards"] is False
    assert not any("SELECT work_shards.id" in statement for statement in statements)


def test_control_client_claims_and_updates_shards(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/shards/claim"):
            assert request.url.params["server_id"] == "server-a"
            return httpx.Response(200, json={"id": 1, "shard_path": "/s.jsonl"})
        return httpx.Response(200, json={"id": 1, "status": "succeeded"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport)

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    client = ControlClient("http://control:8080", "server-a")

    async def exercise():
        try:
            claimed = await client.claim_shard("job-1", "server-a")
            updated = await client.update_shard(1, {"status": "succeeded"})
            return claimed, updated
        finally:
            await client.close()

    claimed, updated = asyncio.run(exercise())

    assert claimed == {"id": 1, "shard_path": "/s.jsonl"}
    assert updated == {"id": 1, "status": "succeeded"}
    assert requests[0].url.path == "/api/jobs/job-1/shards/claim"
    assert requests[1].url.path == "/api/shards/1"


def test_control_client_fail_scan_unit_sends_failure_category(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": 7, "status": "failed"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport)

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    client = ControlClient("http://control:8080", "server-a")

    async def exercise():
        try:
            return await client.fail_scan_unit(
                7,
                "scan exploded",
                assigned_server_id="server-a",
                attempt_count=3,
                failure_category="parser_failed",
            )
        finally:
            await client.close()

    response = asyncio.run(exercise())

    assert response == {"id": 7, "status": "failed"}
    assert requests[0].url.path == "/api/scan-units/7/fail"
    assert json.loads(requests[0].content) == {
        "error_message": "scan exploded",
        "assigned_server_id": "server-a",
        "attempt_count": 3,
        "failure_category": "parser_failed",
    }
