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


def test_archive_running_job_is_rejected(tmp_path):
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
    client.post("/api/agents/server-a/next-job")

    resp = client.post(f"/api/jobs/{job['id']}/archive")

    assert resp.status_code == 409
    assert "Only succeeded, failed, or stopped jobs can be archived." in resp.json()["detail"]


def test_delete_running_job_is_rejected(tmp_path):
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
    client.post("/api/agents/server-a/next-job")

    resp = client.delete(f"/api/jobs/{job['id']}")

    assert resp.status_code == 409
    assert client.get(f"/api/jobs/{job['id']}").status_code == 200


def test_job_summary_returns_aggregate_progress_without_file_rows(tmp_path):
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

    client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "file_started",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a.pdf",
                "total_pages": 3,
            },
        },
    )
    client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "page_done",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a.pdf",
                "page_no": 1,
                "status": "success",
            },
        },
    )
    client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "file_done",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a.pdf",
                "output_path": "/shared/out/a.md",
            },
        },
    )
    client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "file_failed",
            "payload": {
                "file_path": "/shared/in/b.pdf",
                "filename": "b.pdf",
                "error": "model timeout",
            },
        },
    )

    resp = client.get("/api/jobs/summary")

    assert resp.status_code == 200
    summary = resp.json()[0]
    assert summary["id"] == job_id
    assert "files" not in summary
    assert summary["total_files"] == 2
    assert summary["completed_files"] == 1
    assert summary["failed_files"] == 1
    assert summary["skipped_files"] == 0
    assert summary["total_pages"] == 3
    assert summary["completed_pages"] == 1
    assert summary["progress_percent"] == 33.33
    assert summary["last_event_at"] is not None
    assert summary["is_stale"] is False


def test_job_summary_uses_counters_when_detail_rows_are_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(control_limits, "JOB_FILE_DETAIL_LIMIT", 0)
    monkeypatch.setattr(control_limits, "JOB_EVENT_DETAIL_LIMIT", 0)
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

    events = [
        {
            "type": "file_started",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a.pdf",
                "total_pages": 2,
            },
        },
        {
            "type": "page_done",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a.pdf",
                "page_no": 1,
                "status": "success",
            },
        },
        {
            "type": "page_done",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a.pdf",
                "page_no": 2,
                "status": "success_fallback_image",
            },
        },
        {
            "type": "file_done",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a.pdf",
                "output_path": "/shared/out/a.md",
            },
        },
        {
            "type": "file_failed",
            "payload": {
                "file_path": "/shared/in/b.pdf",
                "filename": "b.pdf",
                "error": "model timeout",
            },
        },
    ]
    for event in events:
        assert client.post(f"/api/jobs/{job_id}/events", json=event).status_code == 200

    summary = client.get(f"/api/jobs/{job_id}/summary").json()

    assert summary["total_files"] == 2
    assert summary["completed_files"] == 1
    assert summary["failed_files"] == 1
    assert summary["completed_pages"] == 2
    assert summary["total_pages"] == 2
    assert summary["degraded_pages"] == 1
    assert summary["quality_flags"] == ["image_fallback"]
    with session_factory() as session:
        assert session.query(JobFile).filter_by(job_id=job_id).count() == 0
        assert session.query(JobEvent).filter_by(job_id=job_id).count() == 0


def test_manifest_scan_progress_survives_when_raw_event_details_are_disabled(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(control_limits, "JOB_EVENT_DETAIL_LIMIT", 0)
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

    response = client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "manifest_scan_progress",
            "payload": {
                "status": "running",
                "scanned_files": 100,
                "estimated_total_files": 250,
                "remaining_files": 150,
                "estimated_remaining_seconds": 30,
            },
        },
    )

    assert response.status_code == 200
    second_response = client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "manifest_scan_progress",
            "payload": {
                "status": "running",
                "scanned_files": 150,
                "estimated_total_files": 250,
                "remaining_files": 100,
                "estimated_remaining_seconds": 20,
            },
        },
    )

    assert second_response.status_code == 200
    summary = client.get(f"/api/jobs/{job['id']}/summary").json()
    assert summary["scan_status"] == "running"
    assert summary["scan_progress_files"] == 150
    assert summary["scan_estimated_total_files"] == 250
    assert summary["scan_remaining_files"] == 100
    assert summary["scan_eta_seconds"] == 20
    with session_factory() as session:
        rows = session.query(JobEvent).filter_by(job_id=job["id"]).all()
        assert [row.event_type for row in rows] == ["manifest_scan_progress"]
        payload = json.loads(rows[0].payload_json)
        assert payload["scanned_files"] == 150


def test_recent_failed_files_uses_counter_samples_when_detail_rows_are_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(control_limits, "JOB_FILE_DETAIL_LIMIT", 0)
    monkeypatch.setattr(control_limits, "JOB_EVENT_DETAIL_LIMIT", 0)
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

    assert client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "file_failed",
            "payload": {
                "file_path": "/shared/in/b.pdf",
                "filename": "b.pdf",
                "error": "model timeout",
                "failure_category": "api_timeout",
            },
        },
    ).status_code == 200

    failed = client.get(f"/api/jobs/{job_id}/recent-files?kind=failed&limit=3").json()

    assert failed == [
        {
            "file_path": "/shared/in/b.pdf",
            "filename": "b.pdf",
            "status": "failed",
            "total_pages": None,
            "done_pages": 0,
            "output_path": None,
            "error": "model timeout",
            "failure_category": "api_timeout",
        }
    ]
    with session_factory() as session:
        assert session.query(JobFile).filter_by(job_id=job_id).count() == 0
        assert session.query(JobEvent).filter_by(job_id=job_id).count() == 0


def test_recent_errors_page_uses_counter_samples_when_detail_rows_are_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(control_limits, "JOB_FILE_DETAIL_LIMIT", 0)
    monkeypatch.setattr(control_limits, "JOB_EVENT_DETAIL_LIMIT", 0)
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
    for filename, message in [
        ("a.pdf", "model request timed out after 180s"),
        ("b.pdf", "HTTP 503 from model server"),
        ("c.pdf", "Connection refused while connecting to model server"),
    ]:
        response = client.post(
            f"/api/jobs/{job_id}/events",
            json={
                "type": "file_failed",
                "payload": {
                    "file_path": f"/shared/in/{filename}",
                    "filename": filename,
                    "error": message,
                },
            },
        )
        assert response.status_code == 200

    page = client.get(
        f"/api/jobs/{job_id}/recent-errors/page",
        params={"failure_category": "model_unreachable", "limit": 2, "offset": 0},
    )

    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 1
    assert payload["limit"] == 2
    assert payload["offset"] == 0
    assert payload["has_more"] is False
    assert payload["items"][0]["source"] == "failed_file_sample"
    assert payload["items"][0]["event_type"] == "file_failed"
    assert payload["items"][0]["file_path"] == "/shared/in/c.pdf"
    assert payload["items"][0]["failure_category"] == "model_unreachable"
    assert payload["items"][0]["error"] == "Connection refused while connecting to model server"
    with session_factory() as session:
        assert session.query(JobFile).filter_by(job_id=job_id).count() == 0
        assert session.query(JobEvent).filter_by(job_id=job_id).count() == 0


def test_recent_errors_page_uses_counter_event_samples_for_job_failures_when_events_disabled(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(control_limits, "JOB_FILE_DETAIL_LIMIT", 0)
    monkeypatch.setattr(control_limits, "JOB_EVENT_DETAIL_LIMIT", 0)
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

    response = client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "job_failed",
            "payload": {
                "error": "process killed by signal 9",
                "failure_category": "process_killed",
            },
        },
    )

    assert response.status_code == 200
    page = client.get(
        f"/api/jobs/{job_id}/recent-errors/page",
        params={"failure_category": "process_killed", "limit": 10, "offset": 0},
    )

    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 1
    assert payload["items"][0]["source"] == "event_sample"
    assert payload["items"][0]["event_type"] == "job_failed"
    assert payload["items"][0]["failure_category"] == "process_killed"
    assert payload["items"][0]["error"] == "process killed by signal 9"
    with session_factory() as session:
        assert session.query(JobEvent).filter_by(job_id=job_id).count() == 0


def test_recent_errors_page_filters_event_rows_by_inferred_failure_category(tmp_path):
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
    response = client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "file_failed",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a.pdf",
                "error": "HTTP 503 from model server",
            },
        },
    )
    assert response.status_code == 200

    page = client.get(
        f"/api/jobs/{job_id}/recent-errors/page",
        params={"failure_category": "model_unavailable", "limit": 10, "offset": 0},
    )

    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 1
    assert payload["items"][0]["source"] == "job_event"
    assert payload["items"][0]["failure_category"] == "model_unavailable"
    assert payload["items"][0]["file_path"] == "/shared/in/a.pdf"


def test_failure_events_persist_inferred_failure_category_for_indexed_recent_errors(tmp_path):
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

    response = client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "file_failed",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a.pdf",
                "error": "HTTP 503 from model server",
            },
        },
    )

    assert response.status_code == 200
    with session_factory() as session:
        event = session.query(JobEvent).filter_by(job_id=job["id"]).one()
        assert event.failure_category == "model_unavailable"


def test_job_summary_uses_counter_failure_category_counts_when_detail_rows_are_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(control_limits, "JOB_FILE_DETAIL_LIMIT", 0)
    monkeypatch.setattr(control_limits, "JOB_EVENT_DETAIL_LIMIT", 0)
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

    events = [
        {"file_path": "/shared/in/a.pdf", "error": "model request timed out after 180s"},
        {"file_path": "/shared/in/b.pdf", "error": "HTTP 503 from model server"},
        {"file_path": "/shared/in/c.pdf", "error": "503 Service Unavailable from model server"},
    ]
    for payload in events:
        response = client.post(f"/api/jobs/{job_id}/events", json={"type": "file_failed", "payload": payload})
        assert response.status_code == 200

    summary = client.get(f"/api/jobs/{job_id}/summary").json()

    assert summary["failure_category_counts"] == {
        "api_timeout": 1,
        "model_unavailable": 2,
    }
    with session_factory() as session:
        assert session.query(JobFile).filter_by(job_id=job_id).count() == 0
        assert session.query(JobEvent).filter_by(job_id=job_id).count() == 0


def test_terminal_job_summary_throughput_is_stable_after_completion(tmp_path):
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

    client.post("/api/agents/server-a/next-job")
    client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "page_done",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a.pdf",
                "page_no": 1,
                "status": "success",
            },
        },
    )
    client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "file_done",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a.pdf",
            },
        },
    )
    client.post(f"/api/jobs/{job_id}/events", json={"type": "job_done"})

    first = client.get(f"/api/jobs/{job_id}/summary").json()
    second = client.get(f"/api/jobs/{job_id}/summary").json()

    assert first["status"] == "succeeded"
    assert first["pages_per_second"] == second["pages_per_second"]
    assert first["files_per_minute"] == second["files_per_minute"]


def test_recent_files_is_bounded_by_limit(tmp_path):
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
    with session_factory() as session:
        session.add_all(
            [
                JobFile(
                    job_id=job_id,
                    file_path=f"/shared/in/{index}.pdf",
                    filename=f"{index}.pdf",
                    status="failed" if index % 2 == 0 else "success",
                    done_pages=1,
                    total_pages=1,
                )
                for index in range(25)
            ]
        )
        session.commit()

    resp = client.get(f"/api/jobs/{job_id}/recent-files?kind=failed&limit=3")

    assert resp.status_code == 200
    files = resp.json()
    assert len(files) == 3
    assert all(item["status"] == "failed" for item in files)


def test_create_job_rejects_unknown_server(tmp_path):
    client = make_client(tmp_path)

    resp = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "missing",
        },
    )

    assert resp.status_code == 400


def test_unknown_job_endpoints_return_404(tmp_path):
    client = make_client(tmp_path)

    assert client.get("/api/jobs/missing").status_code == 404
    assert client.post("/api/jobs/missing/events", json={"type": "job_done"}).status_code == 404
    assert (
        client.post(
            "/api/jobs/missing/logs",
            json={"server_id": "server-a", "stream": "stdout", "line": "hello"},
        ).status_code
        == 404
    )
    assert client.post("/api/jobs/missing/request-stop").status_code == 404


def test_next_job_returns_null_when_none_queued(tmp_path):
    client = make_client(tmp_path)

    resp = client.post("/api/agents/server-a/next-job")

    assert resp.status_code == 200
    assert resp.json() is None


def test_job_response_includes_command_extra_args_and_files(tmp_path):
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
            "extra_args": {"save_page_layout": True},
        },
    ).json()

    assert job["extra_args"] == {"save_page_layout": True}
    assert job["command"] == []
    assert job["files"] == []


def test_job_terminal_events_update_status(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )

    expected = [
        ("job_done", "succeeded"),
        ("job_failed", "failed"),
        ("job_stopped", "stopped"),
    ]
    for event_type, status in expected:
        job = client.post(
            "/api/jobs",
            json={
                "input_dir": "/shared/in",
                "output_dir": "/shared/out",
                "engine": "dotsocr",
                "assigned_server_id": "server-a",
            },
        ).json()

        resp = client.post(f"/api/jobs/{job['id']}/events", json={"type": event_type})

        assert resp.status_code == 200
        assert resp.json()["status"] == status


def test_terminal_event_does_not_overwrite_existing_terminal_status(tmp_path):
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

    failed_resp = client.post(f"/api/jobs/{job['id']}/events", json={"type": "job_failed"})
    done_resp = client.post(f"/api/jobs/{job['id']}/events", json={"type": "job_done"})

    assert failed_resp.status_code == 200
    assert done_resp.status_code == 200
    assert done_resp.json()["status"] == "failed"
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "failed"


def test_job_failed_event_records_failure_category_and_error_message(tmp_path):
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

    resp = client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "job_failed",
            "payload": {
                "failure_category": "input_missing",
                "error": "input path does not exist",
            },
        },
    )
    summary = client.get(f"/api/jobs/{job['id']}/summary").json()
    detail = client.get(f"/api/jobs/{job['id']}").json()

    assert resp.status_code == 200
    assert resp.json()["failure_category"] == "input_missing"
    assert resp.json()["error_message"] == "input path does not exist"
    assert summary["failure_category"] == "input_missing"
    assert summary["error_message"] == "input path does not exist"
    assert detail["failure_category"] == "input_missing"
    assert detail["error_message"] == "input path does not exist"


@pytest.mark.parametrize(
    ("payload", "expected_category"),
    [
        ({"return_code": -9}, "process_killed"),
        ({"return_code": 137}, "process_killed"),
        ({"error": "input file missing: /shared/in/a.pdf"}, "input_missing"),
        ({"error": "model request timed out after 180s"}, "api_timeout"),
        ({"error": "Connection refused while connecting to http://dotsocr-lb.internal:13080"}, "model_unreachable"),
        ({"error": "SSL certificate verify failed while connecting to model endpoint"}, "model_unreachable"),
        ({"error": "HTTP 503 from model server"}, "model_unavailable"),
        ({"error": "invalid model response JSON"}, "model_output_invalid"),
        ({"error": "JSONDecodeError: Expecting value while parsing model response"}, "model_output_invalid"),
        ({"error": "permission denied writing /shared/out/a.md"}, "output_unwritable"),
        ({"error": "[Errno 28] No space left on device"}, "output_unwritable"),
        ({"error": "pymupdf.FileDataError: cannot open broken document /shared/in/bad.pdf"}, "input_invalid"),
        ({"error": "document requires a password before page rendering can continue"}, "input_invalid"),
        ({"error": "CUDA out of memory. Tried to allocate 2.00 GiB"}, "resource_exhausted"),
        ({"error": "unexpected parser crash"}, "parser_failed"),
        ({"error": "unexpected exception while parsing page 7"}, "parser_failed"),
    ],
)
def test_job_failed_event_infers_failure_category_when_missing(tmp_path, payload, expected_category):
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

    resp = client.post(
        f"/api/jobs/{job['id']}/events",
        json={"type": "job_failed", "payload": payload},
    )

    assert resp.status_code == 200
    assert resp.json()["failure_category"] == expected_category


def test_success_fallback_image_is_reported_as_degraded_quality(tmp_path):
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

    client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "page_done",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "filename": "a",
                "page_no": 1,
                "status": "success_fallback_image",
            },
        },
    )
    client.post(f"/api/jobs/{job['id']}/events", json={"type": "job_done"})

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["status"] == "succeeded"
    assert summary["degraded_pages"] == 1
    assert summary["quality_flags"] == ["image_fallback"]


def test_job_done_after_stop_request_records_stopped_status(tmp_path):
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

    stop_resp = client.post(f"/api/jobs/{job['id']}/request-stop")
    done_resp = client.post(f"/api/jobs/{job['id']}/events", json={"type": "job_done"})

    assert stop_resp.status_code == 200
    assert done_resp.status_code == 200
    assert done_resp.json()["status"] == "stopped"
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "stopped"


def test_job_stopped_after_stop_requested_done_keeps_stopped_status(tmp_path):
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

    client.post(f"/api/jobs/{job['id']}/request-stop")
    done_resp = client.post(f"/api/jobs/{job['id']}/events", json={"type": "job_done"})
    stopped_resp = client.post(
        f"/api/jobs/{job['id']}/events",
        json={"type": "job_stopped", "payload": {"return_code": 0}},
    )

    assert done_resp.status_code == 200
    assert stopped_resp.status_code == 200
    assert done_resp.json()["status"] == "stopped"
    assert stopped_resp.json()["status"] == "stopped"
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "stopped"


def test_late_stop_request_does_not_overwrite_failed_terminal_status(tmp_path):
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

    failed_resp = client.post(f"/api/jobs/{job['id']}/events", json={"type": "job_failed"})
    stop_resp = client.post(f"/api/jobs/{job['id']}/request-stop")
    done_resp = client.post(f"/api/jobs/{job['id']}/events", json={"type": "job_done"})
    stopped_resp = client.post(
        f"/api/jobs/{job['id']}/events",
        json={"type": "job_stopped", "payload": {"return_code": -15}},
    )

    assert failed_resp.status_code == 200
    assert stop_resp.status_code == 200
    assert done_resp.status_code == 200
    assert stopped_resp.status_code == 200
    assert stop_resp.json()["status"] == "failed"
    assert done_resp.json()["status"] == "failed"
    assert stopped_resp.json()["status"] == "failed"
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "failed"


def test_invalid_event_payload_returns_client_error_not_404(tmp_path):
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

    resp = client.post(
        f"/api/jobs/{job['id']}/events",
        json={
            "type": "page_done",
            "payload": {
                "file_path": "/shared/in/a.pdf",
                "page_no": "not-an-int",
            },
        },
    )

    assert resp.status_code in {400, 422}


def test_claiming_same_queue_twice_returns_null_second_time(tmp_path):
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

    first = client.post("/api/agents/server-a/next-job")
    second = client.post("/api/agents/server-a/next-job")

    assert first.status_code == 200
    assert first.json()["id"] == job["id"]
    assert second.status_code == 200
    assert second.json() is None


def test_log_endpoint_returns_ok(tmp_path):
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

    resp = client.post(
        f"/api/jobs/{job['id']}/logs",
        json={"server_id": "server-a", "stream": "stdout", "line": "started"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_job_log_page_endpoint_returns_bounded_filtered_logs(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    client.post(
        "/api/servers/register",
        json={"id": "server-b", "name": "Server B", "host": "localhost"},
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
    rows = [
        ("server-a", "stdout", "a-stdout-0"),
        ("server-a", "stderr", "a-stderr-1"),
        ("server-b", "stderr", "b-stderr-2"),
        ("server-a", "stderr", "a-stderr-3"),
        ("server-a", "stderr", "a-stderr-4"),
    ]
    for server_id, stream, line in rows:
        response = client.post(
            f"/api/jobs/{job['id']}/logs",
            json={"server_id": server_id, "stream": stream, "line": line},
        )
        assert response.status_code == 200

    page = client.get(
        f"/api/jobs/{job['id']}/logs/page",
        params={"stream": "stderr", "server_id": "server-a", "limit": 2, "offset": 0},
    )

    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 3
    assert payload["limit"] == 2
    assert payload["offset"] == 0
    assert payload["has_more"] is True
    assert [item["line"] for item in payload["items"]] == ["a-stderr-4", "a-stderr-3"]
    assert {item["server_id"] for item in payload["items"]} == {"server-a"}
    assert {item["stream"] for item in payload["items"]} == {"stderr"}
    assert payload["items"][0]["created_at"] is not None


def test_job_log_rows_are_capped(monkeypatch, tmp_path):
    monkeypatch.setattr(control_limits, "JOB_LOG_DETAIL_LIMIT", 2)
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
        response = client.post(
            f"/api/jobs/{job['id']}/logs",
            json={"server_id": "server-a", "stream": "stdout", "line": f"line-{index}"},
        )
        assert response.status_code == 200

    with session_factory() as session:
        rows = (
            session.query(JobLog)
            .filter_by(job_id=job["id"])
            .order_by(JobLog.created_at.asc(), JobLog.id.asc())
            .all()
        )
        assert [row.line for row in rows] == ["line-3", "line-4"]


def test_job_log_detail_rows_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(control_limits, "JOB_LOG_DETAIL_LIMIT", 0)
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

    response = client.post(
        f"/api/jobs/{job['id']}/logs",
        json={"server_id": "server-a", "stream": "stdout", "line": "line-0"},
    )

    assert response.status_code == 200
    with session_factory() as session:
        assert session.query(JobLog).filter_by(job_id=job["id"]).count() == 0
