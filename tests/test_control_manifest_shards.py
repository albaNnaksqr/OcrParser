from fastapi.testclient import TestClient


from datetime import timedelta


import json


import pytest


from sqlalchemy import event


from ocr_platform.control.app import create_app


from ocr_platform.control.database import create_session_factory, init_db


from ocr_platform.control.domains.model_profiles.commands import (
    ACTIVE_TRANSACTION_ERROR,
    ModelProfileTransactionError,
)


from ocr_platform.control.domains.jobs.commands import create_job


from ocr_platform.control.domains.manifests import (
    use_cases as manifest_use_cases,
)


from ocr_platform.control.domains.model_profiles.commands import (
    upsert_model_profile,
)


from ocr_platform.control.domains.common import POOL_SERVER_ID


from ocr_platform.control.models import (
    Job,
    Manifest,
    ModelProfile,
    ScanUnit,
    Server,
    ShardAttempt,
    WorkShard,
    utcnow,
)


from ocr_platform.control.schemas import JobCreateRequest, ModelProfileRequest


from ocr_platform.manifest.models import ManifestItem


def make_client_with_session(tmp_path, *, raise_server_exceptions=True):
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    app = create_app(session_factory=session_factory)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions), session_factory


def register_server(client):
    return client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )


def test_remote_folder_snapshot_pool_job_claims_only_eligible_server(tmp_path):
    client, _ = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
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
    client.post(
        "/api/servers/register",
        json={"id": "server-b", "name": "Server B", "host": "localhost"},
    )

    create_response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
        },
    )
    assert create_response.status_code == 200

    assert client.post("/api/agents/server-b/next-job").json() is None
    claim_response = client.post("/api/agents/server-a/next-job")

    assert claim_response.status_code == 200
    payload = claim_response.json()
    assert payload["input_mode"] == "remote_folder_snapshot"
    assert payload["status"] == "running"


def test_pool_job_allowed_server_ids_limit_claims(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(2):
        (input_root / f"{index}.pdf").write_bytes(b"%PDF-1.4\n")

    client, _ = make_client_with_session(tmp_path)
    for server_id in ("server-a", "server-b"):
        client.post(
            "/api/servers/register",
            json={"id": server_id, "name": server_id, "host": "localhost"},
        )
        client.post(
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
                            "writable": True,
                        }
                    ]
                },
            },
        )

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
            "allowed_server_ids": ["server-a"],
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert job["assigned_server_id"] is None
    assert job["allowed_server_ids"] == ["server-a"]
    assert client.post("/api/agents/server-b/next-job").json() is None

    claimed = client.post("/api/agents/server-a/next-job").json()
    assert claimed["id"] == job["id"]
    assert claimed["allowed_server_ids"] == ["server-a"]

    blocked_shard = client.post(
        f"/api/jobs/{job['id']}/shards/claim?server_id=server-b"
    )
    assert blocked_shard.status_code == 200
    assert blocked_shard.json() is None


def test_register_remote_manifest_creates_static_shards(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
        },
    )
    job_id = response.json()["id"]

    register_response = client.post(
        f"/api/jobs/{job_id}/manifest",
        json={
            "input_mode": "remote_folder_snapshot",
            "input_root": "/shared/input",
            "manifest_path": "/shared/manifests/job/manifest.jsonl",
            "meta_path": "/shared/manifests/job/manifest.meta.json",
            "file_count": 2,
            "total_bytes": 12,
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": "/shared/manifests/job/shards/shard-000001.jsonl",
                    "file_count": 1,
                },
                {
                    "shard_index": 2,
                    "shard_path": "/shared/manifests/job/shards/shard-000002.jsonl",
                    "file_count": 1,
                },
            ],
        },
    )

    assert register_response.status_code == 200
    with session_factory() as session:
        manifest = session.query(Manifest).filter_by(job_id=job_id).one()
        shards = session.query(WorkShard).filter_by(job_id=job_id).order_by(WorkShard.shard_index).all()
        assert manifest.input_mode == "remote_folder_snapshot"
        assert manifest.file_count == 2
        assert [shard.shard_path for shard in shards] == [
            "/shared/manifests/job/shards/shard-000001.jsonl",
            "/shared/manifests/job/shards/shard-000002.jsonl",
        ]


def test_register_remote_manifest_rejects_duplicate_relative_path_when_manifest_is_readable(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    input_root = tmp_path / "input"
    first = input_root / "a" / "same.pdf"
    second = input_root / "b" / "same.pdf"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"%PDF-1.4\n")
    second.write_bytes(b"%PDF-1.4\n")
    manifest_file = tmp_path / "manifest.jsonl"
    manifest_file.write_text(
        "\n".join(
            [
                ManifestItem(
                    input_path=str(first),
                    relative_path="same.pdf",
                    size_bytes=first.stat().st_size,
                    mtime_ns=first.stat().st_mtime_ns,
                ).to_json_line(),
                ManifestItem(
                    input_path=str(second),
                    relative_path="same.pdf",
                    size_bytes=second.stat().st_size,
                    mtime_ns=second.stat().st_mtime_ns,
                ).to_json_line(),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
        },
    ).json()

    response = client.post(
        f"/api/jobs/{job['id']}/manifest",
        json={
            "input_mode": "remote_folder_snapshot",
            "input_root": str(input_root),
            "manifest_path": str(manifest_file),
            "file_count": 2,
            "total_bytes": first.stat().st_size + second.stat().st_size,
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": str(tmp_path / "shard.jsonl"),
                    "file_count": 2,
                }
            ],
        },
    )

    assert response.status_code == 400
    assert "duplicate relative_path" in response.json()["detail"]
    with session_factory() as session:
        assert session.query(Manifest).filter_by(job_id=job["id"]).count() == 0
        assert session.query(WorkShard).filter_by(job_id=job["id"]).count() == 0


def test_job_summary_includes_worker_shard_distribution(tmp_path):
    client, _ = make_client_with_session(tmp_path)
    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
        },
    )
    job_id = response.json()["id"]
    client.post(
        f"/api/jobs/{job_id}/manifest",
        json={
            "input_mode": "remote_folder_snapshot",
            "input_root": "/shared/input",
            "manifest_path": "/shared/manifests/job/manifest.jsonl",
            "file_count": 3,
            "total_bytes": 12,
            "shards": [
                {"shard_index": 1, "shard_path": "/shared/shard-1.jsonl", "file_count": 1},
                {"shard_index": 2, "shard_path": "/shared/shard-2.jsonl", "file_count": 1},
                {"shard_index": 3, "shard_path": "/shared/shard-3.jsonl", "file_count": 1},
            ],
        },
    )
    for server_id in ("worker-a", "worker-b"):
        client.post(
            f"/api/servers/{server_id}/heartbeat",
            json={
                "status": "idle",
                "capabilities": {
                    "shared_paths": [
                        {
                            "path": "/shared",
                            "exists": True,
                            "is_dir": True,
                            "readable": True,
                        }
                    ]
                },
            },
        )
    shard_a = client.post(f"/api/jobs/{job_id}/shards/claim?server_id=worker-a").json()
    client.post(f"/api/shards/{shard_a['id']}", json={"status": "succeeded", "processed_files": 1})
    shard_b = client.post(f"/api/jobs/{job_id}/shards/claim?server_id=worker-b").json()
    client.post(f"/api/shards/{shard_b['id']}", json={"status": "running", "processed_files": 0})

    summary = client.get(f"/api/jobs/{job_id}/summary").json()

    by_worker = {item["server_id"]: item for item in summary["worker_shards"]}
    assert by_worker["worker-a"]["succeeded_shards"] == 1
    assert by_worker["worker-a"]["current_shards"] == []
    assert by_worker["worker-b"]["running_shards"] == 1
    assert by_worker["worker-b"]["current_shards"][0]["id"] == shard_b["id"]
    assert by_worker["worker-b"]["current_shards"][0]["shard_index"] == 2
    assert by_worker["worker-b"]["current_shards"][0]["lease_status"] == "healthy"
    assert by_worker["worker-b"]["current_shards"][0]["lease_seconds_remaining"] > 0
    assert by_worker[None]["pending_shards"] == 1


def test_failed_shard_update_infers_failure_category_from_error_message(tmp_path):
    client, _ = make_client_with_session(tmp_path)
    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
            "max_shard_attempts": 1,
        },
    )
    job_id = response.json()["id"]
    client.post(
        f"/api/jobs/{job_id}/manifest",
        json={
            "input_mode": "remote_folder_snapshot",
            "input_root": "/shared/input",
            "manifest_path": "/shared/manifests/job/manifest.jsonl",
            "file_count": 1,
            "total_bytes": 12,
            "shards": [
                {"shard_index": 1, "shard_path": "/shared/shard-1.jsonl", "file_count": 1},
            ],
        },
    )
    client.post(
        "/api/servers/worker-a/heartbeat",
        json={
            "status": "idle",
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
    shard = client.post(f"/api/jobs/{job_id}/shards/claim?server_id=worker-a").json()

    failed = client.post(
        f"/api/shards/{shard['id']}",
        json={
            "status": "failed",
            "assigned_server_id": "worker-a",
            "attempt_count": 1,
            "processed_files": 1,
            "failed_files": 1,
            "error_message": "OCR API timed out after 60s",
        },
    )

    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["failure_category"] == "api_timeout"
    attempts = client.get(f"/api/jobs/{job_id}/shards/{shard['id']}/attempts").json()
    assert attempts[0]["failure_category"] == "api_timeout"
    summary = client.get(f"/api/jobs/{job_id}/summary").json()
    assert summary["status"] == "failed"
    assert summary["failure_category"] == "api_timeout"


def test_job_summary_includes_active_failed_and_stale_shard_progress_only(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
        },
    )
    job_id = response.json()["id"]
    client.post(
        f"/api/jobs/{job_id}/manifest",
        json={
            "input_mode": "remote_folder_snapshot",
            "input_root": "/shared/input",
            "manifest_path": "/shared/manifests/job/manifest.jsonl",
            "file_count": 5,
            "total_bytes": 12,
            "shards": [
                {"shard_index": 1, "shard_path": "/shared/shard-1.jsonl", "file_count": 2},
                {"shard_index": 2, "shard_path": "/shared/shard-2.jsonl", "file_count": 1},
                {"shard_index": 3, "shard_path": "/shared/shard-3.jsonl", "file_count": 1},
                {"shard_index": 4, "shard_path": "/shared/shard-4.jsonl", "file_count": 1},
                {"shard_index": 5, "shard_path": "/shared/shard-5.jsonl", "file_count": 0},
            ],
        },
    )

    with session_factory() as session:
        job = session.get(Job, job_id)
        job.max_shard_attempts = 3
        now = utcnow()
        shards = session.query(WorkShard).filter_by(job_id=job_id).order_by(WorkShard.shard_index).all()
        shards[0].status = "running"
        shards[0].assigned_server_id = "worker-a"
        shards[0].started_at = now - timedelta(seconds=10)
        shards[0].lease_expires_at = now + timedelta(seconds=120)
        shards[0].processed_files = 1
        shards[0].completed_pages = 20
        shards[0].api_inflight = 7
        shards[0].api_inflight_peak = 9
        shards[0].api_waiting = 2
        shards[0].oldest_api_inflight = 3.25
        shards[0].execution_paused = True
        shards[0].api_concurrency_limit = 1
        shards[0].execution_control_reason = "memory pressure"
        shards[0].attempt_count = 1
        shards[1].status = "succeeded"
        shards[1].assigned_server_id = "worker-a"
        shards[1].processed_files = 1
        shards[1].completed_pages = 3
        shards[1].attempt_count = 1
        shards[2].status = "stale"
        shards[2].assigned_server_id = "worker-b"
        shards[2].processed_files = 0
        shards[2].completed_pages = 0
        shards[2].attempt_count = 2
        shards[3].status = "failed"
        shards[3].assigned_server_id = "worker-b"
        shards[3].processed_files = 1
        shards[3].failed_files = 1
        shards[3].completed_pages = 0
        shards[3].attempt_count = 3
        shards[3].failure_category = "api_timeout"
        shards[3].error_message = "OCR request timed out"
        session.commit()

    summary = client.get(f"/api/jobs/{job_id}/summary").json()

    assert summary["total_files"] == 5
    assert summary["completed_files"] == 2
    assert summary["failed_files"] == 1
    assert summary["total_shards"] == 5
    assert summary["running_shards"] == 1
    assert summary["succeeded_shards"] == 1
    assert summary["failed_shards"] == 1
    assert summary["stale_shards"] == 1
    assert summary["shard_failure_category_counts"] == {"api_timeout": 1}
    assert [item["shard_index"] for item in summary["attention_shards"]] == [1, 3, 4]
    assert {item["status"] for item in summary["attention_shards"]} == {"running", "stale", "failed"}
    assert all(item["status"] != "succeeded" for item in summary["attention_shards"])
    running = summary["attention_shards"][0]
    assert running["assigned_server_id"] == "worker-a"
    assert running["started_at"] is not None
    assert running["running_seconds"] >= 9
    assert running["file_count"] == 2
    assert running["processed_files"] == 1
    assert running["completed_pages"] == 20
    assert running["lease_status"] == "healthy"
    assert running["lease_seconds_remaining"] > 0
    assert running["attempt_count"] == 1
    assert running["max_attempts"] == 3
    assert running["pages_per_second"] > 0
    assert running["api_inflight"] == 7
    assert running["api_inflight_peak"] == 9
    assert running["api_waiting"] == 2
    assert running["oldest_api_inflight"] == 3.25
    assert running["execution_paused"] is True
    assert running["api_concurrency_limit"] == 1
    assert running["execution_control_reason"] == "memory pressure"
    worker_a = next(item for item in summary["worker_shards"] if item["server_id"] == "worker-a")
    assert [item["shard_index"] for item in worker_a["current_shards"]] == [1]
    assert worker_a["api_inflight"] == 7
    assert worker_a["api_inflight_peak"] == 9
    assert worker_a["api_waiting"] == 2
    assert worker_a["oldest_api_inflight"] == 3.25


def test_job_summary_bounds_attention_shard_details(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
        },
    )
    job_id = response.json()["id"]

    with session_factory() as session:
        job = session.get(Job, job_id)
        job.status = "running"
        manifest = Manifest(
            job_id=job_id,
            input_mode="remote_folder_snapshot",
            input_root="/shared/input",
            manifest_path="/shared/manifests/job/manifest.jsonl",
            file_count=60,
            total_bytes=60,
        )
        session.add(manifest)
        session.flush()
        for index in range(1, 61):
            session.add(
                WorkShard(
                    job_id=job_id,
                    manifest_id=manifest.id,
                    shard_index=index,
                    shard_path=f"/shared/shards/shard-{index:06d}.jsonl",
                    status="running",
                    assigned_server_id="worker-a",
                    file_count=1,
                )
            )
        session.commit()

    summary = client.get(f"/api/jobs/{job_id}/summary").json()

    assert summary["total_shards"] == 60
    assert summary["running_shards"] == 60
    assert len(summary["attention_shards"]) == 50
    assert [item["shard_index"] for item in summary["attention_shards"]] == list(range(1, 51))
    worker_a = next(item for item in summary["worker_shards"] if item["server_id"] == "worker-a")
    assert worker_a["running_shards"] == 60
    assert len(worker_a["current_shards"]) == 50


def test_list_shard_attempts_route_is_bounded_by_default(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
        },
    )
    job_id = response.json()["id"]

    with session_factory() as session:
        manifest = Manifest(
            job_id=job_id,
            input_mode="remote_folder_snapshot",
            input_root="/shared/input",
            manifest_path="/shared/manifests/job/manifest.jsonl",
            file_count=1,
            total_bytes=1,
        )
        session.add(manifest)
        session.flush()
        shard = WorkShard(
            job_id=job_id,
            manifest_id=manifest.id,
            shard_index=1,
            shard_path="/shared/shards/shard-000001.jsonl",
            status="failed",
            file_count=1,
        )
        session.add(shard)
        session.flush()
        for attempt_number in range(1, 121):
            session.add(
                ShardAttempt(
                    job_id=job_id,
                    shard_id=shard.id,
                    attempt_number=attempt_number,
                    server_id="worker-a",
                    status="failed",
                )
            )
        session.commit()
        shard_id = shard.id

    attempts = client.get(f"/api/jobs/{job_id}/shards/{shard_id}/attempts").json()

    assert len(attempts) == 100
    assert attempts[0]["attempt_number"] == 1
    assert attempts[-1]["attempt_number"] == 100

    page = client.get(f"/api/jobs/{job_id}/shards/{shard_id}/attempts?limit=10&offset=100").json()
    assert len(page) == 10
    assert page[0]["attempt_number"] == 101
    assert page[-1]["attempt_number"] == 110

    paged = client.get(f"/api/jobs/{job_id}/shards/{shard_id}/attempts/page?limit=10&offset=100").json()
    assert paged["total"] == 120
    assert paged["limit"] == 10
    assert paged["offset"] == 100
    assert paged["has_more"] is True
    assert [item["attempt_number"] for item in paged["items"]] == list(range(101, 111))


def test_create_job_with_existing_manifest_requires_manifest_path(tmp_path):
    client, _ = make_client_with_session(tmp_path)
    register_server(client)

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": str(tmp_path / "input"),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "existing_manifest",
        },
    )

    assert response.status_code == 400
    assert "manifest_path is required" in response.json()["detail"]


def test_create_job_with_existing_manifest_snapshots_manifest_and_shards(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    pdf = input_root / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    manifest_file = tmp_path / "source-manifest.jsonl"
    manifest_file.write_text(
        ManifestItem(
            input_path=str(pdf),
            relative_path="a.pdf",
            size_bytes=pdf.stat().st_size,
            mtime_ns=pdf.stat().st_mtime_ns,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )

    client, session_factory = make_client_with_session(tmp_path)
    register_server(client)

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "existing_manifest",
            "manifest_path": str(manifest_file),
            "target_files_per_shard": 1000,
        },
    )

    assert response.status_code == 200
    job_id = response.json()["id"]
    with session_factory() as session:
        manifest = session.query(Manifest).filter_by(job_id=job_id).one()
        shard = session.query(WorkShard).filter_by(job_id=job_id).one()
        assert manifest.input_mode == "existing_manifest"
        assert manifest.file_count == 1
        assert manifest.manifest_path.endswith("manifest.jsonl")
        assert manifest.frozen_at is not None
        assert json.loads(manifest.freeze_report_json)["integrity_ok"] is True
        assert shard.file_count == 1


@pytest.mark.parametrize("input_mode", ["folder_snapshot", "existing_manifest"])
def test_failed_manifest_job_creation_rolls_back_flushed_job(tmp_path, input_mode):
    input_root = tmp_path / "input"
    input_root.mkdir()
    pdf = input_root / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    manifest_file = tmp_path / "source-manifest.jsonl"
    manifest_file.write_text(
        ManifestItem(
            input_path=str(pdf),
            relative_path="a.pdf",
            size_bytes=pdf.stat().st_size,
            mtime_ns=pdf.stat().st_mtime_ns,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)

    with session_factory() as session:
        session.add(Server(id="server-a", name="Server A", host="localhost"))
        session.commit()
        request = JobCreateRequest(
            input_dir=str(input_root),
            output_dir=str(tmp_path / "output"),
            engine="dotsocr",
            assigned_server_id="server-a",
            input_mode=input_mode,
            manifest_path=str(manifest_file) if input_mode == "existing_manifest" else None,
            target_files_per_shard=0,
        )

        with pytest.raises(ValueError, match="target_files_per_shard"):
            create_job(session, request)
        session.commit()

        assert session.query(Job).count() == 0
        assert session.query(Manifest).count() == 0
        assert session.query(WorkShard).count() == 0


@pytest.mark.parametrize(
    ("content", "expected_detail"),
    [
        ("not json\n", "line 1"),
        ('{"input_path": "/tmp/a.pdf"}\n', "line 1"),
    ],
)
def test_create_job_with_malformed_existing_manifest_returns_400(tmp_path, content, expected_detail):
    manifest_file = tmp_path / "source-manifest.jsonl"
    manifest_file.write_text(content, encoding="utf-8")
    client, _ = make_client_with_session(tmp_path, raise_server_exceptions=False)
    register_server(client)

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": str(tmp_path / "input"),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "existing_manifest",
            "manifest_path": str(manifest_file),
        },
    )

    assert response.status_code == 400
    assert expected_detail in response.json()["detail"]


def test_job_summary_includes_shard_counts(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(3):
        (input_root / f"{index}.pdf").write_bytes(b"%PDF-1.4\n")

    client, session_factory = make_client_with_session(tmp_path)
    register_server(client)
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 2,
        },
    ).json()

    with session_factory() as session:
        shards = session.query(WorkShard).filter_by(job_id=job["id"]).order_by(WorkShard.shard_index).all()
        shards[0].status = "running"
        shards[1].status = "succeeded"
        session.commit()

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["total_files"] == 3
    assert summary["scanned_files"] == 3
    assert summary["completed_files"] == 0
    assert summary["progress_percent"] == 0.0
    assert summary["total_shards"] == 2
    assert summary["shards_created"] == 2
    assert summary["executable_shards"] == 1
    assert summary["pending_shards"] == 0
    assert summary["running_shards"] == 1
    assert summary["succeeded_shards"] == 1
    assert summary["failed_shards"] == 0
    assert summary["stopped_shards"] == 0


def test_static_sharded_job_summary_uses_shard_progress_counters(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(3):
        (input_root / f"{index}.pdf").write_bytes(b"%PDF-1.4\n")

    client, session_factory = make_client_with_session(tmp_path)
    register_server(client)
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 2,
        },
    ).json()

    with session_factory() as session:
        shards = session.query(WorkShard).filter_by(job_id=job["id"]).order_by(WorkShard.shard_index).all()
        shards[0].status = "succeeded"
        shards[0].processed_files = 2
        shards[0].completed_pages = 4
        shards[1].status = "running"
        shards[1].processed_files = 0
        session.commit()

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["total_files"] == 3
    assert summary["completed_files"] == 2
    assert summary["completed_pages"] == 4
    assert summary["progress_percent"] == 66.67


def test_list_work_shards_route_returns_shards_ordered_by_index(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(3):
        (input_root / f"{index}.pdf").write_bytes(b"%PDF-1.4\n")

    client, _ = make_client_with_session(tmp_path)
    register_server(client)
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
        },
    ).json()

    response = client.get(f"/api/jobs/{job['id']}/shards")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["limit"] == 100
    assert payload["offset"] == 0
    assert payload["has_more"] is False
    shard_rows = payload["items"]
    assert [row["shard_index"] for row in shard_rows] == [1, 2, 3]
    assert [row["status"] for row in shard_rows] == ["pending", "pending", "pending"]
    assert all(row["job_id"] == job["id"] for row in shard_rows)


def test_list_work_shards_route_paginates_and_filters_attention_statuses(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(6):
        (input_root / f"{index}.pdf").write_bytes(b"%PDF-1.4\n")

    client, session_factory = make_client_with_session(tmp_path)
    register_server(client)
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
        },
    ).json()

    with session_factory() as session:
        shards = session.query(WorkShard).filter_by(job_id=job["id"]).order_by(WorkShard.shard_index).all()
        statuses = ["succeeded", "running", "failed", "stale", "retrying", "pending"]
        for shard, status in zip(shards, statuses):
            shard.status = status
        session.commit()

    response = client.get(f"/api/jobs/{job['id']}/shards?status=attention&limit=2&offset=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 4
    assert payload["limit"] == 2
    assert payload["offset"] == 1
    assert payload["has_more"] is True
    assert [row["status"] for row in payload["items"]] == ["failed", "stale"]
    assert [row["shard_index"] for row in payload["items"]] == [3, 4]


def test_list_work_shards_route_rejects_unknown_status_filter(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "sample.pdf").write_bytes(b"%PDF-1.4\n")

    client, _ = make_client_with_session(tmp_path)
    register_server(client)
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
        },
    ).json()

    response = client.get(f"/api/jobs/{job['id']}/shards?status=runningg")

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "unknown shard status filter" in detail
    assert "attention" in detail
    assert "running" in detail


def test_list_work_shards_route_filters_worker_attempts_and_long_running(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(4):
        (input_root / f"{index}.pdf").write_bytes(b"%PDF-1.4\n")

    client, session_factory = make_client_with_session(tmp_path)
    register_server(client)
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
        },
    ).json()

    with session_factory() as session:
        shards = session.query(WorkShard).filter_by(job_id=job["id"]).order_by(WorkShard.shard_index).all()
        for shard in shards:
            shard.status = "running"
            shard.assigned_server_id = "server-a"
            shard.started_at = utcnow()
        shards[0].started_at = utcnow() - timedelta(seconds=7200)
        shards[0].attempt_count = 2
        shards[1].started_at = utcnow() - timedelta(seconds=7200)
        shards[1].assigned_server_id = "server-b"
        shards[1].attempt_count = 2
        shards[2].started_at = utcnow() - timedelta(seconds=60)
        shards[2].attempt_count = 3
        session.commit()

    response = client.get(
        f"/api/jobs/{job['id']}/shards"
        "?status=running&worker_id=server-a&min_attempt_count=2&running_longer_than_seconds=3600"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert [row["shard_index"] for row in payload["items"]] == [1]


def test_list_work_shards_route_filters_failure_category(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(4):
        (input_root / f"{index}.pdf").write_bytes(b"%PDF-1.4\n")

    client, session_factory = make_client_with_session(tmp_path)
    register_server(client)
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
        },
    ).json()

    with session_factory() as session:
        shards = session.query(WorkShard).filter_by(job_id=job["id"]).order_by(WorkShard.shard_index).all()
        shards[0].status = "failed"
        shards[0].failure_category = "api_timeout"
        shards[1].status = "failed"
        shards[1].failure_category = "output_unwritable"
        shards[2].status = "retrying"
        shards[2].failure_category = "api_timeout"
        shards[3].status = "succeeded"
        shards[3].failure_category = "api_timeout"
        session.commit()

    response = client.get(
        f"/api/jobs/{job['id']}/shards?status=attention&failure_category=api_timeout"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [row["shard_index"] for row in payload["items"]] == [1, 3]
    assert {row["failure_category"] for row in payload["items"]} == {"api_timeout"}


def test_list_work_shards_route_returns_404_for_unknown_job(tmp_path):
    client, _ = make_client_with_session(tmp_path)

    response = client.get("/api/jobs/missing/shards")

    assert response.status_code == 404
    assert "unknown job" in response.json()["detail"]
