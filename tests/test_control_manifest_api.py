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


@pytest.mark.parametrize(
    "transaction_mode",
    ["none", "explicit", "autobegin"],
)
def test_model_profile_rejects_unknown_parser_extra_arg(
    tmp_path,
    transaction_mode,
):
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)

    with session_factory() as session:
        commits = []
        rollbacks = []
        event.listen(
            session,
            "after_commit",
            lambda current: commits.append(1),
        )
        event.listen(
            session,
            "after_rollback",
            lambda current: rollbacks.append(1),
        )
        outer_profile = None
        if transaction_mode == "explicit":
            session.begin()
        elif transaction_mode == "autobegin":
            assert session.get(ModelProfile, "missing") is None
        if transaction_mode != "none":
            outer_profile = ModelProfile(
                id=f"outer-{transaction_mode}",
                label="Outer transaction",
                engine="dotsocr",
            )
            session.add(outer_profile)
            expected_error = ModelProfileTransactionError
            expected_message = f"^{ACTIVE_TRANSACTION_ERROR}$"
        else:
            expected_error = ValueError
            expected_message = "unknown model profile extra_args key"

        with pytest.raises(expected_error, match=expected_message):
            upsert_model_profile(
                session,
                "bad",
                ModelProfileRequest(
                    label="Bad",
                    engine="dotsocr",
                    extra_args={"not_a_parser_option": True},
                ),
            )
        if transaction_mode != "none":
            assert session.in_transaction() is True
            assert outer_profile in session.new
            assert commits == []
            assert rollbacks == []
            session.rollback()
            assert commits == []
            assert rollbacks == [1]

    if transaction_mode != "none":
        with session_factory() as session:
            assert session.get(
                ModelProfile,
                f"outer-{transaction_mode}",
            ) is None
            assert session.get(ModelProfile, "bad") is None


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
    monkeypatch.setattr(
        manifest_use_cases,
        "SCAN_UNIT_CLAIM_BATCH_SIZE",
        2,
    )
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
