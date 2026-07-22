from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi.testclient import TestClient

from ocr_platform.control.app import create_app
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.models import ScanUnit, ShardAttempt, WorkShard, utcnow
from ocr_platform.control import service
from sqlalchemy.dialects import postgresql


def make_client_with_session(tmp_path):
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    app = create_app(session_factory=session_factory)
    return TestClient(app), session_factory


def heartbeat_worker(client, server_id, *, status="idle", current_job_id=None):
    return client.post(
        f"/api/servers/{server_id}/heartbeat",
        json={
            "status": status,
            "current_job_id": current_job_id,
            "capabilities": {
                "shared_paths": [
                    {
                        "path": "/shared",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ]
            },
        },
    )


def create_remote_shard_job(client, *, max_shard_attempts=3):
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
            "max_shard_attempts": max_shard_attempts,
        },
    ).json()
    client.post(
        f"/api/jobs/{job['id']}/manifest",
        json={
            "input_mode": "remote_folder_snapshot",
            "input_root": "/shared/input",
            "manifest_path": "/shared/manifests/job/manifest.jsonl",
            "file_count": 1,
            "total_bytes": 12,
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": "/shared/manifests/job/shards/shard-000001.jsonl",
                    "file_count": 1,
                }
            ],
        },
    )
    return job


def reregister_worker(client, server_id):
    return client.post(
        "/api/servers/register",
        json={
            "id": server_id,
            "name": server_id,
            "host": "localhost",
        },
    )


def test_expired_running_shard_becomes_stale_and_can_be_reclaimed(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    heartbeat_worker(client, "worker-a")
    heartbeat_worker(client, "worker-b")
    job = create_remote_shard_job(client)
    job_id = job["id"]
    assert client.post("/api/agents/worker-a/next-job").json()["id"] == job_id
    first_claim = client.post(f"/api/jobs/{job_id}/shards/claim?server_id=worker-a").json()
    assert first_claim["attempt_count"] == 1

    with session_factory() as session:
        shard = session.get(WorkShard, first_claim["id"])
        shard.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    summary = client.get(f"/api/jobs/{job_id}/summary").json()
    assert summary["recovery_status"] == "recovering"
    assert summary["stale_shards"] == 1
    assert summary["running_shards"] == 0

    second_claim = client.post(f"/api/jobs/{job_id}/shards/claim?server_id=worker-b").json()

    assert second_claim["id"] == first_claim["id"]
    assert second_claim["assigned_server_id"] == "worker-b"
    assert second_claim["attempt_count"] == 2
    assert second_claim["status"] == "running"


def test_late_shard_update_from_reclaimed_attempt_is_rejected(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    heartbeat_worker(client, "worker-a")
    heartbeat_worker(client, "worker-b")
    job = create_remote_shard_job(client)
    job_id = job["id"]
    client.post("/api/agents/worker-a/next-job")
    first_claim = client.post(f"/api/jobs/{job_id}/shards/claim?server_id=worker-a").json()
    with session_factory() as session:
        shard = session.get(WorkShard, first_claim["id"])
        shard.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    second_claim = client.post(f"/api/jobs/{job_id}/shards/claim?server_id=worker-b").json()
    assert second_claim["id"] == first_claim["id"]
    assert second_claim["attempt_count"] == 2

    response = client.post(
        f"/api/shards/{first_claim['id']}",
        json={
            "assigned_server_id": "worker-a",
            "attempt_count": 1,
            "status": "succeeded",
            "processed_files": 1,
            "completed_pages": 10,
        },
    )

    assert response.status_code == 409
    with session_factory() as session:
        shard = session.get(WorkShard, first_claim["id"])
        assert shard.status == "running"
        assert shard.assigned_server_id == "worker-b"
        assert shard.attempt_count == 2
        assert shard.processed_files == 0
        assert shard.completed_pages == 0


def test_same_server_restart_reclaims_orphan_before_pending_shard(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    heartbeat_worker(client, "worker-a")
    job = create_remote_shard_job(client)
    job_id = job["id"]
    client.post("/api/agents/worker-a/next-job")
    old_claim = client.post(
        f"/api/jobs/{job_id}/shards/claim?server_id=worker-a"
    ).json()
    with session_factory() as session:
        orphan = session.get(WorkShard, old_claim["id"])
        orphan.shard_index = 2
        orphan.shard_path = "/shared/manifests/job/shards/shard-000002.jsonl"
        session.flush()
        session.add(
            WorkShard(
                job_id=orphan.job_id,
                manifest_id=orphan.manifest_id,
                shard_index=1,
                shard_path="/shared/manifests/job/shards/shard-000001.jsonl",
                status="pending",
                file_count=1,
            )
        )
        session.commit()

    response = reregister_worker(client, "worker-a")

    assert response.status_code == 200
    heartbeat_worker(
        client,
        "worker-a",
        status="busy",
        current_job_id=job_id,
    )
    with session_factory() as session:
        orphan = session.get(WorkShard, old_claim["id"])
        attempt = session.query(ShardAttempt).filter_by(shard_id=orphan.id).one()
        assert orphan.status == "stale"
        assert orphan.assigned_server_id is None
        assert orphan.lease_expires_at is None
        assert attempt.status == "stale"
        assert attempt.finished_at is not None

    reclaimed = client.post(
        f"/api/jobs/{job_id}/shards/claim?server_id=worker-a"
    ).json()

    assert reclaimed["id"] == old_claim["id"]
    assert reclaimed["attempt_count"] == 2
    assert reclaimed["assigned_server_id"] == "worker-a"

    late = client.post(
        f"/api/shards/{old_claim['id']}",
        json={
            "status": "succeeded",
            "assigned_server_id": "worker-a",
            "attempt_count": 1,
            "processed_files": 1,
        },
    )
    assert late.status_code == 409

    current_payload = {
        "status": "succeeded",
        "assigned_server_id": "worker-a",
        "attempt_count": 2,
        "processed_files": 1,
    }
    assert client.post(f"/api/shards/{reclaimed['id']}", json=current_payload).status_code == 200
    repeated = client.post(f"/api/shards/{reclaimed['id']}", json=current_payload)
    regressive = client.post(
        f"/api/shards/{reclaimed['id']}",
        json=current_payload | {"status": "running", "processed_files": 0},
    )
    assert repeated.json()["status"] == "succeeded"
    assert regressive.json()["status"] == "succeeded"
    assert regressive.json()["processed_files"] == 1


def test_same_server_restart_reclaims_only_orphan_without_pending_work(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    heartbeat_worker(client, "worker-a")
    job = create_remote_shard_job(client)
    job_id = job["id"]
    client.post("/api/agents/worker-a/next-job")
    old_claim = client.post(
        f"/api/jobs/{job_id}/shards/claim?server_id=worker-a"
    ).json()

    assert reregister_worker(client, "worker-a").status_code == 200
    assert reregister_worker(client, "worker-a").status_code == 200
    with session_factory() as session:
        fenced = session.get(WorkShard, old_claim["id"])
        assert fenced.status == "stale"
        assert fenced.attempt_count == 1
        assert session.query(ShardAttempt).filter_by(shard_id=fenced.id).count() == 1
    heartbeat_worker(client, "worker-a")
    reclaimed = client.post(
        f"/api/jobs/{job_id}/shards/claim?server_id=worker-a"
    ).json()

    assert reclaimed["id"] == old_claim["id"]
    assert reclaimed["attempt_count"] == 2
    with session_factory() as session:
        attempts = (
            session.query(ShardAttempt)
            .filter_by(shard_id=old_claim["id"])
            .order_by(ShardAttempt.attempt_number)
            .all()
        )
        assert [attempt.status for attempt in attempts] == ["stale", "running"]
        assert [attempt.attempt_number for attempt in attempts] == [1, 2]


def test_same_server_restart_prioritizes_orphaned_scan_unit(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    heartbeat_worker(client, "worker-a")
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
        },
    ).json()
    with session_factory() as session:
        pending = session.query(ScanUnit).filter_by(job_id=job["id"]).one()
        session.add(
            ScanUnit(
                job_id=job["id"],
                path="/shared/input/orphan",
                status="running",
                assigned_server_id="worker-a",
                attempt_count=1,
                started_at=utcnow(),
                lease_expires_at=utcnow() + timedelta(seconds=120),
            )
        )
        session.commit()
        pending_id = pending.id
        orphan_id = session.query(ScanUnit).filter_by(path="/shared/input/orphan").one().id

    assert reregister_worker(client, "worker-a").status_code == 200
    heartbeat_worker(client, "worker-a")
    reclaimed = client.post("/api/scan-units/claim?server_id=worker-a").json()

    assert reclaimed["id"] == orphan_id
    assert reclaimed["id"] != pending_id
    assert reclaimed["attempt_count"] == 2
    late = client.post(
        f"/api/scan-units/{orphan_id}/complete",
        json={
            "assigned_server_id": "worker-a",
            "attempt_count": 1,
            "manifest_path": "/shared/manifests/late.jsonl",
            "file_count": 0,
            "total_bytes": 0,
            "child_paths": [],
            "shards": [],
        },
    )
    assert late.status_code == 409


def test_concurrent_shard_claims_do_not_duplicate_recovery_attempt(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    heartbeat_worker(client, "worker-a")
    heartbeat_worker(client, "worker-b")
    job = create_remote_shard_job(client)
    job_id = job["id"]
    client.post("/api/agents/worker-a/next-job")
    old_claim = client.post(
        f"/api/jobs/{job_id}/shards/claim?server_id=worker-a"
    ).json()
    assert reregister_worker(client, "worker-a").status_code == 200
    heartbeat_worker(client, "worker-a")

    def claim(server_id):
        response = client.post(
            f"/api/jobs/{job_id}/shards/claim?server_id={server_id}"
        )
        assert response.status_code == 200
        return response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("worker-a", "worker-b")))

    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1
    assert claimed[0]["id"] == old_claim["id"]
    assert claimed[0]["attempt_count"] == 2
    with session_factory() as session:
        attempts = session.query(ShardAttempt).filter_by(shard_id=old_claim["id"]).all()
        assert sorted(attempt.attempt_number for attempt in attempts) == [1, 2]


def test_running_shard_execution_control_update_preserves_progress_counters(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    heartbeat_worker(client, "worker-a")
    job = create_remote_shard_job(client)
    job_id = job["id"]
    client.post("/api/agents/worker-a/next-job")
    shard = client.post(f"/api/jobs/{job_id}/shards/claim?server_id=worker-a").json()
    client.post(
        f"/api/shards/{shard['id']}",
        json={
            "status": "running",
            "assigned_server_id": "worker-a",
            "attempt_count": shard["attempt_count"],
            "processed_files": 4,
            "failed_files": 1,
            "skipped_files": 1,
            "completed_pages": 12,
        },
    )

    response = client.post(
        f"/api/shards/{shard['id']}",
        json={
            "status": "running",
            "assigned_server_id": "worker-a",
            "attempt_count": shard["attempt_count"],
            "execution_paused": True,
            "api_concurrency_limit": 1,
            "execution_control_reason": "memory pressure",
        },
    )

    assert response.status_code == 200
    with session_factory() as session:
        stored = session.get(WorkShard, shard["id"])
        assert stored.processed_files == 4
        assert stored.failed_files == 1
        assert stored.skipped_files == 1
        assert stored.completed_pages == 12
        assert stored.execution_paused is True
        assert stored.api_concurrency_limit == 1
        assert stored.execution_control_reason == "memory pressure"


def test_running_shard_execution_control_update_is_recorded_on_attempt(tmp_path):
    client, _ = make_client_with_session(tmp_path)
    heartbeat_worker(client, "worker-a")
    job = create_remote_shard_job(client)
    job_id = job["id"]
    client.post("/api/agents/worker-a/next-job")
    shard = client.post(f"/api/jobs/{job_id}/shards/claim?server_id=worker-a").json()

    response = client.post(
        f"/api/shards/{shard['id']}",
        json={
            "status": "running",
            "assigned_server_id": "worker-a",
            "attempt_count": shard["attempt_count"],
            "execution_paused": True,
            "api_concurrency_limit": 1,
            "execution_control_reason": "memory pressure",
        },
    )
    attempts = client.get(f"/api/jobs/{job_id}/shards/{shard['id']}/attempts").json()

    assert response.status_code == 200
    assert attempts[0]["execution_paused"] is True
    assert attempts[0]["api_concurrency_limit"] == 1
    assert attempts[0]["execution_control_reason"] == "memory pressure"


def test_failed_shard_retries_until_max_attempts_then_marks_failed(tmp_path):
    client, _ = make_client_with_session(tmp_path)
    heartbeat_worker(client, "worker-a")
    heartbeat_worker(client, "worker-b")
    job = create_remote_shard_job(client, max_shard_attempts=2)
    job_id = job["id"]
    assert client.post("/api/agents/worker-a/next-job").json()["id"] == job_id

    first_claim = client.post(f"/api/jobs/{job_id}/shards/claim?server_id=worker-a").json()
    retryable = client.post(
        f"/api/shards/{first_claim['id']}",
        json={
            "status": "failed",
            "processed_files": 0,
            "failure_category": "model_error",
            "error_message": "transient OCR failure",
        },
    ).json()

    assert retryable["status"] == "retrying"
    assert retryable["attempt_count"] == 1
    summary = client.get(f"/api/jobs/{job_id}/summary").json()
    assert summary["recovery_status"] == "recovering"
    assert summary["retrying_shards"] == 1
    assert summary["failed_shards"] == 0

    second_claim = client.post(f"/api/jobs/{job_id}/shards/claim?server_id=worker-b").json()
    assert second_claim["attempt_count"] == 2
    exhausted = client.post(
        f"/api/shards/{second_claim['id']}",
        json={
            "status": "failed",
            "processed_files": 0,
            "failure_category": "model_error",
            "error_message": "permanent OCR failure",
        },
    ).json()

    assert exhausted["status"] == "failed"
    summary = client.get(f"/api/jobs/{job_id}/summary").json()
    assert summary["recovery_status"] == "exhausted"
    assert summary["retrying_shards"] == 0
    assert summary["failed_shards"] == 1
    assert summary["status"] == "failed"


def test_shard_attempts_preserve_retry_evidence(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    heartbeat_worker(client, "worker-a")
    heartbeat_worker(client, "worker-b")
    job = create_remote_shard_job(client, max_shard_attempts=2)
    job_id = job["id"]
    client.post("/api/agents/worker-a/next-job")

    first_claim = client.post(f"/api/jobs/{job_id}/shards/claim?server_id=worker-a").json()
    client.post(
        f"/api/shards/{first_claim['id']}",
        json={
            "status": "failed",
            "processed_files": 0,
            "completed_pages": 3,
            "failure_category": "model_timeout",
            "error_message": "timeout on page 4",
        },
    )
    second_claim = client.post(f"/api/jobs/{job_id}/shards/claim?server_id=worker-b").json()
    client.post(
        f"/api/shards/{second_claim['id']}",
        json={
            "status": "failed",
            "processed_files": 0,
            "completed_pages": 5,
            "failure_category": "model_error",
            "error_message": "bad response",
        },
    )

    response = client.get(f"/api/jobs/{job_id}/shards/{first_claim['id']}/attempts")

    assert response.status_code == 200
    attempts = response.json()
    assert [attempt["attempt_number"] for attempt in attempts] == [1, 2]
    assert [attempt["server_id"] for attempt in attempts] == ["worker-a", "worker-b"]
    assert [attempt["status"] for attempt in attempts] == ["retrying", "failed"]
    assert attempts[0]["failure_category"] == "model_timeout"
    assert attempts[0]["error_message"] == "timeout on page 4"
    assert attempts[0]["completed_pages"] == 3
    assert attempts[1]["failure_category"] == "model_error"

    with session_factory() as session:
        stored = session.query(ShardAttempt).order_by(ShardAttempt.attempt_number).all()
        assert len(stored) == 2
        assert stored[0].finished_at is not None
        assert stored[1].finished_at is not None


def test_claimable_shard_select_uses_postgresql_skip_locked():
    statement = service._claimable_shard_id_select("job-1")

    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FOR UPDATE SKIP LOCKED" in compiled


def test_claimable_scan_unit_select_uses_postgresql_skip_locked_with_limit():
    statement = service._claimable_scan_unit_id_select(limit=25)

    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FOR UPDATE SKIP LOCKED" in compiled
    assert "LIMIT 25" in compiled


def test_manifest_completion_select_uses_postgresql_for_update():
    statement = service._manifest_for_scan_unit_completion_select("job-1")

    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FOR UPDATE" in compiled
    assert "manifests.job_id = 'job-1'" in compiled
    assert "LIMIT 1" in compiled
