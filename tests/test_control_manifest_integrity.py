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
