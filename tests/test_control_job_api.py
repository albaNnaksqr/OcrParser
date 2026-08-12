from datetime import timedelta


import json


import pytest


from fastapi.testclient import TestClient


from sqlalchemy import text


from ocr_platform.control.app import create_app


from ocr_platform.control import database as control_database


from ocr_platform.control import limits as control_limits


from ocr_platform.control.database import create_session_factory, init_db


from ocr_platform.control.domains.workers import (
    identity as worker_identity,
)


from ocr_platform.control.domains.workers.preflight import (
    database_migration_preflight_issue,
)


from ocr_platform.control.models import (
    Job,
    JobEvent,
    JobFile,
    JobLog,
    Manifest,
    ModelProfile,
    ModelProfileCertification,
    ScanUnit,
    Server,
    WorkShard,
    utcnow,
)


def make_client(tmp_path):
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    app = create_app(session_factory=session_factory)
    return TestClient(app)


def make_client_with_session(tmp_path):
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    app = create_app(session_factory=session_factory)
    return TestClient(app), session_factory


def test_job_summary_endpoint_supports_pagination_and_status_filter(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    first = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/first",
            "output_dir": "/shared/out/first",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    second = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/second",
            "output_dir": "/shared/out/second",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/third",
            "output_dir": "/shared/out/third",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    )

    page = client.get("/api/jobs/summary?limit=1&offset=1")

    assert page.status_code == 200
    assert len(page.json()) == 1
    assert page.json()[0]["id"] == second["id"]

    filtered = client.get("/api/jobs/summary?status=queued&limit=10")

    assert filtered.status_code == 200
    assert {item["id"] for item in filtered.json()} >= {first["id"], second["id"]}


def test_job_summary_page_endpoint_returns_total_and_has_more(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    first = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/first",
            "output_dir": "/shared/out/first",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    second = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/second",
            "output_dir": "/shared/out/second",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/third",
            "output_dir": "/shared/out/third",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    )

    page = client.get("/api/jobs/summary/page?limit=1&offset=1")

    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 3
    assert payload["limit"] == 1
    assert payload["offset"] == 1
    assert payload["has_more"] is True
    assert [item["id"] for item in payload["items"]] == [second["id"]]

    filtered = client.get("/api/jobs/summary/page?status=queued&limit=10&offset=0")

    assert filtered.status_code == 200
    filtered_payload = filtered.json()
    assert filtered_payload["total"] == 3
    assert filtered_payload["has_more"] is False
    assert {item["id"] for item in filtered_payload["items"]} >= {first["id"], second["id"]}


def test_job_summary_page_status_filter_is_case_and_space_insensitive(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    first = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/first",
            "output_dir": "/shared/out/first",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    second = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/second",
            "output_dir": "/shared/out/second",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()

    filtered = client.get("/api/jobs/summary/page?status=%20QUEUED%20&limit=10")

    assert filtered.status_code == 200
    payload = filtered.json()
    assert payload["total"] == 2
    assert {item["id"] for item in payload["items"]} == {first["id"], second["id"]}


def test_job_detail_list_endpoint_supports_pagination_and_status_filter(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    first = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/first",
            "output_dir": "/shared/out/first",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    second = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/second",
            "output_dir": "/shared/out/second",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/third",
            "output_dir": "/shared/out/third",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    )

    page = client.get("/api/jobs?limit=1&offset=1")

    assert page.status_code == 200
    assert len(page.json()) == 1
    assert page.json()[0]["id"] == second["id"]

    filtered = client.get("/api/jobs?status=queued&limit=10")

    assert filtered.status_code == 200
    assert {item["id"] for item in filtered.json()} >= {first["id"], second["id"]}


def test_job_detail_page_status_filter_is_case_and_space_insensitive(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    first = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/first",
            "output_dir": "/shared/out/first",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    second = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/second",
            "output_dir": "/shared/out/second",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()

    filtered = client.get("/api/jobs/page?status=%20QUEUED%20&limit=10")

    assert filtered.status_code == 200
    payload = filtered.json()
    assert payload["total"] == 2
    assert {item["id"] for item in payload["items"]} == {first["id"], second["id"]}


def test_legacy_job_list_status_filter_is_case_and_space_insensitive(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    first = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/first",
            "output_dir": "/shared/out/first",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    second = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/second",
            "output_dir": "/shared/out/second",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()

    filtered = client.get("/api/jobs?status=%20QUEUED%20&limit=10")

    assert filtered.status_code == 200
    assert {item["id"] for item in filtered.json()} == {first["id"], second["id"]}


@pytest.mark.parametrize(
    "path",
    [
        "/api/jobs?status=runningg",
        "/api/jobs/page?status=runningg",
        "/api/jobs/summary?status=runningg",
        "/api/jobs/summary/page?status=runningg",
    ],
)
def test_job_list_status_filters_reject_unknown_status(tmp_path, path):
    client = make_client(tmp_path)

    response = client.get(path)

    assert response.status_code == 400
    assert "unknown job status filter" in response.json()["detail"]
    assert "queued" in response.json()["detail"]


def test_job_detail_page_endpoint_returns_total_and_has_more(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    first = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/first",
            "output_dir": "/shared/out/first",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    second = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/second",
            "output_dir": "/shared/out/second",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    third = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in/third",
            "output_dir": "/shared/out/third",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()

    page = client.get("/api/jobs/page?limit=2&offset=1")

    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 3
    assert payload["limit"] == 2
    assert payload["offset"] == 1
    assert payload["has_more"] is False
    assert [item["id"] for item in payload["items"]] == [second["id"], first["id"]]

    filtered = client.get("/api/jobs/page?status=queued&limit=1&offset=0")

    assert filtered.status_code == 200
    filtered_payload = filtered.json()
    assert filtered_payload["total"] == 3
    assert filtered_payload["has_more"] is True
    assert [item["id"] for item in filtered_payload["items"]] == [third["id"]]


def test_job_summary_reports_lifecycle_stage_for_production_views(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    normal_job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in2",
            "output_dir": "/shared/out2",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    client.post("/api/agents/server-a/next-job")
    assert client.get(f"/api/jobs/{normal_job['id']}/summary").json()["lifecycle_stage"] == "running"

    client.post(f"/api/jobs/{normal_job['id']}/request-stop")
    assert client.get(f"/api/jobs/{normal_job['id']}/summary").json()["lifecycle_stage"] == "draining"

    scan_job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "assigned_server_id": "server-a",
        },
    ).json()
    assert client.get(f"/api/jobs/{scan_job['id']}/summary").json()["lifecycle_stage"] == "scanning"

    with session_factory() as session:
        sharding_job = client.post(
            "/api/jobs",
            json={
                "input_dir": "/shared/in-sharding",
                "output_dir": "/shared/out-sharding",
                "engine": "dotsocr",
                "input_mode": "remote_folder_snapshot",
                "assigned_server_id": "server-a",
            },
        ).json()
        job = session.get(Job, sharding_job["id"])
        job.status = "running"
        session.add(
            Manifest(
                job_id=job.id,
                input_mode="remote_folder_snapshot",
                input_root="/shared/in-sharding",
                manifest_path="/shared/manifest/sharding/manifest.jsonl",
                file_count=10,
                total_bytes=100,
                status="ready",
            )
        )
        session.commit()

    sharding_summary = client.get(f"/api/jobs/{sharding_job['id']}/summary").json()
    assert sharding_summary["scan_status"] == "done"
    assert sharding_summary["total_shards"] == 0
    assert sharding_summary["lifecycle_stage"] == "sharding"

    with session_factory() as session:
        retry_job = client.post(
            "/api/jobs",
            json={
                "input_dir": "/shared/in3",
                "output_dir": "/shared/out3",
                "engine": "dotsocr",
                "input_mode": "remote_folder_snapshot",
                "assigned_server_id": "server-a",
            },
        ).json()
        job = session.get(Job, retry_job["id"])
        manifest = Manifest(
            job_id=job.id,
            input_mode="remote_folder_snapshot",
            input_root="/shared/in3",
            manifest_path="/shared/manifest/manifest.jsonl",
            file_count=10,
            total_bytes=100,
        )
        session.add(manifest)
        session.flush()
        session.add(
            WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=1,
                shard_path="/shared/manifest/shard-000001.jsonl",
                status="stale",
                file_count=10,
                attempt_count=1,
            )
        )
        job.status = "running"
        session.commit()
    assert client.get(f"/api/jobs/{retry_job['id']}/summary").json()["lifecycle_stage"] == "recovering"


def test_server_list_marks_stale_heartbeat_offline(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    with session_factory() as session:
        server = session.get(Server, "server-a")
        server.status = "busy"
        server.last_heartbeat_at = utcnow() - timedelta(seconds=300)
        session.commit()

    resp = client.get("/api/servers")

    assert resp.status_code == 200
    server = resp.json()[0]
    assert server["status"] == "offline"
    assert server["is_stale"] is True


def test_server_eligibility_reports_shared_path_access(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
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
                        "writable": False,
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
                        "path": "/other",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                    }
                ]
            },
        },
    )

    resp = client.get("/api/servers/eligibility", params={"input_dir": "/shared/project/a"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["input_dir"] == "/shared/project/a"
    assert payload["total_servers"] == 2
    assert payload["eligible_servers"] == 1
    by_server = {item["server_id"]: item for item in payload["servers"]}
    assert by_server["server-a"]["can_access"] is True
    assert by_server["server-a"]["matched_path"] == "/shared"
    assert by_server["server-a"]["reason"] == "ok"
    assert by_server["server-b"]["can_access"] is False
    assert by_server["server-b"]["reason"] == "no_matching_shared_root"


def test_distributed_job_defaults_manifest_root_under_shared_root(tmp_path):
    client = make_client(tmp_path)
    for server_id in ["server-a", "server-b"]:
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

    resp = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/project/input",
            "output_dir": "/shared/project/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "allowed_server_ids": ["server-a", "server-b"],
        },
    )

    assert resp.status_code == 200
    job = resp.json()
    assert job["manifest_root"] == "/shared/.ocr_platform/manifests"


def test_delete_stale_server_archives_it_from_current_worker_lists(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
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
                    }
                ]
            },
        },
    )
    with session_factory() as session:
        server = session.get(Server, "server-a")
        server.last_heartbeat_at = utcnow() - timedelta(seconds=300)
        session.commit()

    resp = client.delete("/api/servers/server-a")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "archived": True}
    assert [server["id"] for server in client.get("/api/servers").json()] == []
    eligibility = client.get("/api/servers/eligibility", params={"input_dir": "/shared/in"}).json()
    assert eligibility["total_servers"] == 0
    with session_factory() as session:
        assert session.get(Server, "server-a").archived_at is not None


def test_delete_online_server_is_rejected(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )

    resp = client.delete("/api/servers/server-a")

    assert resp.status_code == 409
    assert client.get("/api/servers").json()[0]["id"] == "server-a"


def test_delete_online_server_with_queued_job_is_rejected(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    )

    resp = client.delete("/api/servers/server-a")

    assert resp.status_code == 409
    assert client.get("/api/servers").json()[0]["id"] == "server-a"


def test_archived_server_heartbeat_restores_current_worker(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    with session_factory() as session:
        server = session.get(Server, "server-a")
        server.last_heartbeat_at = utcnow() - timedelta(seconds=300)
        session.commit()
    assert client.delete("/api/servers/server-a").status_code == 200

    heartbeat = client.post(
        "/api/servers/server-a/heartbeat",
        json={"status": "idle", "capabilities": {"shared_roots": ["/shared"]}},
    )

    assert heartbeat.status_code == 200
    assert client.get("/api/servers").json()[0]["id"] == "server-a"
    with session_factory() as session:
        assert session.get(Server, "server-a").archived_at is None


def test_create_job_rejects_archived_assigned_server(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    with session_factory() as session:
        server = session.get(Server, "server-a")
        server.last_heartbeat_at = utcnow() - timedelta(seconds=300)
        session.commit()
    assert client.delete("/api/servers/server-a").status_code == 200

    resp = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    )

    assert resp.status_code == 400


def test_job_events_update_file_progress(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    job_id = job["id"]

    event_resp = client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "page_done",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a",
                "page_no": 1,
                "status": "success",
            },
        },
    )
    assert event_resp.status_code == 200

    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["files"][0]["file_path"] == "/shared/in/a.pdf"
    assert detail["files"][0]["done_pages"] == 1


def test_job_detail_rows_are_capped(monkeypatch, tmp_path):
    monkeypatch.setattr(control_limits, "JOB_FILE_DETAIL_LIMIT", 2)
    monkeypatch.setattr(control_limits, "JOB_EVENT_DETAIL_LIMIT", 3)
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()

    for index in range(5):
        client.post(
            f"/api/jobs/{job['id']}/events",
            json={
                "type": "file_done",
                "payload": {
                    "file_path": f"/shared/in/{index}.pdf",
                    "filename": f"{index}.pdf",
                },
            },
        )

    with session_factory() as session:
        assert session.query(JobFile).filter_by(job_id=job["id"]).count() == 2
        assert session.query(JobEvent).filter_by(job_id=job["id"]).count() == 3


def test_job_file_detail_pruning_prioritizes_failed_files(monkeypatch, tmp_path):
    monkeypatch.setattr(control_limits, "JOB_FILE_DETAIL_LIMIT", 2)
    monkeypatch.setattr(control_limits, "JOB_EVENT_DETAIL_LIMIT", 10)
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()

    client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "file_failed",
            "payload": {
                "file_path": "/shared/in/failed.pdf",
                "filename": "failed.pdf",
                "error": "model timeout",
            },
        },
    )
    for index in range(3):
        client.post(
            f"/api/jobs/{job['id']}/events",
            json={
                "type": "file_done",
                "payload": {
                    "file_path": f"/shared/in/success-{index}.pdf",
                    "filename": f"success-{index}.pdf",
                },
            },
        )

    with session_factory() as session:
        rows = (
            session.query(JobFile)
            .filter_by(job_id=job["id"])
            .order_by(JobFile.file_path)
            .all()
        )
        assert [(row.file_path, row.status) for row in rows] == [
            ("/shared/in/failed.pdf", "failed"),
            ("/shared/in/success-2.pdf", "success"),
        ]


def test_job_event_detail_pruning_prioritizes_failure_and_terminal_events(monkeypatch, tmp_path):
    monkeypatch.setattr(control_limits, "JOB_FILE_DETAIL_LIMIT", 10)
    monkeypatch.setattr(control_limits, "JOB_EVENT_DETAIL_LIMIT", 3)
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()

    events = [
        {"type": "file_failed", "payload": {"file_path": "/shared/in/failed.pdf", "error": "model timeout"}},
        {"type": "page_done", "payload": {"file_path": "/shared/in/a.pdf", "page_no": 1, "status": "success"}},
        {"type": "page_done", "payload": {"file_path": "/shared/in/a.pdf", "page_no": 2, "status": "success"}},
        {"type": "file_done", "payload": {"file_path": "/shared/in/a.pdf", "status": "success"}},
        {"type": "runtime_metrics", "payload": {"runtime": {"api_inflight": 3}}},
    ]
    for event in events:
        client.post(f"/api/jobs/{job['id']}/events", json=event)

    with session_factory() as session:
        rows = (
            session.query(JobEvent)
            .filter_by(job_id=job["id"])
            .order_by(JobEvent.event_type, JobEvent.id)
            .all()
        )
        assert [(row.event_type, row.file_path) for row in rows] == [
            ("file_done", "/shared/in/a.pdf"),
            ("file_failed", "/shared/in/failed.pdf"),
            ("runtime_metrics", None),
        ]


def test_page_done_counts_distinct_completed_pages_not_highest_page_number(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    job_id = job["id"]

    def post_page(page_no):
        return client.post(
            f"/api/jobs/{job_id}/events",
            json={
                "type": "page_done",
                "payload": {
                    "file_path": "/shared/in/a.pdf",
                    "filename": "a",
                    "page_no": page_no,
                    "status": "success",
                },
            },
        )

    assert post_page(10).status_code == 200
    assert client.get(f"/api/jobs/{job_id}").json()["files"][0]["done_pages"] == 1

    assert post_page(2).status_code == 200
    assert client.get(f"/api/jobs/{job_id}").json()["files"][0]["done_pages"] == 2

    assert post_page(10).status_code == 200
    assert client.get(f"/api/jobs/{job_id}").json()["files"][0]["done_pages"] == 2


def test_request_stop_sets_flag(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()

    claim_resp = client.post("/api/agents/server-a/next-job")
    assert claim_resp.status_code == 200
    assert claim_resp.json()["status"] == "running"

    resp = client.post(f"/api/jobs/{job['id']}/request-stop")
    assert resp.status_code == 200
    assert resp.json()["stop_requested"] is True
    assert resp.json()["status"] == "stopping"


def test_request_stop_finalizes_queued_job_without_agent_claim(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()

    resp = client.post(f"/api/jobs/{job['id']}/request-stop")
    claim_resp = client.post("/api/agents/server-a/next-job")

    assert resp.status_code == 200
    assert resp.json()["stop_requested"] is True
    assert resp.json()["status"] == "stopped"
    assert claim_resp.status_code == 200
    assert claim_resp.json() is None


def test_archive_stale_server_stops_assigned_queued_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(
        worker_identity,
        "SERVER_STALE_AFTER_SECONDS",
        1,
    )
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()

    with session_factory() as session:
        server = session.get(Server, "server-a")
        server.last_heartbeat_at = utcnow() - timedelta(seconds=10)
        session.commit()

    archive_resp = client.delete("/api/servers/server-a")
    job_resp = client.get(f"/api/jobs/{job['id']}")

    assert archive_resp.status_code == 200
    assert archive_resp.json() == {"ok": True, "archived": True}
    assert job_resp.json()["status"] == "stopped"
    assert job_resp.json()["stop_requested"] is True


def test_request_stop_finalizes_queued_static_shard_job(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
            "assigned_server_id": "server-a",
        },
    ).json()
    client.post(
        f"/api/jobs/{job['id']}/manifest",
        json={
            "input_mode": "remote_folder_snapshot",
            "input_root": "/shared/in",
            "manifest_path": "/shared/manifests/job/manifest.jsonl",
            "meta_path": "/shared/manifests/job/manifest.meta.json",
            "file_count": 1,
            "shards": [
                {
                    "shard_index": 1,
                    "shard_path": "/shared/manifests/job/shards/shard-000001.jsonl",
                    "file_count": 1,
                }
            ],
        },
    )

    stop_resp = client.post(f"/api/jobs/{job['id']}/request-stop")
    claim_resp = client.post("/api/jobs/{}/shards/claim".format(job["id"]), params={"server_id": "server-a"})

    assert stop_resp.status_code == 200
    assert stop_resp.json()["status"] == "stopped"
    assert stop_resp.json()["stop_requested"] is True
    assert claim_resp.status_code == 200
    assert claim_resp.json() is None
    with session_factory() as session:
        shard = session.query(WorkShard).filter_by(job_id=job["id"]).one()
        assert shard.status == "stopped"


def test_request_stop_finalizes_queued_scan_unit_job(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/manifests",
        },
    ).json()

    stop_resp = client.post(f"/api/jobs/{job['id']}/request-stop")
    claim_resp = client.post("/api/scan-units/claim", params={"server_id": "server-a"})

    assert stop_resp.status_code == 200
    assert stop_resp.json()["status"] == "stopped"
    assert stop_resp.json()["stop_requested"] is True
    assert claim_resp.status_code == 200
    assert claim_resp.json() is None
    with session_factory() as session:
        unit = session.query(ScanUnit).filter_by(job_id=job["id"]).one()
        assert unit.status == "stopped"


def test_job_summary_deduplicates_replayed_file_events(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    file_payload = {
        "file_path": "/shared/in/a.pdf",
        "filename": "a",
        "status": "success",
    }

    for _ in range(2):
        client.post(
            f"/api/jobs/{job['id']}/events",
            json={"type": "file_started", "payload": file_payload},
        )
        client.post(
            f"/api/jobs/{job['id']}/events",
            json={"type": "page_done", "payload": file_payload | {"page_no": 1}},
        )
        client.post(
            f"/api/jobs/{job['id']}/events",
            json={"type": "file_done", "payload": file_payload},
        )

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["total_files"] == 1
    assert summary["completed_files"] == 1
    assert summary["completed_pages"] == 1
    assert summary["progress_percent"] == 100.0


def test_delete_terminal_job_removes_job_and_progress_rows(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    job_id = job["id"]

    client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "page_done",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a",
                "page_no": 1,
                "status": "success",
            },
        },
    )
    client.post(
        f"/api/jobs/{job_id}/logs",
        json={"server_id": "server-a", "stream": "stdout", "line": "started"},
    )
    client.post(f"/api/jobs/{job_id}/events", json={"type": "job_done"})

    resp = client.delete(f"/api/jobs/{job_id}")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    with session_factory() as session:
        assert session.query(JobFile).count() == 0
        assert session.query(JobEvent).count() == 0
        assert session.query(JobLog).count() == 0


def test_archive_terminal_job_hides_it_from_default_job_lists_but_preserves_rows(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    job_id = job["id"]

    client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "file_done",
            "payload": {"file_path": "/shared/in/a.pdf", "filename": "a.pdf"},
        },
    )
    client.post(f"/api/jobs/{job_id}/events", json={"type": "job_done"})

    resp = client.post(f"/api/jobs/{job_id}/archive")

    assert resp.status_code == 200
    assert resp.json()["archived"] is True
    assert resp.json()["job_id"] == job_id
    assert all(item["id"] != job_id for item in client.get("/api/jobs").json())
    assert all(item["id"] != job_id for item in client.get("/api/jobs/summary").json())

    archived_jobs = client.get("/api/jobs?include_archived=true").json()
    archived_summaries = client.get("/api/jobs/summary?include_archived=true").json()

    assert [item["id"] for item in archived_jobs] == [job_id]
    assert archived_jobs[0]["archived_at"] is not None
    assert [item["id"] for item in archived_summaries] == [job_id]
    assert archived_summaries[0]["archived_at"] is not None
    with session_factory() as session:
        assert session.get(Job, job_id) is not None
        assert session.query(JobFile).filter_by(job_id=job_id).count() == 1
        assert session.query(JobEvent).filter_by(job_id=job_id).count() >= 2
