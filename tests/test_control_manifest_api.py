from fastapi.testclient import TestClient
from datetime import timedelta
import json
import pytest
from sqlalchemy import event

from ocr_platform.control.app import create_app
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.models import Job, Manifest, ScanUnit, Server, ShardAttempt, WorkShard, utcnow
from ocr_platform.control.schemas import JobCreateRequest, ModelProfileRequest
from ocr_platform.control.service import POOL_SERVER_ID, create_job, upsert_model_profile
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


def test_model_profile_rejects_unknown_parser_extra_arg(tmp_path):
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)

    with session_factory() as session:
        with pytest.raises(ValueError, match="unknown model profile extra_args key"):
            upsert_model_profile(
                session,
                "bad",
                ModelProfileRequest(
                    label="Bad",
                    engine="dotsocr",
                    extra_args={"not_a_parser_option": True},
                ),
            )


def test_create_job_rejects_invalid_parser_extra_arg_value(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)

    with session_factory() as session:
        session.add(Server(id="server-a", name="Server A", host="localhost"))
        session.commit()
        with pytest.raises(ValueError, match="file_concurrency"):
            create_job(
                session,
                JobCreateRequest(
                    input_dir=str(input_root),
                    output_dir=str(tmp_path / "output"),
                    engine="dotsocr",
                    assigned_server_id="server-a",
                    extra_args={"file_concurrency": "many"},
                ),
            )


def test_create_job_with_folder_snapshot_creates_manifest_and_shards(tmp_path):
    input_root = tmp_path / "input"
    nested = input_root / "nested"
    nested.mkdir(parents=True)
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (nested / "b.pdf").write_bytes(b"%PDF-1.4\n")
    manifest_root = tmp_path / "manifests"

    client, session_factory = make_client_with_session(tmp_path)
    register_server(client)

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "folder_snapshot",
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    )

    assert response.status_code == 200
    job_id = response.json()["id"]
    with session_factory() as session:
        manifest = session.query(Manifest).filter_by(job_id=job_id).one()
        shards = session.query(WorkShard).filter_by(job_id=job_id).order_by(WorkShard.shard_index).all()
        assert manifest.file_count == 2
        assert manifest.input_mode == "folder_snapshot"
        assert manifest.manifest_path.endswith("manifest.jsonl")
        assert manifest.manifest_path.startswith(str(manifest_root / job_id))
        assert manifest.frozen_at is not None
        assert manifest.freeze_report_json is not None
        freeze_report = json.loads(manifest.freeze_report_json)
        assert freeze_report["frozen"] is True
        assert freeze_report["integrity_ok"] is True
        assert freeze_report["shard_file_count_matches_manifest"] is True
        assert len(shards) == 2
        assert [shard.file_count for shard in shards] == [1, 1]
    freeze_response = client.get(f"/api/jobs/{job_id}/manifest/freeze-report").json()
    assert freeze_response["report"]["frozen"] is True
    assert freeze_response["report"]["integrity_ok"] is True
    summary = client.get(f"/api/jobs/{job_id}/summary").json()
    assert summary["manifest_integrity_ok"] is True
    assert summary["manifest_integrity_status"] == "ok"


def test_create_folder_snapshot_job_records_bounded_scan_error_samples(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(8):
        (input_root / f"bad-{index}.pdf").write_bytes(b"%PDF-1.4\n")

    from ocr_platform.manifest import scanner

    def stat_fails(path):
        raise PermissionError(f"cannot stat {path.name}")

    monkeypatch.setattr(scanner, "_stat_manifest_file", stat_fails)
    manifest_root = tmp_path / "manifests"
    client, _ = make_client_with_session(tmp_path)
    register_server(client)

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "folder_snapshot",
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    )

    assert response.status_code == 200
    meta_path = manifest_root / response.json()["id"] / "manifest.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["file_count"] == 0
    assert meta["skipped_error_count"] == 8
    assert len(meta["skipped_errors"]) == 5
    assert meta["skipped_errors"][0]["failure_category"] == "input_invalid"
    freeze = client.get(f"/api/jobs/{response.json()['id']}/manifest/freeze-report").json()
    assert freeze["report"]["frozen"] is True
    assert freeze["report"]["scan_error_count"] == 8
    assert len(freeze["report"]["scan_error_samples"]) == 5
    assert freeze["report"]["scan_error_samples"][0]["failure_category"] == "input_invalid"


def test_folder_snapshot_job_summary_uses_manifest_scan_error_metadata(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    nested = input_root / "nested"
    nested.mkdir(parents=True)
    for index in range(8):
        (input_root / f"bad-{index}.pdf").write_bytes(b"%PDF-1.4\n")
    (nested / "note.txt").write_text("ignore me")

    from ocr_platform.manifest import scanner

    def stat_fails(path):
        raise PermissionError(f"cannot stat {path.name}")

    monkeypatch.setattr(scanner, "_stat_manifest_file", stat_fails)
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

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["scan_status"] == "done"
    assert summary["scan_progress_dirs"] == 2
    assert summary["scan_error_count"] == 8
    assert len(summary["scan_error_samples"]) == 5
    assert summary["scan_error_samples"][0]["failure_category"] == "input_invalid"


def test_create_folder_snapshot_job_without_assigned_server_creates_pool_job(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    client, session_factory = make_client_with_session(tmp_path)

    response = client.post(
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

    assert response.status_code == 200
    payload = response.json()
    assert payload["assigned_server_id"] is None
    assert payload["has_static_shards"] is True
    with session_factory() as session:
        job = session.get(Job, payload["id"])
        assert job.assigned_server_id == POOL_SERVER_ID
        assert session.get(Server, POOL_SERVER_ID) is not None


def test_create_remote_folder_snapshot_does_not_scan_on_control_host(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/not-mounted-on-control",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
            "target_files_per_shard": 1,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["assigned_server_id"] is None
    assert payload["input_mode"] == "remote_folder_snapshot"
    assert payload["has_static_shards"] is False
    with session_factory() as session:
        job = session.get(Job, payload["id"])
        assert job.assigned_server_id == POOL_SERVER_ID
        assert job.input_mode == "remote_folder_snapshot"
        assert job.manifest_root == "/shared/manifests"
        assert job.target_files_per_shard == 1
        assert session.query(Manifest).filter_by(job_id=job.id).count() == 0
        assert session.query(WorkShard).filter_by(job_id=job.id).count() == 0


def test_create_distributed_manifest_scan_job_seeds_root_scan_unit(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
            "target_files_per_shard": 1000,
        },
    )

    assert response.status_code == 200
    job_id = response.json()["id"]
    with session_factory() as session:
        job = session.get(Job, job_id)
        manifest = session.query(Manifest).filter_by(job_id=job_id).one()
        unit = session.query(ScanUnit).filter_by(job_id=job_id).one()
        assert job.assigned_server_id == POOL_SERVER_ID
        assert manifest.status == "scanning"
        assert manifest.manifest_path == "/shared/manifests/" + job_id + "/manifest.jsonl"
        assert unit.path == "/shared/input"
        assert unit.status == "pending"


def test_distributed_scan_unit_claim_and_complete_adds_children_and_shards(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
            "target_files_per_shard": 1000,
        },
    ).json()

    claimed = client.post("/api/scan-units/claim?server_id=server-a").json()

    assert claimed["job_id"] == job["id"]
    assert claimed["path"] == "/shared/input"

    response = client.post(
        f"/api/scan-units/{claimed['id']}/complete",
        json={
            "manifest_path": "/shared/manifests/job/scan-units/1/manifest.jsonl",
            "meta_path": "/shared/manifests/job/scan-units/1/manifest.meta.json",
            "file_count": 2,
            "total_bytes": 30,
            "child_paths": ["/shared/input/a", "/shared/input/b"],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": "/shared/manifests/job/scan-units/1/shards/shard-000001.jsonl",
                    "file_count": 2,
                }
            ],
        },
    )

    assert response.status_code == 200
    with session_factory() as session:
        units = session.query(ScanUnit).filter_by(job_id=job["id"]).order_by(ScanUnit.id).all()
        manifest = session.query(Manifest).filter_by(job_id=job["id"]).one()
        assert [unit.status for unit in units] == ["succeeded", "pending", "pending"]
        assert [unit.path for unit in units[1:]] == ["/shared/input/a", "/shared/input/b"]
        assert session.query(WorkShard).filter_by(job_id=job["id"]).count() == 1
        assert manifest.status == "scanning"
        assert manifest.file_count == 2
        assert manifest.next_shard_index == 2


def test_distributed_scan_unit_completion_skips_existing_child_paths(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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

    root = client.post("/api/scan-units/claim?server_id=server-a").json()
    first = client.post(
        f"/api/scan-units/{root['id']}/complete",
        json={
            "manifest_path": "/shared/manifests/job/root/manifest.jsonl",
            "meta_path": "/shared/manifests/job/root/manifest.meta.json",
            "file_count": 0,
            "total_bytes": 0,
            "child_paths": ["/shared/input/a", "/shared/input/b"],
            "shards": [],
        },
    )
    assert first.status_code == 200
    child = client.post("/api/scan-units/claim?server_id=server-a").json()

    second = client.post(
        f"/api/scan-units/{child['id']}/complete",
        json={
            "manifest_path": "/shared/manifests/job/child/manifest.jsonl",
            "meta_path": "/shared/manifests/job/child/manifest.meta.json",
            "file_count": 0,
            "total_bytes": 0,
            "child_paths": ["/shared/input/b", "/shared/input/c"],
            "shards": [],
        },
    )

    assert second.status_code == 200
    with session_factory() as session:
        paths = [
            row.path
            for row in session.query(ScanUnit)
            .filter_by(job_id=job["id"])
            .order_by(ScanUnit.path.asc())
            .all()
        ]
    assert paths.count("/shared/input/b") == 1
    assert paths == [
        "/shared/input",
        "/shared/input/a",
        "/shared/input/b",
        "/shared/input/c",
    ]


def test_scan_unit_claim_checks_later_batches_when_first_batch_inaccessible(tmp_path, monkeypatch):
    monkeypatch.setattr("ocr_platform.control.service.SCAN_UNIT_CLAIM_BATCH_SIZE", 2)
    client, session_factory = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "shared_paths": [
                    {
                        "path": "/shared/allowed",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ]
            },
        },
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/blocked/root",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
            "target_files_per_shard": 1000,
        },
    ).json()
    with session_factory() as session:
        session.query(ScanUnit).filter_by(job_id=job["id"]).delete()
        session.add_all(
            [
                ScanUnit(job_id=job["id"], path="/shared/blocked/a", status="pending"),
                ScanUnit(job_id=job["id"], path="/shared/blocked/b", status="pending"),
                ScanUnit(job_id=job["id"], path="/shared/allowed/c", status="pending"),
            ]
        )
        session.commit()

    claimed = client.post("/api/scan-units/claim?server_id=server-a").json()

    assert claimed["path"] == "/shared/allowed/c"


def test_scan_unit_completion_is_idempotent_for_agent_retries(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
            "target_files_per_shard": 1000,
        },
    ).json()
    claimed = client.post("/api/scan-units/claim?server_id=server-a").json()
    payload = {
        "manifest_path": "/shared/manifests/job/scan-units/1/manifest.jsonl",
        "meta_path": "/shared/manifests/job/scan-units/1/manifest.meta.json",
        "file_count": 2,
        "total_bytes": 30,
        "child_paths": ["/shared/input/a"],
        "shards": [
            {
                "shard_index": 1,
                "shard_path": "/shared/manifests/job/scan-units/1/shards/shard-000001.jsonl",
                "file_count": 2,
            }
        ],
    }

    first = client.post(f"/api/scan-units/{claimed['id']}/complete", json=payload)
    second = client.post(f"/api/scan-units/{claimed['id']}/complete", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    with session_factory() as session:
        manifest = session.query(Manifest).filter_by(job_id=job["id"]).one()
        shards = session.query(WorkShard).filter_by(job_id=job["id"]).all()
        units = session.query(ScanUnit).filter_by(job_id=job["id"]).order_by(ScanUnit.id).all()
        assert manifest.file_count == 2
        assert manifest.total_bytes == 30
        assert manifest.next_shard_index == 2
        assert len(shards) == 1
        assert len(units) == 2
        assert [unit.path for unit in units] == ["/shared/input", "/shared/input/a"]


def test_scan_unit_late_complete_from_reclaimed_attempt_is_rejected(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/register",
        json={"id": "server-b", "name": "Server B", "host": "localhost"},
    )
    for server_id in ("server-a", "server-b"):
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
                            "writable": True,
                        }
                    ]
                },
            },
        )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
            "target_files_per_shard": 1000,
        },
    ).json()
    first = client.post("/api/scan-units/claim?server_id=server-a").json()
    with session_factory() as session:
        unit = session.get(ScanUnit, first["id"])
        unit.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    second = client.post("/api/scan-units/claim?server_id=server-b").json()
    assert second["id"] == first["id"]
    assert second["attempt_count"] == 2

    late_response = client.post(
        f"/api/scan-units/{first['id']}/complete",
        json={
            "assigned_server_id": "server-a",
            "attempt_count": 1,
            "manifest_path": "/shared/manifests/job/scan-units/1/manifest-late.jsonl",
            "meta_path": "/shared/manifests/job/scan-units/1/manifest-late.meta.json",
            "file_count": 2,
            "total_bytes": 30,
            "child_paths": ["/shared/input/late"],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": "/shared/manifests/job/scan-units/1/shards/shard-late.jsonl",
                    "file_count": 2,
                }
            ],
        },
    )

    assert late_response.status_code == 409
    with session_factory() as session:
        manifest = session.query(Manifest).filter_by(job_id=job["id"]).one()
        assert manifest.file_count == 0
        assert manifest.next_shard_index == 1
        assert session.query(WorkShard).filter_by(job_id=job["id"]).count() == 0
        assert session.query(ScanUnit).filter_by(job_id=job["id"]).count() == 1

    current_response = client.post(
        f"/api/scan-units/{second['id']}/complete",
        json={
            "assigned_server_id": "server-b",
            "attempt_count": 2,
            "manifest_path": "/shared/manifests/job/scan-units/1/manifest.jsonl",
            "meta_path": "/shared/manifests/job/scan-units/1/manifest.meta.json",
            "file_count": 1,
            "total_bytes": 10,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": "/shared/manifests/job/scan-units/1/shards/shard.jsonl",
                    "file_count": 1,
                }
            ],
        },
    )

    assert current_response.status_code == 200
    with session_factory() as session:
        manifest = session.query(Manifest).filter_by(job_id=job["id"]).one()
        shards = session.query(WorkShard).filter_by(job_id=job["id"]).all()
        assert manifest.file_count == 1
        assert manifest.next_shard_index == 2
        assert len(shards) == 1


def test_scan_unit_late_fail_from_reclaimed_attempt_is_rejected(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/register",
        json={"id": "server-b", "name": "Server B", "host": "localhost"},
    )
    for server_id in ("server-a", "server-b"):
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
                            "writable": True,
                        }
                    ]
                },
            },
        )
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
    first = client.post("/api/scan-units/claim?server_id=server-a").json()
    with session_factory() as session:
        unit = session.get(ScanUnit, first["id"])
        unit.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    second = client.post("/api/scan-units/claim?server_id=server-b").json()
    assert second["id"] == first["id"]
    assert second["attempt_count"] == 2

    late_response = client.post(
        f"/api/scan-units/{first['id']}/fail",
        json={
            "assigned_server_id": "server-a",
            "attempt_count": 1,
            "error_message": "old scan attempt failed late",
        },
    )

    assert late_response.status_code == 409
    with session_factory() as session:
        unit = session.get(ScanUnit, first["id"])
        manifest = session.query(Manifest).filter_by(job_id=job["id"]).one()
        assert unit.status == "running"
        assert unit.assigned_server_id == "server-b"
        assert unit.attempt_count == 2
        assert unit.error_message is None
        assert manifest.status == "scanning"

    current_response = client.post(
        f"/api/scan-units/{second['id']}/fail",
        json={
            "assigned_server_id": "server-b",
            "attempt_count": 2,
            "error_message": "current scan failed",
        },
    )

    assert current_response.status_code == 200
    with session_factory() as session:
        unit = session.get(ScanUnit, second["id"])
        manifest = session.query(Manifest).filter_by(job_id=job["id"]).one()
        assert unit.status == "failed"
        assert unit.error_message == "current scan failed"
        assert manifest.status == "failed"


def test_distributed_scan_unit_completion_allocates_global_shard_indexes(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
            "target_files_per_shard": 1,
        },
    ).json()

    root = client.post("/api/scan-units/claim?server_id=server-a").json()
    client.post(
        f"/api/scan-units/{root['id']}/complete",
        json={
            "manifest_path": "/shared/manifests/job/scan-units/root/manifest.jsonl",
            "meta_path": "/shared/manifests/job/scan-units/root/manifest.meta.json",
            "file_count": 0,
            "total_bytes": 0,
            "child_paths": ["/shared/input/a", "/shared/input/b"],
            "shards": [],
        },
    )
    first_child = client.post("/api/scan-units/claim?server_id=server-a").json()
    second_child = client.post("/api/scan-units/claim?server_id=server-a").json()

    for unit in (first_child, second_child):
        response = client.post(
            f"/api/scan-units/{unit['id']}/complete",
            json={
                "manifest_path": f"/shared/manifests/job/scan-units/{unit['id']}/manifest.jsonl",
                "meta_path": f"/shared/manifests/job/scan-units/{unit['id']}/manifest.meta.json",
                "file_count": 1,
                "total_bytes": 10,
                "child_paths": [],
                "shards": [
                    {
                        "shard_index": 1,
                        "shard_path": f"/shared/manifests/job/scan-units/{unit['id']}/shards/shard-local.jsonl",
                        "file_count": 1,
                    }
                ],
            },
        )
        assert response.status_code == 200

    with session_factory() as session:
        manifest = session.query(Manifest).filter_by(job_id=job["id"]).one()
        shards = session.query(WorkShard).filter_by(job_id=job["id"]).order_by(WorkShard.shard_index).all()
        assert [shard.shard_index for shard in shards] == [1, 2]
        assert manifest.next_shard_index == 3


def test_distributed_scan_unit_completion_uses_manifest_counter_without_max_scan(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
            "target_files_per_shard": 1,
        },
    ).json()
    claimed = client.post("/api/scan-units/claim?server_id=server-a").json()
    statements = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    bind = session_factory.kw["bind"]
    event.listen(bind, "before_cursor_execute", record_sql)
    try:
        response = client.post(
            f"/api/scan-units/{claimed['id']}/complete",
            json={
                "manifest_path": "/shared/manifests/job/scan-units/root/manifest.jsonl",
                "meta_path": "/shared/manifests/job/scan-units/root/manifest.meta.json",
                "file_count": 1,
                "total_bytes": 10,
                "child_paths": [],
                "shards": [
                    {
                        "shard_index": 1,
                        "shard_path": "/shared/manifests/job/scan-units/root/shards/shard.jsonl",
                        "file_count": 1,
                    }
                ],
            },
        )
    finally:
        event.remove(bind, "before_cursor_execute", record_sql)

    assert response.status_code == 200
    assert not any(
        "max(work_shards.shard_index)" in statement
        for statement in statements
    )


def test_distributed_scan_unit_completion_recovers_from_stale_next_shard_index(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
            "target_files_per_shard": 1,
        },
    ).json()
    root = client.post("/api/scan-units/claim?server_id=server-a").json()
    client.post(
        f"/api/scan-units/{root['id']}/complete",
        json={
            "manifest_path": "/shared/manifests/job/scan-units/root/manifest.jsonl",
            "meta_path": "/shared/manifests/job/scan-units/root/manifest.meta.json",
            "file_count": 0,
            "total_bytes": 0,
            "child_paths": ["/shared/input/a"],
            "shards": [],
        },
    )
    child = client.post("/api/scan-units/claim?server_id=server-a").json()
    with session_factory() as session:
        manifest = session.query(Manifest).filter_by(job_id=job["id"]).one()
        session.add(
            WorkShard(
                job_id=job["id"],
                manifest_id=manifest.id,
                shard_index=1,
                shard_path="/shared/manifests/job/scan-units/legacy/shard-1.jsonl",
                status="pending",
                file_count=1,
            )
        )
        manifest.next_shard_index = 1
        session.commit()

    response = client.post(
        f"/api/scan-units/{child['id']}/complete",
        json={
            "manifest_path": "/shared/manifests/job/scan-units/child/manifest.jsonl",
            "meta_path": "/shared/manifests/job/scan-units/child/manifest.meta.json",
            "file_count": 1,
            "total_bytes": 10,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": "/shared/manifests/job/scan-units/child/shards/shard-local.jsonl",
                    "file_count": 1,
                }
            ],
        },
    )

    assert response.status_code == 200
    with session_factory() as session:
        manifest = session.query(Manifest).filter_by(job_id=job["id"]).one()
        shards = session.query(WorkShard).filter_by(job_id=job["id"]).order_by(WorkShard.shard_index).all()
        assert [shard.shard_index for shard in shards] == [1, 2]
        assert manifest.next_shard_index == 3


def test_manifest_freeze_report_is_created_when_distributed_scan_finishes(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
            "target_files_per_shard": 1,
        },
    ).json()
    root = client.post("/api/scan-units/claim?server_id=server-a").json()

    before = client.get(f"/api/jobs/{job['id']}/manifest/freeze-report").json()

    assert before["status"] == "scanning"
    assert before["frozen_at"] is None
    assert before["report"]["frozen"] is False

    response = client.post(
        f"/api/scan-units/{root['id']}/complete",
        json={
            "manifest_path": "/shared/manifests/job/scan-units/root/manifest.jsonl",
            "meta_path": "/shared/manifests/job/scan-units/root/manifest.meta.json",
            "file_count": 2,
            "total_bytes": 30,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": "/shared/manifests/job/scan-units/root/shard-1.jsonl",
                    "file_count": 1,
                },
                {
                    "shard_index": 2,
                    "shard_path": "/shared/manifests/job/scan-units/root/shard-2.jsonl",
                    "file_count": 1,
                },
            ],
        },
    )

    assert response.status_code == 200
    report = client.get(f"/api/jobs/{job['id']}/manifest/freeze-report").json()

    assert report["status"] == "ready"
    assert report["frozen_at"] is not None
    assert report["report"]["frozen"] is True
    assert report["report"]["file_count"] == 2
    assert report["report"]["total_bytes"] == 30
    assert report["report"]["shard_count"] == 2
    assert report["report"]["shard_file_count"] == 2
    assert report["report"]["shard_file_count_matches_manifest"] is True
    assert report["report"]["scan_units"]["succeeded"] == 1
    assert report["report"]["scan_units"]["failed"] == 0
    summary = client.get(f"/api/jobs/{job['id']}/summary").json()
    assert summary["manifest_integrity_status"] == report["report"]["integrity_status"]
    assert summary["manifest_integrity_ok"] == report["report"]["integrity_ok"]
    assert summary["manifest_integrity_issue_count"] == report["report"]["integrity_issue_count"]
    with session_factory() as session:
        manifest = session.query(Manifest).filter_by(job_id=job["id"]).one()
        assert manifest.status == "ready"
        assert manifest.frozen_at is not None


def test_manifest_freeze_report_preserves_successful_scan_error_samples(tmp_path):
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
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
            "target_files_per_shard": 1,
        },
    ).json()
    root = client.post("/api/scan-units/claim?server_id=server-a").json()

    progress = client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "manifest_scan_progress",
            "payload": {
                "status": "running",
                "scanned_files": 12,
                "scanned_dirs": 4,
                "skipped_error_count": 2,
                "skipped_errors": [
                    {
                        "path": "/shared/input/blocked",
                        "reason": "permission denied",
                        "failure_category": "input_invalid",
                    }
                ],
            },
        },
    )
    assert progress.status_code == 200

    response = client.post(
        f"/api/scan-units/{root['id']}/complete",
        json={
            "manifest_path": "/shared/manifests/job/scan-units/root/manifest.jsonl",
            "meta_path": "/shared/manifests/job/scan-units/root/manifest.meta.json",
            "file_count": 12,
            "total_bytes": 30,
            "child_paths": [],
            "shards": [],
        },
    )

    assert response.status_code == 200
    report = client.get(f"/api/jobs/{job['id']}/manifest/freeze-report").json()

    assert report["status"] == "ready"
    assert report["report"]["frozen"] is True
    assert report["report"]["scan_error_count"] == 2
    assert report["report"]["scan_error_samples"] == [
        {
            "path": "/shared/input/blocked",
            "reason": "permission denied",
            "failure_category": "input_invalid",
        }
    ]


def test_job_summary_exposes_manifest_snapshot_state_for_distributed_scan(tmp_path):
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
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
            "target_files_per_shard": 1,
        },
    ).json()
    root = client.post("/api/scan-units/claim?server_id=server-a").json()

    scanning = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert scanning["scan_status"] == "running"
    assert scanning["manifest_status"] == "scanning"
    assert scanning["manifest_snapshot_status"] == "scanning"
    assert scanning["manifest_frozen_at"] is None

    response = client.post(
        f"/api/scan-units/{root['id']}/complete",
        json={
            "manifest_path": "/shared/manifests/job/scan-units/root/manifest.jsonl",
            "meta_path": "/shared/manifests/job/scan-units/root/manifest.meta.json",
            "file_count": 2,
            "total_bytes": 30,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": "/shared/manifests/job/scan-units/root/shard-1.jsonl",
                    "file_count": 1,
                },
                {
                    "shard_index": 2,
                    "shard_path": "/shared/manifests/job/scan-units/root/shard-2.jsonl",
                    "file_count": 1,
                },
            ],
        },
    )
    assert response.status_code == 200

    frozen = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert frozen["scan_status"] == "done"
    assert frozen["manifest_status"] == "ready"
    assert frozen["manifest_snapshot_status"] == "frozen"
    assert frozen["manifest_frozen_at"] is not None
    assert frozen["shards_created"] == 2


def test_distributed_scan_unit_lease_expires_and_reclaims(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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
    client.post(
        "/api/servers/server-b/heartbeat",
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
    first = client.post("/api/scan-units/claim?server_id=server-a").json()
    assert first["attempt_count"] == 1

    with session_factory() as session:
        unit = session.get(ScanUnit, first["id"])
        unit.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()
    assert summary["stale_scan_units"] == 1
    assert summary["scan_error_count"] == 1
    assert summary["scan_error_samples"] == [
        {
            "path": "/shared/input",
            "reason": "scan unit lease expired",
            "failure_category": "lease_expired",
        }
    ]
    with session_factory() as session:
        unit = session.get(ScanUnit, first["id"])
        assert unit.status == "stale"
        assert unit.failure_category == "lease_expired"
        assert unit.error_message == "scan unit lease expired"

    second = client.post("/api/scan-units/claim?server_id=server-b").json()

    assert second["id"] == first["id"]
    assert second["assigned_server_id"] == "server-b"
    assert second["attempt_count"] == 2
    assert second["failure_category"] is None
    assert second["error_message"] is None


def test_job_summary_includes_scan_unit_counts(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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
        root = session.query(ScanUnit).filter_by(job_id=job["id"]).one()
        root.status = "running"
        session.add(ScanUnit(job_id=job["id"], path="/shared/input/a", status="pending"))
        session.add(ScanUnit(job_id=job["id"], path="/shared/input/b", status="succeeded"))
        session.commit()

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["total_scan_units"] == 3
    assert summary["pending_scan_units"] == 1
    assert summary["running_scan_units"] == 1
    assert summary["succeeded_scan_units"] == 1


def test_job_summary_estimates_scan_eta_from_scan_unit_completion(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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
    now = utcnow()
    with session_factory() as session:
        db_job = session.get(Job, job["id"])
        db_job.started_at = now - timedelta(seconds=120)
        root = session.query(ScanUnit).filter_by(job_id=job["id"]).one()
        root.status = "succeeded"
        root.started_at = now - timedelta(seconds=120)
        root.finished_at = now - timedelta(seconds=60)
        session.add(ScanUnit(job_id=job["id"], path="/shared/input/a", status="succeeded"))
        session.add(ScanUnit(job_id=job["id"], path="/shared/input/b", status="pending"))
        session.add(ScanUnit(job_id=job["id"], path="/shared/input/c", status="running"))
        session.commit()

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["scan_eta_seconds"] is not None
    assert 110 <= summary["scan_eta_seconds"] <= 130


def test_job_summary_estimates_scan_eta_from_progress_scan_started_at(tmp_path):
    client, _ = make_client_with_session(tmp_path)
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
        },
    ).json()
    scan_started_at = (utcnow() - timedelta(seconds=120)).isoformat()
    response = client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "manifest_scan_progress",
            "payload": {
                "status": "running",
                "scanned_files": 100,
                "estimated_total_files": 200,
                "scan_started_at": scan_started_at,
            },
        },
    )
    assert response.status_code == 200

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["scan_eta_seconds"] is not None
    assert 110 <= summary["scan_eta_seconds"] <= 130


def test_job_summary_estimates_scan_eta_from_reported_scan_rate(tmp_path):
    client, _ = make_client_with_session(tmp_path)
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
        },
    ).json()
    response = client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "manifest_scan_progress",
            "payload": {
                "status": "running",
                "scanned_files": 50,
                "estimated_total_files": 200,
                "files_per_second": 5.0,
            },
        },
    )
    assert response.status_code == 200

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["scan_eta_seconds"] == 30


def test_job_summary_ignores_invalid_progress_scan_started_at(tmp_path):
    client, _ = make_client_with_session(tmp_path)
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
        },
    ).json()
    response = client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "manifest_scan_progress",
            "payload": {
                "status": "running",
                "scanned_files": 100,
                "estimated_total_files": 200,
                "scan_started_at": "not-a-date",
            },
        },
    )
    assert response.status_code == 200

    summary = client.get(f"/api/jobs/{job['id']}/summary")

    assert summary.status_code == 200


def test_job_summary_ignores_non_finite_reported_scan_rate(tmp_path):
    client, _ = make_client_with_session(tmp_path)
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
        },
    ).json()
    response = client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "manifest_scan_progress",
            "payload": {
                "status": "running",
                "scanned_files": 50,
                "estimated_total_files": 200,
                "files_per_second": "NaN",
            },
        },
    )
    assert response.status_code == 200

    summary = client.get(f"/api/jobs/{job['id']}/summary")

    assert summary.status_code == 200


def test_job_summary_does_not_show_scan_eta_after_done_progress(tmp_path):
    client, _ = make_client_with_session(tmp_path)
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
        },
    ).json()
    response = client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "manifest_scan_progress",
            "payload": {
                "status": "done",
                "scanned_files": 100,
                "estimated_total_files": 200,
                "scan_started_at": "2026-06-01T00:00:00+00:00",
                "scan_finished_at": "2026-06-01T00:10:00+00:00",
            },
        },
    )
    assert response.status_code == 200

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["scan_status"] == "done"
    assert summary["scan_started_at"] == "2026-06-01T00:00:00Z"
    assert summary["scan_finished_at"] == "2026-06-01T00:10:00Z"
    assert summary["scan_eta_seconds"] is None


def test_job_summary_uses_manifest_and_scan_units_as_scan_progress_fallback(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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
    root = client.post("/api/scan-units/claim?server_id=server-a").json()
    client.post(
        f"/api/scan-units/{root['id']}/complete",
        json={
            "manifest_path": "/shared/manifests/job/scan-units/root/manifest.jsonl",
            "meta_path": "/shared/manifests/job/scan-units/root/manifest.meta.json",
            "file_count": 2,
            "total_bytes": 30,
            "child_paths": ["/shared/input/a"],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": "/shared/manifests/job/scan-units/root/shard.jsonl",
                    "file_count": 2,
                }
            ],
        },
    )
    with session_factory() as session:
        child = session.query(ScanUnit).filter_by(job_id=job["id"], path="/shared/input/a").one()
        child.status = "running"
        session.commit()

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["scan_status"] == "running"
    assert summary["scan_progress_files"] == 2
    assert summary["scan_progress_dirs"] == 1
    assert summary["scan_progress_bytes"] == 30


def test_scan_unit_failure_is_recorded_immediately_and_visible_in_summary(tmp_path):
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
    claimed = client.post("/api/scan-units/claim?server_id=server-a").json()

    response = client.post(
        f"/api/scan-units/{claimed['id']}/fail",
        json={"error_message": "permission denied"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["failure_category"] == "input_invalid"
    summary = client.get(f"/api/jobs/{job['id']}/summary").json()
    assert summary["scan_status"] == "failed"
    assert summary["failed_scan_units"] == 1
    assert summary["scan_unit_failure_category_counts"] == {"input_invalid": 1}
    assert summary["scan_error_count"] == 1
    assert summary["scan_error_samples"] == [
        {
            "path": "/shared/input",
            "reason": "permission denied",
            "failure_category": "input_invalid",
        }
    ]


def test_scan_unit_failure_infers_failure_category_from_error_message(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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
    claimed = client.post("/api/scan-units/claim?server_id=server-a").json()

    response = client.post(
        f"/api/scan-units/{claimed['id']}/fail",
        json={
            "assigned_server_id": "server-a",
            "attempt_count": claimed["attempt_count"],
            "error_message": "No space left on device while writing manifest shard",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["failure_category"] == "output_unwritable"
    assert response.json()["error_message"] == "No space left on device while writing manifest shard"
    summary = client.get(f"/api/jobs/{job['id']}/summary").json()
    assert summary["scan_error_samples"] == [
        {
            "path": "/shared/input",
            "reason": "No space left on device while writing manifest shard",
            "failure_category": "output_unwritable",
        }
    ]
    with session_factory() as session:
        unit = session.get(ScanUnit, claimed["id"])
        assert unit.failure_category == "output_unwritable"
        assert unit.error_message == "No space left on device while writing manifest shard"
        assert session.query(Manifest).filter_by(job_id=job["id"]).one().status == "failed"


def test_job_summary_includes_manifest_scan_progress_and_error_sample(tmp_path):
    client, _ = make_client_with_session(tmp_path)
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

    client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "manifest_scan_progress",
            "payload": {
                "status": "running",
                "server_id": "server-a",
                "scanned_files": 1200,
                "estimated_total_files": 5000,
                "remaining_files": 3800,
                "estimated_remaining_seconds": 760,
                "scanned_dirs": 34,
                "total_bytes": 9876,
                "current_path": "/shared/input/a/b.pdf",
                "skipped_errors": [
                    {"path": "/shared/input/private", "reason": "permission denied"}
                ],
            },
        },
    )

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["scan_status"] == "running"
    assert summary["scan_progress_files"] == 1200
    assert summary["scan_discovered_pdf_count"] == 1200
    assert summary["scan_estimated_total_files"] == 5000
    assert summary["scan_estimated_total_pdf_count"] == 5000
    assert summary["scan_remaining_files"] == 3800
    assert summary["scan_remaining_pdf_count"] == 3800
    assert summary["scan_progress_percent"] == 24.0
    assert summary["scan_progress_dirs"] == 34
    assert summary["scan_progress_bytes"] == 9876
    assert summary["scan_current_path"] == "/shared/input/a/b.pdf"
    assert summary["scan_error_count"] == 1
    assert summary["scan_error_samples"] == [
        {
            "path": "/shared/input/private",
            "reason": "permission denied",
            "failure_category": "input_invalid",
        }
    ]
    assert summary["scan_eta_seconds"] == 760


def test_job_summary_preserves_recent_scan_error_samples_across_progress_events(tmp_path):
    client, _ = make_client_with_session(tmp_path)
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
    client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "manifest_scan_progress",
            "payload": {
                "status": "running",
                "scanned_files": 10,
                "scanned_dirs": 2,
                "skipped_error_count": 1,
                "skipped_errors": [
                    {"path": "/shared/input/private", "reason": "permission denied"}
                ],
            },
        },
    )
    client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "manifest_scan_progress",
            "payload": {
                "status": "running",
                "scanned_files": 200,
                "scanned_dirs": 20,
                "skipped_error_count": 1,
                "skipped_errors": [],
            },
        },
    )

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["scan_progress_files"] == 200
    assert summary["scan_progress_dirs"] == 20
    assert summary["scan_error_count"] == 1
    assert summary["scan_error_samples"] == [
        {
            "path": "/shared/input/private",
            "reason": "permission denied",
            "failure_category": "input_invalid",
        }
    ]


def test_manifest_integrity_report_checks_files_and_shard_counts(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (input_root / "b.pdf").write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()

    ok_report = client.get(f"/api/jobs/{job['id']}/manifest/integrity")

    assert ok_report.status_code == 200
    assert ok_report.json()["ok"] is True
    assert ok_report.json()["manifest_file_exists"] is True
    assert ok_report.json()["manifest_file_count_matches"] is True
    assert ok_report.json()["shard_count"] == 2

    shard_path = manifest_root / job["id"] / "shards" / "shard-000001.jsonl"
    shard_path.write_text("", encoding="utf-8")

    bad_report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert bad_report["ok"] is False
    assert bad_report["bad_shards"][0]["reason"] == "file_count_mismatch"


def test_manifest_integrity_reports_worker_shared_paths_as_not_accessible_from_control(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
        },
    ).json()

    with session_factory() as session:
        manifest = Manifest(
            job_id=job["id"],
            input_mode="remote_folder_snapshot",
            input_root="/shared/input",
            manifest_path=f"/shared/manifests/{job['id']}/manifest.jsonl",
            meta_path=f"/shared/manifests/{job['id']}/manifest.meta.json",
            file_count=2,
            total_bytes=20,
            status="ready",
        )
        session.add(manifest)
        session.flush()
        session.add(
            WorkShard(
                job_id=job["id"],
                manifest_id=manifest.id,
                shard_index=1,
                shard_path=f"/shared/manifests/{job['id']}/shards/shard-000001.jsonl",
                status="pending",
                file_count=2,
            )
        )
        session.commit()

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["status"] == "not_accessible_from_control"
    assert report["shard_count"] == 1
    assert report["bad_shard_count"] == 0


def test_manifest_integrity_can_be_checked_by_worker_for_worker_only_paths(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
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
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
        },
    ).json()

    with session_factory() as session:
        manifest = Manifest(
            job_id=job["id"],
            input_mode="remote_folder_snapshot",
            input_root="/shared/input",
            manifest_path=f"/shared/manifests/{job['id']}/manifest.jsonl",
            meta_path=f"/shared/manifests/{job['id']}/manifest.meta.json",
            file_count=2,
            total_bytes=20,
            status="ready",
        )
        session.add(manifest)
        session.flush()
        session.add(
            WorkShard(
                job_id=job["id"],
                manifest_id=manifest.id,
                shard_index=1,
                shard_path=f"/shared/manifests/{job['id']}/shards/shard-000001.jsonl",
                status="pending",
                file_count=2,
            )
        )
        session.commit()
        manifest_id = manifest.id

    request = client.post(f"/api/jobs/{job['id']}/manifest/integrity/worker-request")
    assert request.status_code == 200
    assert request.json()["worker_integrity_status"] == "pending"

    claimed = client.post("/api/manifest-integrity/claim?server_id=server-a").json()
    assert claimed["job_id"] == job["id"]
    assert claimed["manifest_id"] == manifest_id
    assert claimed["manifest_path"] == f"/shared/manifests/{job['id']}/manifest.jsonl"
    assert claimed["shards"][0]["shard_path"].endswith("shard-000001.jsonl")

    complete = client.post(
        f"/api/manifest-integrity/{manifest_id}/complete?server_id=server-a",
        json={
            "report": {
                "job_id": job["id"],
                "manifest_id": manifest_id,
                "ok": True,
                "status": "ok",
                "manifest_path": f"/shared/manifests/{job['id']}/manifest.jsonl",
                "manifest_file_exists": True,
                "manifest_expected_file_count": 2,
                "manifest_actual_file_count": 2,
                "manifest_file_count_matches": True,
                "manifest_expected_total_bytes": 20,
                "manifest_actual_total_bytes": 20,
                "manifest_total_bytes_matches": True,
                "meta_path": f"/shared/manifests/{job['id']}/manifest.meta.json",
                "meta_file_exists": True,
                "meta_expected_file_count": 2,
                "meta_actual_file_count": 2,
                "meta_file_count_matches": True,
                "meta_expected_total_bytes": 20,
                "meta_actual_total_bytes": 20,
                "meta_total_bytes_matches": True,
                "shard_count": 1,
                "shard_expected_file_count": 2,
                "shard_reference_file_count": 2,
                "shard_file_count_matches_manifest": True,
                "bad_shard_count": 0,
                "bad_shards": [],
            }
        },
    )
    assert complete.status_code == 200
    assert complete.json()["worker_integrity_status"] == "ok"

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()
    assert report["source"] == "worker"
    assert report["checked_by_server_id"] == "server-a"
    assert report["ok"] is True
    assert report["status"] == "ok"

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()
    assert summary["manifest_integrity_ok"] is True
    assert summary["manifest_integrity_status"] == "ok"


def test_manifest_integrity_report_bounds_bad_shard_samples(tmp_path):
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
                    shard_path=f"/shared/missing/shard-{index:06d}.jsonl",
                    status="pending",
                    file_count=1,
                )
            )
        session.commit()

    report = client.get(f"/api/jobs/{job_id}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["bad_shard_count"] == 60
    assert len(report["bad_shards"]) == 50
    assert [item["shard_index"] for item in report["bad_shards"]] == list(range(1, 51))


def test_manifest_integrity_report_rejects_malformed_shard_jsonl(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()
    shard_path = manifest_root / job["id"] / "shards" / "shard-000001.jsonl"
    shard_path.write_text('{"ok": ', encoding="utf-8")

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["bad_shards"][0]["reason"] == "malformed_jsonl"


def test_manifest_integrity_report_rejects_invalid_shard_manifest_row_schema(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()
    shard_path = manifest_root / job["id"] / "shards" / "shard-000001.jsonl"
    shard_path.write_text("{}\n", encoding="utf-8")

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["bad_shards"][0]["reason"] == "invalid_manifest_row"


def test_manifest_integrity_report_rejects_unsafe_shard_relative_path(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    pdf = input_root / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()
    shard_path = manifest_root / job["id"] / "shards" / "shard-000001.jsonl"
    shard_path.write_text(
        ManifestItem(
            input_path=str(pdf),
            relative_path="../escape.pdf",
            size_bytes=pdf.stat().st_size,
            mtime_ns=pdf.stat().st_mtime_ns,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["bad_shards"][0]["reason"] == "invalid_relative_path"


def test_manifest_integrity_report_rejects_duplicate_shard_relative_path(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    first = input_root / "a.pdf"
    second = input_root / "b.pdf"
    first.write_bytes(b"%PDF-1.4\n")
    second.write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 2,
        },
    ).json()
    duplicate_rows = "\n".join(
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
    shard_path = manifest_root / job["id"] / "shards" / "shard-000001.jsonl"
    shard_path.write_text(duplicate_rows + "\n", encoding="utf-8")

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["bad_shards"][0]["reason"] == "duplicate_relative_path"


def test_manifest_integrity_report_rejects_duplicate_relative_path_across_shards(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    first = input_root / "a.pdf"
    second = input_root / "b.pdf"
    first.write_bytes(b"%PDF-1.4\n")
    second.write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()
    first_shard = manifest_root / job["id"] / "shards" / "shard-000001.jsonl"
    second_shard = manifest_root / job["id"] / "shards" / "shard-000002.jsonl"
    first_shard.write_text(
        ManifestItem(
            input_path=str(first),
            relative_path="a.pdf",
            size_bytes=first.stat().st_size,
            mtime_ns=first.stat().st_mtime_ns,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )
    second_shard.write_text(
        ManifestItem(
            input_path=str(second),
            relative_path="a.pdf",
            size_bytes=second.stat().st_size,
            mtime_ns=second.stat().st_mtime_ns,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["bad_shards"][0]["shard_index"] == 2
    assert report["bad_shards"][0]["reason"] == "duplicate_relative_path"


def test_manifest_integrity_report_surfaces_malformed_top_level_manifest_jsonl(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()
    manifest_path = manifest_root / job["id"] / "manifest.jsonl"
    manifest_path.write_text('{"ok": ', encoding="utf-8")

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["manifest_actual_file_count"] is None
    assert report["manifest_file_count_matches"] is False
    assert report["manifest_error"] == "malformed_jsonl"


def test_manifest_integrity_report_rejects_invalid_top_level_manifest_row_schema(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()
    manifest_path = manifest_root / job["id"] / "manifest.jsonl"
    manifest_path.write_text("{}\n", encoding="utf-8")

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["manifest_actual_file_count"] is None
    assert report["manifest_file_count_matches"] is False
    assert report["manifest_error"] == "invalid_manifest_row"


def test_manifest_integrity_report_checks_top_level_manifest_total_bytes(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    pdf = input_root / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()
    manifest_path = manifest_root / job["id"] / "manifest.jsonl"
    manifest_path.write_text(
        ManifestItem(
            input_path=str(pdf),
            relative_path="a.pdf",
            size_bytes=pdf.stat().st_size + 1,
            mtime_ns=pdf.stat().st_mtime_ns,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["manifest_expected_total_bytes"] == pdf.stat().st_size
    assert report["manifest_actual_total_bytes"] == pdf.stat().st_size + 1
    assert report["manifest_total_bytes_matches"] is False
    assert report["manifest_error"] == "total_bytes_mismatch"


def test_manifest_integrity_report_rejects_unsafe_top_level_manifest_relative_path(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    pdf = input_root / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()
    manifest_path = manifest_root / job["id"] / "manifest.jsonl"
    manifest_path.write_text(
        ManifestItem(
            input_path=str(pdf),
            relative_path="../escape.pdf",
            size_bytes=pdf.stat().st_size,
            mtime_ns=pdf.stat().st_mtime_ns,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["manifest_actual_file_count"] is None
    assert report["manifest_file_count_matches"] is False
    assert report["manifest_error"] == "invalid_relative_path"


def test_manifest_integrity_report_rejects_duplicate_top_level_manifest_relative_path(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    first = input_root / "a.pdf"
    second = input_root / "b.pdf"
    first.write_bytes(b"%PDF-1.4\n")
    second.write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 2,
        },
    ).json()
    duplicate_rows = "\n".join(
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
    manifest_path = manifest_root / job["id"] / "manifest.jsonl"
    manifest_path.write_text(duplicate_rows + "\n", encoding="utf-8")

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["manifest_actual_file_count"] is None
    assert report["manifest_file_count_matches"] is False
    assert report["manifest_error"] == "duplicate_relative_path"


def test_manifest_integrity_report_rejects_malformed_meta_json(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()
    meta_path = manifest_root / job["id"] / "manifest.meta.json"
    meta_path.write_text("{", encoding="utf-8")

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["meta_file_exists"] is True
    assert report["meta_error"] == "malformed_json"


def test_manifest_integrity_report_checks_top_level_meta_file_count(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (input_root / "b.pdf").write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()
    meta_path = manifest_root / job["id"] / "manifest.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["file_count"] = 1
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["meta_expected_file_count"] == 2
    assert report["meta_actual_file_count"] == 1
    assert report["meta_file_count_matches"] is False


def test_manifest_integrity_report_checks_top_level_meta_total_bytes(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (input_root / "b.pdf").write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()
    meta_path = manifest_root / job["id"] / "manifest.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    expected_total_bytes = meta["total_bytes"]
    meta["total_bytes"] = expected_total_bytes + 1
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["meta_expected_total_bytes"] == expected_total_bytes
    assert report["meta_actual_total_bytes"] == expected_total_bytes + 1
    assert report["meta_total_bytes_matches"] is False
    assert report["meta_error"] == "total_bytes_mismatch"


def test_manifest_integrity_report_fails_when_shard_totals_do_not_cover_manifest(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (input_root / "b.pdf").write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()
    with session_factory() as session:
        extra_shard = (
            session.query(WorkShard)
            .filter_by(job_id=job["id"], shard_index=2)
            .one()
        )
        session.delete(extra_shard)
        session.commit()

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["shard_expected_file_count"] == 1
    assert report["shard_reference_file_count"] == 2
    assert report["shard_file_count_matches_manifest"] is False


def test_manifest_integrity_report_rejects_shard_relative_path_not_in_manifest(tmp_path):
    manifest_root = tmp_path / "manifests"
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (input_root / "b.pdf").write_bytes(b"%PDF-1.4\n")
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
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1,
        },
    ).json()
    shard_path = manifest_root / job["id"] / "shards" / "shard-000001.jsonl"
    shard_path.write_text(
        ManifestItem(
            input_path=str(input_root / "a.pdf"),
            relative_path="ghost.pdf",
            size_bytes=(input_root / "a.pdf").stat().st_size,
            mtime_ns=(input_root / "a.pdf").stat().st_mtime_ns,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["bad_shards"][0]["shard_index"] == 1
    assert report["bad_shards"][0]["reason"] == "relative_path_not_in_manifest"


def test_manifest_integrity_report_checks_distributed_scan_unit_manifests(tmp_path):
    shared_root = tmp_path / "shared"
    manifest_root = shared_root / "manifests"
    scan_dir = manifest_root / "job" / "scan-units" / "1"
    scan_dir.mkdir(parents=True)
    scan_manifest = scan_dir / "manifest.jsonl"
    scan_manifest.write_text(
        "\n".join(
            [
                ManifestItem(
                    input_path=str(shared_root / "input" / "a.pdf"),
                    relative_path="a.pdf",
                    size_bytes=10,
                    mtime_ns=1,
                ).to_json_line(),
                ManifestItem(
                    input_path=str(shared_root / "input" / "b.pdf"),
                    relative_path="b.pdf",
                    size_bytes=20,
                    mtime_ns=2,
                ).to_json_line(),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    scan_meta = scan_dir / "manifest.meta.json"
    scan_meta.write_text("{}", encoding="utf-8")
    shard_dir = scan_dir / "shards"
    shard_dir.mkdir()
    shard = shard_dir / "shard-000001.jsonl"
    shard.write_text(scan_manifest.read_text(encoding="utf-8"), encoding="utf-8")
    client, _ = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "shared_paths": [
                    {
                        "path": str(shared_root),
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ]
            },
        },
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(shared_root / "input"),
            "output_dir": str(shared_root / "output"),
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1000,
        },
    ).json()
    unit = client.post("/api/scan-units/claim?server_id=server-a").json()

    complete = client.post(
        f"/api/scan-units/{unit['id']}/complete",
        json={
            "manifest_path": str(scan_manifest),
            "meta_path": str(scan_meta),
            "file_count": 2,
            "total_bytes": 30,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": str(shard),
                    "file_count": 2,
                }
            ],
        },
    )

    assert complete.status_code == 200
    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is True
    assert report["manifest_file_exists"] is False
    assert report["scan_unit_count"] == 1
    assert report["scan_unit_manifest_expected_file_count"] == 2
    assert report["scan_unit_manifest_actual_file_count"] == 2
    assert report["scan_unit_manifest_count_matches"] is True
    assert report["bad_scan_units"] == []

    freeze_report = client.get(f"/api/jobs/{job['id']}/manifest/freeze-report").json()
    assert freeze_report["report"]["integrity_ok"] is True
    assert freeze_report["report"]["integrity_status"] == "ok"
    assert freeze_report["report"]["integrity_issue_count"] == 0
    assert freeze_report["report"]["integrity_scan_unit_manifest_count_matches"] is True
    assert freeze_report["report"]["integrity_shard_file_count_matches_manifest"] is True


def test_manifest_integrity_report_rejects_shard_relative_path_not_in_scan_unit_manifest(tmp_path):
    shared_root = tmp_path / "shared"
    manifest_root = shared_root / "manifests"
    scan_dir = manifest_root / "job" / "scan-units" / "1"
    scan_dir.mkdir(parents=True)
    scan_manifest = scan_dir / "manifest.jsonl"
    scan_manifest.write_text(
        ManifestItem(
            input_path=str(shared_root / "input" / "a.pdf"),
            relative_path="a.pdf",
            size_bytes=10,
            mtime_ns=1,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )
    scan_meta = scan_dir / "manifest.meta.json"
    scan_meta.write_text("{}", encoding="utf-8")
    shard_dir = scan_dir / "shards"
    shard_dir.mkdir()
    shard = shard_dir / "shard-000001.jsonl"
    shard.write_text(
        ManifestItem(
            input_path=str(shared_root / "input" / "a.pdf"),
            relative_path="ghost.pdf",
            size_bytes=10,
            mtime_ns=1,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )
    client, _ = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "shared_paths": [
                    {
                        "path": str(shared_root),
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ]
            },
        },
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(shared_root / "input"),
            "output_dir": str(shared_root / "output"),
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1000,
        },
    ).json()
    unit = client.post("/api/scan-units/claim?server_id=server-a").json()
    complete = client.post(
        f"/api/scan-units/{unit['id']}/complete",
        json={
            "manifest_path": str(scan_manifest),
            "meta_path": str(scan_meta),
            "file_count": 1,
            "total_bytes": 10,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": str(shard),
                    "file_count": 1,
                }
            ],
        },
    )

    assert complete.status_code == 200
    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["bad_shards"][0]["shard_index"] == 1
    assert report["bad_shards"][0]["reason"] == "relative_path_not_in_manifest"


def test_manifest_integrity_report_rejects_invalid_scan_unit_manifest_row_schema(tmp_path):
    shared_root = tmp_path / "shared"
    manifest_root = shared_root / "manifests"
    scan_dir = manifest_root / "job" / "scan-units" / "1"
    scan_dir.mkdir(parents=True)
    scan_manifest = scan_dir / "manifest.jsonl"
    scan_manifest.write_text("{}\n", encoding="utf-8")
    valid_manifest_line = (
        ManifestItem(
            input_path=str(shared_root / "input" / "a.pdf"),
            relative_path="a.pdf",
            size_bytes=10,
            mtime_ns=1,
        ).to_json_line()
        + "\n"
    )
    scan_meta = scan_dir / "manifest.meta.json"
    scan_meta.write_text("{}", encoding="utf-8")
    shard_dir = scan_dir / "shards"
    shard_dir.mkdir()
    shard = shard_dir / "shard-000001.jsonl"
    shard.write_text(valid_manifest_line, encoding="utf-8")
    client, _ = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "shared_paths": [
                    {
                        "path": str(shared_root),
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ]
            },
        },
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(shared_root / "input"),
            "output_dir": str(shared_root / "output"),
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1000,
        },
    ).json()
    unit = client.post("/api/scan-units/claim?server_id=server-a").json()
    complete = client.post(
        f"/api/scan-units/{unit['id']}/complete",
        json={
            "manifest_path": str(scan_manifest),
            "meta_path": str(scan_meta),
            "file_count": 1,
            "total_bytes": 10,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": str(shard),
                    "file_count": 1,
                }
            ],
        },
    )

    assert complete.status_code == 200
    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["scan_unit_manifest_actual_file_count"] is None
    assert report["scan_unit_manifest_count_matches"] is False
    assert report["bad_scan_units"][0]["reason"] == "invalid_manifest_row"


def test_manifest_integrity_report_checks_scan_unit_manifest_total_bytes(tmp_path):
    shared_root = tmp_path / "shared"
    manifest_root = shared_root / "manifests"
    scan_dir = manifest_root / "job" / "scan-units" / "1"
    scan_dir.mkdir(parents=True)
    scan_manifest = scan_dir / "manifest.jsonl"
    scan_manifest.write_text(
        ManifestItem(
            input_path=str(shared_root / "input" / "a.pdf"),
            relative_path="a.pdf",
            size_bytes=11,
            mtime_ns=1,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )
    scan_meta = scan_dir / "manifest.meta.json"
    scan_meta.write_text("{}", encoding="utf-8")
    shard_dir = scan_dir / "shards"
    shard_dir.mkdir()
    shard = shard_dir / "shard-000001.jsonl"
    shard.write_text(scan_manifest.read_text(encoding="utf-8"), encoding="utf-8")
    client, _ = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "shared_paths": [
                    {
                        "path": str(shared_root),
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ]
            },
        },
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(shared_root / "input"),
            "output_dir": str(shared_root / "output"),
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1000,
        },
    ).json()
    unit = client.post("/api/scan-units/claim?server_id=server-a").json()
    complete = client.post(
        f"/api/scan-units/{unit['id']}/complete",
        json={
            "manifest_path": str(scan_manifest),
            "meta_path": str(scan_meta),
            "file_count": 1,
            "total_bytes": 10,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": str(shard),
                    "file_count": 1,
                }
            ],
        },
    )

    assert complete.status_code == 200
    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["scan_unit_manifest_actual_file_count"] is None
    assert report["scan_unit_manifest_count_matches"] is False
    assert report["bad_scan_units"][0]["reason"] == "total_bytes_mismatch"


def test_manifest_integrity_report_rejects_unsafe_scan_unit_relative_path(tmp_path):
    shared_root = tmp_path / "shared"
    manifest_root = shared_root / "manifests"
    scan_dir = manifest_root / "job" / "scan-units" / "1"
    scan_dir.mkdir(parents=True)
    pdf = shared_root / "input" / "a.pdf"
    scan_manifest = scan_dir / "manifest.jsonl"
    scan_manifest.write_text(
        ManifestItem(
            input_path=str(pdf),
            relative_path="../escape.pdf",
            size_bytes=10,
            mtime_ns=1,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )
    valid_manifest_line = (
        ManifestItem(
            input_path=str(pdf),
            relative_path="a.pdf",
            size_bytes=10,
            mtime_ns=1,
        ).to_json_line()
        + "\n"
    )
    scan_meta = scan_dir / "manifest.meta.json"
    scan_meta.write_text("{}", encoding="utf-8")
    shard_dir = scan_dir / "shards"
    shard_dir.mkdir()
    shard = shard_dir / "shard-000001.jsonl"
    shard.write_text(valid_manifest_line, encoding="utf-8")
    client, _ = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "shared_paths": [
                    {
                        "path": str(shared_root),
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ]
            },
        },
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(shared_root / "input"),
            "output_dir": str(shared_root / "output"),
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1000,
        },
    ).json()
    unit = client.post("/api/scan-units/claim?server_id=server-a").json()
    complete = client.post(
        f"/api/scan-units/{unit['id']}/complete",
        json={
            "manifest_path": str(scan_manifest),
            "meta_path": str(scan_meta),
            "file_count": 1,
            "total_bytes": 10,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": str(shard),
                    "file_count": 1,
                }
            ],
        },
    )

    assert complete.status_code == 200
    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["scan_unit_manifest_actual_file_count"] is None
    assert report["scan_unit_manifest_count_matches"] is False
    assert report["bad_scan_units"][0]["reason"] == "invalid_relative_path"


def test_manifest_integrity_report_rejects_duplicate_scan_unit_relative_path(tmp_path):
    shared_root = tmp_path / "shared"
    manifest_root = shared_root / "manifests"
    scan_dir = manifest_root / "job" / "scan-units" / "1"
    scan_dir.mkdir(parents=True)
    first = shared_root / "input" / "a.pdf"
    second = shared_root / "input" / "b.pdf"
    duplicate_rows = "\n".join(
        [
            ManifestItem(
                input_path=str(first),
                relative_path="same.pdf",
                size_bytes=10,
                mtime_ns=1,
            ).to_json_line(),
            ManifestItem(
                input_path=str(second),
                relative_path="same.pdf",
                size_bytes=20,
                mtime_ns=2,
            ).to_json_line(),
        ]
    )
    scan_manifest = scan_dir / "manifest.jsonl"
    scan_manifest.write_text(duplicate_rows + "\n", encoding="utf-8")
    valid_manifest_rows = "\n".join(
        [
            ManifestItem(
                input_path=str(first),
                relative_path="a.pdf",
                size_bytes=10,
                mtime_ns=1,
            ).to_json_line(),
            ManifestItem(
                input_path=str(second),
                relative_path="b.pdf",
                size_bytes=20,
                mtime_ns=2,
            ).to_json_line(),
        ]
    )
    scan_meta = scan_dir / "manifest.meta.json"
    scan_meta.write_text("{}", encoding="utf-8")
    shard_dir = scan_dir / "shards"
    shard_dir.mkdir()
    shard = shard_dir / "shard-000001.jsonl"
    shard.write_text(valid_manifest_rows + "\n", encoding="utf-8")
    client, _ = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "shared_paths": [
                    {
                        "path": str(shared_root),
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ]
            },
        },
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(shared_root / "input"),
            "output_dir": str(shared_root / "output"),
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1000,
        },
    ).json()
    unit = client.post("/api/scan-units/claim?server_id=server-a").json()
    complete = client.post(
        f"/api/scan-units/{unit['id']}/complete",
        json={
            "manifest_path": str(scan_manifest),
            "meta_path": str(scan_meta),
            "file_count": 2,
            "total_bytes": 30,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": str(shard),
                    "file_count": 2,
                }
            ],
        },
    )

    assert complete.status_code == 200
    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["scan_unit_manifest_actual_file_count"] is None
    assert report["scan_unit_manifest_count_matches"] is False
    assert report["bad_scan_units"][0]["reason"] == "duplicate_relative_path"


def test_manifest_integrity_report_rejects_duplicate_relative_path_across_scan_units(tmp_path):
    shared_root = tmp_path / "shared"
    manifest_root = shared_root / "manifests"
    first_scan_dir = manifest_root / "job" / "scan-units" / "1"
    second_scan_dir = manifest_root / "job" / "scan-units" / "2"
    first_scan_dir.mkdir(parents=True)
    second_scan_dir.mkdir(parents=True)
    first_pdf = shared_root / "input" / "a.pdf"
    second_pdf = shared_root / "input" / "b.pdf"
    first_manifest = first_scan_dir / "manifest.jsonl"
    second_manifest = second_scan_dir / "manifest.jsonl"
    first_manifest.write_text(
        ManifestItem(
            input_path=str(first_pdf),
            relative_path="same.pdf",
            size_bytes=10,
            mtime_ns=1,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )
    second_manifest.write_text(
        ManifestItem(
            input_path=str(second_pdf),
            relative_path="same.pdf",
            size_bytes=20,
            mtime_ns=2,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )
    first_meta = first_scan_dir / "manifest.meta.json"
    second_meta = second_scan_dir / "manifest.meta.json"
    first_meta.write_text("{}", encoding="utf-8")
    second_meta.write_text("{}", encoding="utf-8")
    first_shard_dir = first_scan_dir / "shards"
    second_shard_dir = second_scan_dir / "shards"
    first_shard_dir.mkdir()
    second_shard_dir.mkdir()
    first_shard = first_shard_dir / "shard-000001.jsonl"
    second_shard = second_shard_dir / "shard-000001.jsonl"
    first_shard.write_text(first_manifest.read_text(encoding="utf-8"), encoding="utf-8")
    second_shard.write_text(second_manifest.read_text(encoding="utf-8"), encoding="utf-8")
    client, session_factory = make_client_with_session(tmp_path)
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(shared_root / "input"),
            "output_dir": str(shared_root / "output"),
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1000,
        },
    ).json()
    with session_factory() as session:
        manifest = session.query(Manifest).filter_by(job_id=job["id"]).one()
        session.query(ScanUnit).filter_by(job_id=job["id"]).delete()
        first_unit = ScanUnit(
            job_id=job["id"],
            path=str(shared_root / "input" / "a"),
            status="succeeded",
            manifest_path=str(first_manifest),
            meta_path=str(first_meta),
            file_count=1,
            total_bytes=10,
        )
        second_unit = ScanUnit(
            job_id=job["id"],
            path=str(shared_root / "input" / "b"),
            status="succeeded",
            manifest_path=str(second_manifest),
            meta_path=str(second_meta),
            file_count=1,
            total_bytes=20,
        )
        session.add_all([first_unit, second_unit])
        session.flush()
        manifest.file_count = 2
        manifest.total_bytes = 30
        session.add_all(
            [
                WorkShard(
                    job_id=job["id"],
                    manifest_id=manifest.id,
                    shard_index=1,
                    shard_path=str(first_shard),
                    status="pending",
                    file_count=1,
                ),
                WorkShard(
                    job_id=job["id"],
                    manifest_id=manifest.id,
                    shard_index=2,
                    shard_path=str(second_shard),
                    status="pending",
                    file_count=1,
                ),
            ]
        )
        session.commit()

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["scan_unit_manifest_actual_file_count"] is None
    assert report["scan_unit_manifest_count_matches"] is False
    assert report["bad_scan_units"][0]["manifest_path"] == str(second_manifest)
    assert report["bad_scan_units"][0]["reason"] == "duplicate_relative_path"


def test_manifest_integrity_report_rejects_malformed_scan_unit_meta_json(tmp_path):
    shared_root = tmp_path / "shared"
    manifest_root = shared_root / "manifests"
    scan_dir = manifest_root / "job" / "scan-units" / "1"
    scan_dir.mkdir(parents=True)
    scan_manifest = scan_dir / "manifest.jsonl"
    scan_manifest.write_text(
        ManifestItem(
            input_path=str(shared_root / "input" / "a.pdf"),
            relative_path="a.pdf",
            size_bytes=10,
            mtime_ns=1,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )
    scan_meta = scan_dir / "manifest.meta.json"
    scan_meta.write_text("{", encoding="utf-8")
    shard_dir = scan_dir / "shards"
    shard_dir.mkdir()
    shard = shard_dir / "shard-000001.jsonl"
    shard.write_text(scan_manifest.read_text(encoding="utf-8"), encoding="utf-8")
    client, _ = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "shared_paths": [
                    {
                        "path": str(shared_root),
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ]
            },
        },
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(shared_root / "input"),
            "output_dir": str(shared_root / "output"),
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1000,
        },
    ).json()
    unit = client.post("/api/scan-units/claim?server_id=server-a").json()
    client.post(
        f"/api/scan-units/{unit['id']}/complete",
        json={
            "manifest_path": str(scan_manifest),
            "meta_path": str(scan_meta),
            "file_count": 1,
            "total_bytes": 10,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": str(shard),
                    "file_count": 1,
                }
            ],
        },
    )

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["bad_scan_units"][0]["reason"] == "meta_file_malformed"

    freeze_report = client.get(f"/api/jobs/{job['id']}/manifest/freeze-report").json()
    assert freeze_report["report"]["integrity_ok"] is False
    assert freeze_report["report"]["integrity_bad_scan_unit_count"] == 1
    assert freeze_report["report"]["integrity_issue_samples"] == [
        {
            "kind": "scan_unit",
            "scan_unit_id": unit["id"],
            "path": str(shared_root / "input"),
            "manifest_path": str(scan_meta),
            "expected_file_count": 1,
            "actual_file_count": 1,
            "reason": "meta_file_malformed",
        }
    ]


def test_manifest_integrity_report_checks_scan_unit_meta_file_count(tmp_path):
    shared_root = tmp_path / "shared"
    manifest_root = shared_root / "manifests"
    scan_dir = manifest_root / "job" / "scan-units" / "1"
    scan_dir.mkdir(parents=True)
    scan_manifest = scan_dir / "manifest.jsonl"
    scan_manifest.write_text(
        ManifestItem(
            input_path=str(shared_root / "input" / "a.pdf"),
            relative_path="a.pdf",
            size_bytes=10,
            mtime_ns=1,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )
    scan_meta = scan_dir / "manifest.meta.json"
    scan_meta.write_text(json.dumps({"file_count": 2}), encoding="utf-8")
    shard_dir = scan_dir / "shards"
    shard_dir.mkdir()
    shard = shard_dir / "shard-000001.jsonl"
    shard.write_text(scan_manifest.read_text(encoding="utf-8"), encoding="utf-8")
    client, _ = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "shared_paths": [
                    {
                        "path": str(shared_root),
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ]
            },
        },
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(shared_root / "input"),
            "output_dir": str(shared_root / "output"),
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1000,
        },
    ).json()
    unit = client.post("/api/scan-units/claim?server_id=server-a").json()
    client.post(
        f"/api/scan-units/{unit['id']}/complete",
        json={
            "manifest_path": str(scan_manifest),
            "meta_path": str(scan_meta),
            "file_count": 1,
            "total_bytes": 10,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": str(shard),
                    "file_count": 1,
                }
            ],
        },
    )

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["bad_scan_units"][0]["reason"] == "meta_file_count_mismatch"
    assert report["bad_scan_units"][0]["expected_file_count"] == 1
    assert report["bad_scan_units"][0]["actual_file_count"] == 2
    freeze_report = client.get(f"/api/jobs/{job['id']}/manifest/freeze-report").json()
    assert freeze_report["report"]["integrity_ok"] is False
    assert freeze_report["report"]["integrity_bad_scan_unit_count"] == 1


def test_manifest_integrity_report_checks_scan_unit_meta_total_bytes(tmp_path):
    shared_root = tmp_path / "shared"
    manifest_root = shared_root / "manifests"
    scan_dir = manifest_root / "job" / "scan-units" / "1"
    scan_dir.mkdir(parents=True)
    scan_manifest = scan_dir / "manifest.jsonl"
    scan_manifest.write_text(
        ManifestItem(
            input_path=str(shared_root / "input" / "a.pdf"),
            relative_path="a.pdf",
            size_bytes=10,
            mtime_ns=1,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )
    scan_meta = scan_dir / "manifest.meta.json"
    scan_meta.write_text(json.dumps({"total_bytes": 11}), encoding="utf-8")
    shard_dir = scan_dir / "shards"
    shard_dir.mkdir()
    shard = shard_dir / "shard-000001.jsonl"
    shard.write_text(scan_manifest.read_text(encoding="utf-8"), encoding="utf-8")
    client, _ = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "shared_paths": [
                    {
                        "path": str(shared_root),
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ]
            },
        },
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(shared_root / "input"),
            "output_dir": str(shared_root / "output"),
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1000,
        },
    ).json()
    unit = client.post("/api/scan-units/claim?server_id=server-a").json()
    client.post(
        f"/api/scan-units/{unit['id']}/complete",
        json={
            "manifest_path": str(scan_manifest),
            "meta_path": str(scan_meta),
            "file_count": 1,
            "total_bytes": 10,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": str(shard),
                    "file_count": 1,
                }
            ],
        },
    )

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["bad_scan_units"][0]["reason"] == "meta_total_bytes_mismatch"
    freeze_report = client.get(f"/api/jobs/{job['id']}/manifest/freeze-report").json()
    assert freeze_report["report"]["integrity_ok"] is False
    assert freeze_report["report"]["integrity_bad_scan_unit_count"] == 1


def test_manifest_integrity_report_rejects_non_object_scan_unit_meta_json(tmp_path):
    shared_root = tmp_path / "shared"
    manifest_root = shared_root / "manifests"
    scan_dir = manifest_root / "job" / "scan-units" / "1"
    scan_dir.mkdir(parents=True)
    scan_manifest = scan_dir / "manifest.jsonl"
    scan_manifest.write_text(
        ManifestItem(
            input_path=str(shared_root / "input" / "a.pdf"),
            relative_path="a.pdf",
            size_bytes=10,
            mtime_ns=1,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )
    scan_meta = scan_dir / "manifest.meta.json"
    scan_meta.write_text("[]", encoding="utf-8")
    shard_dir = scan_dir / "shards"
    shard_dir.mkdir()
    shard = shard_dir / "shard-000001.jsonl"
    shard.write_text(scan_manifest.read_text(encoding="utf-8"), encoding="utf-8")
    client, _ = make_client_with_session(tmp_path)
    register_server(client)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "shared_paths": [
                    {
                        "path": str(shared_root),
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ]
            },
        },
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": str(shared_root / "input"),
            "output_dir": str(shared_root / "output"),
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": str(manifest_root),
            "target_files_per_shard": 1000,
        },
    ).json()
    unit = client.post("/api/scan-units/claim?server_id=server-a").json()
    client.post(
        f"/api/scan-units/{unit['id']}/complete",
        json={
            "manifest_path": str(scan_manifest),
            "meta_path": str(scan_meta),
            "file_count": 1,
            "total_bytes": 10,
            "child_paths": [],
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": str(shard),
                    "file_count": 1,
                }
            ],
        },
    )

    report = client.get(f"/api/jobs/{job['id']}/manifest/integrity").json()

    assert report["ok"] is False
    assert report["bad_scan_units"][0]["reason"] == "meta_file_malformed"
    freeze_report = client.get(f"/api/jobs/{job['id']}/manifest/freeze-report").json()
    assert freeze_report["report"]["integrity_ok"] is False
    assert freeze_report["report"]["integrity_bad_scan_unit_count"] == 1


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
