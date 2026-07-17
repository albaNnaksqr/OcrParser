from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ocr_platform.control.app import create_app
from ocr_platform.control.database import create_session_factory, init_db
import ocr_platform.control.service as service

from ocr_platform.control.models import Job, JobEvent, JobFile, JobLog, Manifest, ModelProfile, ScanUnit, Server, WorkShard, utcnow


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


def test_api_token_auth_is_optional_by_default(tmp_path):
    client = make_client(tmp_path)

    response = client.get("/api/servers")

    assert response.status_code == 200


def test_api_token_can_be_required_for_production(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_REQUIRE_API_TOKEN", "1")
    monkeypatch.delenv("OCR_PLATFORM_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="API token is required"):
        make_client(tmp_path)


def test_api_token_required_mode_accepts_configured_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_REQUIRE_API_TOKEN", "true")
    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "control-secret")

    client = make_client(tmp_path)
    response = client.get(
        "/api/servers",
        headers={"Authorization": "Bearer control-secret"},
    )

    assert response.status_code == 200


def test_api_token_auth_rejects_missing_or_wrong_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "control-secret")
    client = make_client(tmp_path)

    missing = client.get("/api/servers")
    wrong = client.get("/api/servers", headers={"Authorization": "Bearer wrong"})

    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_control_app_can_require_current_postgres_migrations_at_startup(tmp_path, monkeypatch):
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    monkeypatch.setenv("OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS", "1")
    monkeypatch.setattr(
        service.database,
        "describe_database_status",
        lambda db_engine: {
            "dialect": "postgresql",
            "schema_migrations_table_exists": True,
            "known_migrations": ["0001_control_schema", "0002_add_indexes"],
            "applied_migrations": [
                {
                    "version": "0001_control_schema",
                    "applied_at": "2026-05-31T00:00:00+00:00",
                }
            ],
            "latest_applied_migration": "0001_control_schema",
            "missing_migrations": ["0002_add_indexes"],
            "is_current": False,
        },
    )

    with pytest.raises(RuntimeError, match="database migrations are not current"):
        create_app(session_factory=session_factory)


def test_api_token_auth_accepts_bearer_or_platform_header(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "control-secret")
    client = make_client(tmp_path)

    bearer = client.get("/api/servers", headers={"Authorization": "Bearer control-secret"})
    platform_header = client.get("/api/servers", headers={"X-OCR-Platform-Token": "control-secret"})
    api_key_header = client.get("/api/servers", headers={"X-API-Key": "control-secret"})

    assert bearer.status_code == 200
    assert platform_header.status_code == 200
    assert api_key_header.status_code == 200


def test_database_status_exposes_applied_schema_migrations(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    with session_factory() as session:
        session.execute(
            text(
                "CREATE TABLE schema_migrations ("
                "version VARCHAR(128) PRIMARY KEY, "
                "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        session.execute(text("INSERT INTO schema_migrations (version) VALUES ('0001_control_schema')"))
        session.commit()

    response = client.get("/api/system/database")

    assert response.status_code == 200
    payload = response.json()
    assert payload["dialect"] == "sqlite"
    assert payload["schema_migrations_table_exists"] is True
    assert payload["known_migrations"][-1] == "0019_schema_migration_checksums"
    assert payload["missing_migrations"] == [
        "0002_enforce_work_shard_job_index",
        "0003_job_counter_failed_file_samples",
        "0004_unique_shard_attempt_number",
        "0005_job_file_failure_category",
        "0006_job_log_pruning_index",
        "0007_shard_attempt_execution_control",
        "0008_detail_pruning_indexes",
        "0009_compatibility_schema_columns",
        "0010_shard_inspector_filter_indexes",
        "0011_unique_scan_unit_path",
        "0012_jobs_default_list_index",
        "0013_job_counter_failure_category_counts",
        "0014_job_event_failure_category",
        "0015_job_counter_recent_error_samples",
        "0016_job_file_upsert_path_index",
            "0017_worker_manifest_integrity",
            "0018_widen_input_mode_columns",
            "0019_schema_migration_checksums",
        ]
    assert payload["is_current"] is False
    assert payload["latest_applied_migration"] == "0001_control_schema"
    assert payload["applied_migrations"][0]["version"] == "0001_control_schema"
    assert payload["applied_migrations"][0]["applied_at"]


def test_deployment_doctor_reports_migration_checksum_mismatch(tmp_path):
    client, session_factory = make_client_with_session(tmp_path)
    with session_factory() as session:
        session.execute(
            text(
                "CREATE TABLE schema_migrations ("
                "version VARCHAR(128) PRIMARY KEY, "
                "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "checksum VARCHAR(64))"
            )
        )
        session.execute(
            text(
                "INSERT INTO schema_migrations (version, checksum) "
                "VALUES ('0001_control_schema', 'incorrect')"
            )
        )
        session.commit()

    payload = client.get("/api/system/diagnostics").json()

    issue_codes = {issue["code"] for issue in payload["issues"]}
    assert "database_migration_checksum_mismatch" in issue_codes
    assert payload["database"]["checksum_mismatches"][0]["version"] == "0001_control_schema"


def test_healthz_and_readyz_are_public_when_api_token_is_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "control-secret")
    client = make_client(tmp_path)

    health = client.get("/healthz")
    ready = client.get("/readyz")

    assert health.status_code == 200
    assert health.json() == {"ok": True, "service": "ocr-platform-control"}
    assert ready.status_code == 200
    payload = ready.json()
    assert payload["ok"] is True
    assert payload["database"]["dialect"] == "sqlite"
    assert "api_auth" in payload


def test_agpl_source_offer_and_license_are_public(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "control-secret")
    monkeypatch.setenv("OCR_PLATFORM_SOURCE_REVISION", "abc123def456")
    client = make_client(tmp_path)

    source = client.get("/source", follow_redirects=False)
    metadata = client.get("/source.json")
    license_response = client.get("/legal/agpl-3.0")

    assert source.status_code == 307
    assert source.headers["location"] == (
        "https://github.com/albaNnaksqr/OcrParser/tree/abc123def456"
    )
    assert metadata.status_code == 200
    assert metadata.json()["source_revision"] == "abc123def456"
    assert metadata.json()["source_revision_explicit"] is True
    assert metadata.json()["license_url"] == "/legal/agpl-3.0"
    assert license_response.status_code == 200
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in license_response.text
    assert "Remote Network Interaction" in license_response.text

    source_head = client.head("/source", follow_redirects=False)
    assert source_head.status_code == 307
    assert source_head.headers["location"].endswith("/tree/abc123def456")


def test_agpl_source_offer_can_use_an_explicit_archive_url(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "OCR_PLATFORM_SOURCE_URL",
        "https://downloads.example.test/ocrparser/source-abc123.tar.gz",
    )
    client = make_client(tmp_path)

    source = client.get("/source", follow_redirects=False)

    assert source.status_code == 307
    assert source.headers["location"] == (
        "https://downloads.example.test/ocrparser/source-abc123.tar.gz"
    )


def test_system_diagnostics_summarizes_deployment_readiness_for_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "control-secret")
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        headers={"Authorization": "Bearer control-secret"},
        json={
            "id": "worker-a",
            "name": "Worker A",
            "host": "10.0.0.10",
            "capacity_slots": 2,
            "capabilities": {
                "shared_roots": ["/shared/ocr-data"],
                "shared_paths": [{"path": "/shared/ocr-data", "exists": True, "readable": True, "writable": True}],
                "git_ref": "abc123",
                "script_version": "worker-v1",
            },
        },
    )

    missing = client.get("/api/system/diagnostics")
    authorized = client.get(
        "/api/system/diagnostics",
        headers={"Authorization": "Bearer control-secret"},
    )

    assert missing.status_code == 401
    assert authorized.status_code == 200
    payload = authorized.json()
    assert payload["ok"] is False
    assert payload["api_auth"]["enabled"] is True
    assert payload["database"]["dialect"] == "sqlite"
    assert payload["workers"]["total"] == 1
    assert payload["workers"]["ready"] == 1
    assert payload["workers"]["with_shared_roots"] == 1
    assert any(issue["code"] == "database_not_postgres" for issue in payload["issues"])


def test_register_server_create_job_and_claim(tmp_path):
    client = make_client(tmp_path)

    server_resp = client.post(
        "/api/servers/register",
        json={
            "id": "server-a",
            "name": "Server A",
            "host": "10.0.0.1",
            "capacity_slots": 1,
            "capabilities": {"engines": ["dotsocr"]},
        },
    )
    assert server_resp.status_code == 200

    job_resp = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "page_concurrency": 2,
        },
    )
    assert job_resp.status_code == 200
    job_id = job_resp.json()["id"]

    claim_resp = client.post("/api/agents/server-a/next-job")
    assert claim_resp.status_code == 200
    claimed = claim_resp.json()
    assert claimed["id"] == job_id
    assert claimed["status"] == "running"

    detail_resp = client.get(f"/api/jobs/{job_id}")
    assert detail_resp.json()["status"] == "running"


def test_model_profiles_are_persisted_and_do_not_echo_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS", "1")
    client = make_client(tmp_path)

    profiles = client.get("/api/model-profiles")

    assert profiles.status_code == 200
    dotsocr = next(item for item in profiles.json() if item["id"] == "dotsocr_15")
    assert dotsocr["engine"] == "dotsocr"
    assert dotsocr["requires_api_key"] is True
    assert dotsocr["has_api_key"] is False
    assert "api_key" not in dotsocr

    saved = client.put(
        "/api/model-profiles/dotsocr_15",
        json={
            "label": "DotsOCR production",
            "engine": "dotsocr",
            "ip": "dotsocr-lb.internal",
            "port": 13080,
            "model_name": "DotsOCR",
            "page_concurrency": 160,
            "extra_args": {
                "file_concurrency": 8,
                "api_concurrency_start": 80,
                "api_concurrency_max": 160,
                "num_cpu_workers": 24,
            },
            "requires_api_key": True,
            "api_key": "profile-secret",
        },
    )

    assert saved.status_code == 200
    payload = saved.json()
    assert payload["label"] == "DotsOCR production"
    assert payload["has_api_key"] is True
    assert "api_key" not in payload


def test_model_profile_rejects_api_key_in_extra_args(tmp_path):
    client = make_client(tmp_path)

    response = client.put(
        "/api/model-profiles/dotsocr_15",
        json={
            "label": "DotsOCR production",
            "engine": "dotsocr",
            "ip": "dotsocr-lb.internal",
            "port": 13080,
            "model_name": "DotsOCR",
            "page_concurrency": 160,
            "extra_args": {
                "api_key": "profile-secret",
                "file_concurrency": 8,
            },
            "requires_api_key": True,
        },
    )

    assert response.status_code == 400
    assert "api_key" in response.json()["detail"]


def test_model_profile_rejects_secret_like_extra_args(tmp_path):
    client = make_client(tmp_path)

    response = client.put(
        "/api/model-profiles/dotsocr_15",
        json={
            "label": "DotsOCR production",
            "engine": "dotsocr",
            "ip": "dotsocr-lb.internal",
            "port": 13080,
            "model_name": "DotsOCR",
            "page_concurrency": 160,
            "extra_args": {
                "access_token": "profile-token",
                "file_concurrency": 8,
            },
            "requires_api_key": True,
        },
    )

    assert response.status_code == 400
    assert "access_token" in response.json()["detail"]


def test_model_profile_rejects_saved_api_key_when_db_profile_keys_are_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS", "1")
    client = make_client(tmp_path)

    response = client.put(
        "/api/model-profiles/dotsocr_15",
        json={
            "label": "DotsOCR production",
            "engine": "dotsocr",
            "ip": "dotsocr-lb.internal",
            "port": 13080,
            "model_name": "DotsOCR",
            "page_concurrency": 160,
            "extra_args": {"file_concurrency": 8},
            "requires_api_key": True,
            "api_key": "profile-secret",
        },
    )

    assert response.status_code == 400
    assert "api_key_env_var" in response.json()["detail"]


def test_model_profile_rejects_saved_api_key_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS", raising=False)
    monkeypatch.delenv("OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS", raising=False)
    client = make_client(tmp_path)

    response = client.put(
        "/api/model-profiles/dotsocr_15",
        json={
            "label": "DotsOCR production",
            "engine": "dotsocr",
            "requires_api_key": True,
            "api_key": "profile-secret",
        },
    )

    assert response.status_code == 400
    assert "api_key_env_var" in response.json()["detail"]


def test_model_profile_allows_env_api_key_when_db_profile_keys_are_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS", "1")
    monkeypatch.setenv("OCR_MODEL_DOTSOCR_API_KEY", "env-profile-secret")
    client, session_factory = make_client_with_session(tmp_path)

    response = client.put(
        "/api/model-profiles/dotsocr_15",
        json={
            "label": "DotsOCR production",
            "engine": "dotsocr",
            "ip": "dotsocr-lb.internal",
            "port": 13080,
            "model_name": "DotsOCR",
            "page_concurrency": 160,
            "extra_args": {"file_concurrency": 8},
            "requires_api_key": True,
            "api_key_env_var": "OCR_MODEL_DOTSOCR_API_KEY",
            "clear_api_key": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["has_api_key"] is True
    assert response.json()["api_key_env_var"] == "OCR_MODEL_DOTSOCR_API_KEY"
    with session_factory() as session:
        profile = session.get(ModelProfile, "dotsocr_15")
        assert profile.api_key is None
        assert profile.api_key_env_var == "OCR_MODEL_DOTSOCR_API_KEY"


def test_model_profile_requires_clearing_existing_saved_key_when_db_profile_keys_are_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS", "1")
    client, session_factory = make_client_with_session(tmp_path)
    saved = client.put(
        "/api/model-profiles/dotsocr_15",
        json={
            "label": "DotsOCR production",
            "engine": "dotsocr",
            "ip": "dotsocr-lb.internal",
            "port": 13080,
            "model_name": "DotsOCR",
            "page_concurrency": 160,
            "extra_args": {"file_concurrency": 8},
            "requires_api_key": True,
            "api_key": "profile-secret",
        },
    )
    assert saved.status_code == 200
    monkeypatch.delenv("OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS", raising=False)
    monkeypatch.setenv("OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS", "1")

    blocked = client.put(
        "/api/model-profiles/dotsocr_15",
        json={
            "label": "DotsOCR production",
            "engine": "dotsocr",
            "ip": "dotsocr-lb.internal",
            "port": 13080,
            "model_name": "DotsOCR",
            "page_concurrency": 160,
            "extra_args": {"file_concurrency": 8},
            "requires_api_key": True,
        },
    )

    assert blocked.status_code == 400
    assert "clear_api_key" in blocked.json()["detail"]
    with session_factory() as session:
        assert session.get(ModelProfile, "dotsocr_15").api_key == "profile-secret"


def test_create_job_can_inject_saved_model_profile_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS", "1")
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    client.put(
        "/api/model-profiles/dotsocr_15",
        json={
            "label": "DotsOCR production",
            "engine": "dotsocr",
            "ip": "dotsocr-lb.internal",
            "port": 13080,
            "model_name": "DotsOCR",
            "page_concurrency": 160,
            "extra_args": {"file_concurrency": 8, "num_cpu_workers": 24},
            "requires_api_key": True,
            "api_key": "profile-secret",
        },
    )

    response = client.post(
        "/api/jobs",
        json={
            "model_profile_id": "dotsocr_15",
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "extra_args": {"file_concurrency": 4},
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert job["engine"] == "dotsocr"
    assert job["ip"] == "dotsocr-lb.internal"
    assert job["port"] == 13080
    assert job["model_name"] == "DotsOCR"
    assert job["page_concurrency"] == 160
    assert job["extra_args"]["file_concurrency"] == 4
    assert job["extra_args"]["num_cpu_workers"] == 24
    assert "api_key" not in job["extra_args"]

    claimed = client.post("/api/agents/server-a/next-job").json()
    assert claimed["extra_args"]["api_key"] == "profile-secret"
    with session_factory() as session:
        stored = session.get(Job, job["id"])
        assert "profile-secret" not in stored.extra_args_json
        assert "api_key" not in stored.extra_args_json


def test_create_job_can_inject_model_profile_api_key_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("OCR_MODEL_DOTSOCR_API_KEY", "env-profile-secret")
    client, session_factory = make_client_with_session(tmp_path)
    client.put(
        "/api/model-profiles/dotsocr_15",
        json={
            "label": "DotsOCR env secret",
            "engine": "dotsocr",
            "ip": "dotsocr-lb.internal",
            "port": 13080,
            "model_name": "DotsOCR",
            "page_concurrency": 160,
            "extra_args": {"file_concurrency": 8},
            "requires_api_key": True,
            "api_key_env_var": "OCR_MODEL_DOTSOCR_API_KEY",
            "clear_api_key": True,
        },
    )
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )

    response = client.post(
        "/api/jobs",
        json={
            "model_profile_id": "dotsocr_15",
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    )

    assert response.status_code == 200
    profile = client.get("/api/model-profiles").json()
    dotsocr = next(item for item in profile if item["id"] == "dotsocr_15")
    assert dotsocr["has_api_key"] is True
    assert dotsocr["api_key_env_var"] == "OCR_MODEL_DOTSOCR_API_KEY"
    assert "api_key" not in dotsocr
    claimed = client.post("/api/agents/server-a/next-job").json()
    assert claimed["extra_args"]["api_key"] == "env-profile-secret"
    with session_factory() as session:
        stored_profile = session.get(ModelProfile, "dotsocr_15")
        stored_job = session.get(Job, response.json()["id"])
        assert stored_profile.api_key is None
        assert stored_profile.api_key_env_var == "OCR_MODEL_DOTSOCR_API_KEY"
        assert "env-profile-secret" not in stored_job.extra_args_json


def test_model_profile_requiring_env_api_key_blocks_when_env_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("OCR_MODEL_DOTSOCR_API_KEY", raising=False)
    client = make_client(tmp_path)
    client.put(
        "/api/model-profiles/dotsocr_15",
        json={
            "label": "DotsOCR env secret",
            "engine": "dotsocr",
            "ip": "dotsocr-lb.internal",
            "port": 13080,
            "model_name": "DotsOCR",
            "page_concurrency": 160,
            "extra_args": {"file_concurrency": 8},
            "requires_api_key": True,
            "api_key_env_var": "OCR_MODEL_DOTSOCR_API_KEY",
            "clear_api_key": True,
        },
    )
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )

    response = client.post(
        "/api/jobs",
        json={
            "model_profile_id": "dotsocr_15",
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    )

    assert response.status_code == 400
    assert "requires api_key" in response.json()["detail"]


def test_job_preflight_reports_production_readiness_issues(tmp_path):
    client = make_client(tmp_path)
    for server_id, git_ref in (("server-a", "main-aaaaaaa"), ("server-b", "main-bbbbbbb")):
        client.post(
            f"/api/servers/{server_id}/heartbeat",
            json={
                "status": "idle",
                "capabilities": {
                    "git_ref": git_ref,
                    "script_version": "ocr-agent-worker-v1",
                    "shared_paths": [
                        {
                            "path": "/shared",
                            "exists": True,
                            "is_dir": True,
                            "readable": True,
                            "writable": True,
                        }
                    ],
                },
            },
        )

    response = client.post(
        "/api/jobs/preflight",
        json={
            "model_profile_id": "dotsocr_15",
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/.ocr_platform/manifests",
            "allowed_server_ids": ["server-a", "server-b"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    codes = {issue["code"]: issue for issue in payload["issues"]}
    assert payload["ok"] is False
    assert payload["eligible_workers"] == 2
    assert codes["model_profile_missing_api_key"]["severity"] == "error"
    assert codes["mixed_worker_versions"]["severity"] == "warning"
    assert codes["database_not_postgres"]["severity"] == "warning"


def test_job_summary_warns_when_assigned_workers_report_mixed_versions(tmp_path):
    client = make_client(tmp_path)
    for server_id, git_ref in (("server-a", "main-aaaaaaa"), ("server-b", "main-bbbbbbb")):
        client.post(
            f"/api/servers/{server_id}/heartbeat",
            json={
                "status": "idle",
                "capabilities": {
                    "git_ref": git_ref,
                    "script_version": "ocr-agent-worker-v1",
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
            "allowed_server_ids": ["server-a", "server-b"],
        },
    ).json()

    summary = client.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["worker_version_status"] == "mixed"
    assert summary["worker_version_warning"] == "assigned workers report different git_ref or script_version values"
    assert summary["worker_version_refs"] == {
        "main-aaaaaaa / ocr-agent-worker-v1": ["server-a"],
        "main-bbbbbbb / ocr-agent-worker-v1": ["server-b"],
    }


def test_job_preflight_warns_when_control_api_auth_is_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("OCR_PLATFORM_API_TOKEN", raising=False)
    monkeypatch.delenv("OCR_PLATFORM_REQUIRE_API_TOKEN", raising=False)
    client = make_client(tmp_path)
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

    response = client.post(
        "/api/jobs/preflight",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/.ocr_platform/manifests",
            "allowed_server_ids": ["server-a"],
        },
    )

    assert response.status_code == 200
    codes = {issue["code"]: issue for issue in response.json()["issues"]}
    assert codes["control_api_auth_disabled"]["severity"] == "warning"
    assert codes["control_api_auth_disabled"]["details"]["require_api_token"] is False


def test_job_preflight_does_not_warn_when_control_api_auth_is_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "control-secret")
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer control-secret"}
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
        headers=headers,
    )

    response = client.post(
        "/api/jobs/preflight",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/.ocr_platform/manifests",
            "allowed_server_ids": ["server-a"],
        },
        headers=headers,
    )

    assert response.status_code == 200
    codes = {issue["code"] for issue in response.json()["issues"]}
    assert "control_api_auth_disabled" not in codes


def test_job_preflight_warns_when_model_profile_uses_saved_db_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS", "1")
    client = make_client(tmp_path)
    client.put(
        "/api/model-profiles/dotsocr_15",
        json={
            "label": "DotsOCR saved secret",
            "engine": "dotsocr",
            "ip": "dotsocr-lb.internal",
            "port": 13080,
            "model_name": "DotsOCR",
            "page_concurrency": 160,
            "extra_args": {"file_concurrency": 8},
            "requires_api_key": True,
            "api_key": "profile-secret",
        },
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
                        "writable": True,
                    }
                ]
            },
        },
    )

    response = client.post(
        "/api/jobs/preflight",
        json={
            "model_profile_id": "dotsocr_15",
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/.ocr_platform/manifests",
            "allowed_server_ids": ["server-a"],
        },
    )

    assert response.status_code == 200
    codes = {issue["code"]: issue for issue in response.json()["issues"]}
    assert codes["model_profile_saved_api_key"]["severity"] == "warning"
    assert codes["model_profile_saved_api_key"]["details"] == {
        "model_profile_id": "dotsocr_15",
        "api_key_env_var": None,
    }


def test_job_preflight_does_not_warn_when_model_profile_uses_env_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_MODEL_DOTSOCR_API_KEY", "env-profile-secret")
    client = make_client(tmp_path)
    client.put(
        "/api/model-profiles/dotsocr_15",
        json={
            "label": "DotsOCR env secret",
            "engine": "dotsocr",
            "ip": "dotsocr-lb.internal",
            "port": 13080,
            "model_name": "DotsOCR",
            "page_concurrency": 160,
            "extra_args": {"file_concurrency": 8},
            "requires_api_key": True,
            "api_key_env_var": "OCR_MODEL_DOTSOCR_API_KEY",
            "clear_api_key": True,
        },
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
                        "writable": True,
                    }
                ]
            },
        },
    )

    response = client.post(
        "/api/jobs/preflight",
        json={
            "model_profile_id": "dotsocr_15",
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/.ocr_platform/manifests",
            "allowed_server_ids": ["server-a"],
        },
    )

    assert response.status_code == 200
    codes = {issue["code"] for issue in response.json()["issues"]}
    assert "model_profile_saved_api_key" not in codes


def test_job_preflight_blocks_postgres_when_schema_migrations_table_is_missing(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "git_ref": "main-aaaaaaa",
                "script_version": "ocr-agent-worker-v1",
                "shared_paths": [
                    {
                        "path": "/shared",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ],
            },
        },
    )

    monkeypatch.setattr(
        service.database,
        "describe_database_status",
        lambda db_engine: {
            "dialect": "postgresql",
            "schema_migrations_table_exists": False,
            "known_migrations": ["0001_control_schema"],
            "applied_migrations": [],
            "latest_applied_migration": None,
        },
    )

    response = client.post(
        "/api/jobs/preflight",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/.ocr_platform/manifests",
            "allowed_server_ids": ["server-a"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    codes = {issue["code"]: issue for issue in payload["issues"]}
    assert payload["ok"] is False
    assert "database_not_postgres" not in codes
    assert codes["database_migrations_missing"]["severity"] == "error"


def test_postgres_migration_preflight_issue_reports_latest_unapplied_migration():
    issue = service._database_migration_preflight_issue(
        {
            "dialect": "postgresql",
            "schema_migrations_table_exists": True,
            "known_migrations": ["0001_control_schema", "0002_add_indexes"],
            "applied_migrations": [
                {
                    "version": "0001_control_schema",
                    "applied_at": "2026-05-31T00:00:00+00:00",
                }
            ],
            "latest_applied_migration": "0001_control_schema",
        }
    )

    assert issue is not None
    assert issue.severity == "error"
    assert issue.code == "database_migration_not_current"
    assert issue.details["latest_known_migration"] == "0002_add_indexes"
    assert issue.details["missing_migrations"] == ["0002_add_indexes"]


def test_create_job_rejects_postgres_with_unapplied_schema_migrations(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    monkeypatch.setattr(
        service.database,
        "describe_database_status",
        lambda db_engine: {
            "dialect": "postgresql",
            "schema_migrations_table_exists": True,
            "known_migrations": ["0001_control_schema", "0002_add_indexes"],
            "applied_migrations": [{"version": "0001_control_schema", "applied_at": "2026-05-31T00:00:00+00:00"}],
            "latest_applied_migration": "0001_control_schema",
        },
    )

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "directory",
            "assigned_server_id": "server-a",
        },
    )

    assert response.status_code == 400
    assert "unapplied SQL migrations" in response.json()["detail"]


def test_job_preflight_warns_when_eligible_workers_are_resource_constrained(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "git_ref": "main-aaaaaaa",
                "script_version": "ocr-agent-worker-v1",
                "shared_paths": [
                    {
                        "path": "/shared",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ],
                "resource_pressure": {
                    "constrained": True,
                    "level": "blocked",
                    "reasons": ["memory percent 94.0% >= 90.0%"],
                },
            },
        },
    )

    response = client.post(
        "/api/jobs/preflight",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/.ocr_platform/manifests",
            "allowed_server_ids": ["server-a"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    codes = {issue["code"]: issue for issue in payload["issues"]}
    assert payload["ok"] is True
    assert codes["resource_constrained_workers"]["severity"] == "warning"
    assert codes["resource_constrained_workers"]["details"]["workers"] == [
        {
            "server_id": "server-a",
            "level": "blocked",
            "reasons": ["memory percent 94.0% >= 90.0%"],
        }
    ]


def test_job_preflight_warns_when_eligible_workers_have_spooled_events(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "git_ref": "main-aaaaaaa",
                "script_version": "ocr-agent-worker-v1",
                "shared_paths": [
                    {
                        "path": "/shared",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ],
                "event_spool": {
                    "dir": "/shared/.ocr-agent/event-spool",
                    "pending_events": 3,
                    "pending_logs": 2,
                    "failed_events": 1,
                    "failed_logs": 4,
                    "dropped_events": 5,
                    "dropped_logs": 6,
                },
            },
        },
    )

    response = client.post(
        "/api/jobs/preflight",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/.ocr_platform/manifests",
            "allowed_server_ids": ["server-a"],
        },
    )

    assert response.status_code == 200
    codes = {issue["code"]: issue for issue in response.json()["issues"]}
    assert codes["worker_event_spool_backlog"]["severity"] == "warning"
    assert codes["worker_event_spool_backlog"]["details"]["workers"] == [
        {
            "server_id": "server-a",
            "dir": "/shared/.ocr-agent/event-spool",
            "pending_events": 3,
            "pending_logs": 2,
            "failed_events": 1,
            "failed_logs": 4,
            "dropped_events": 5,
            "dropped_logs": 6,
            "total_backlog": 21,
        }
    ]


def test_job_preflight_warns_when_eligible_workers_have_pending_shard_updates(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "git_ref": "main-aaaaaaa",
                "script_version": "ocr-agent-worker-v1",
                "shared_paths": [
                    {
                        "path": "/shared",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ],
                "pending_shard_updates": {
                    "pending": 5,
                    "failed": 2,
                },
            },
        },
    )

    response = client.post(
        "/api/jobs/preflight",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/.ocr_platform/manifests",
            "allowed_server_ids": ["server-a"],
        },
    )

    assert response.status_code == 200
    codes = {issue["code"]: issue for issue in response.json()["issues"]}
    assert codes["worker_pending_shard_update_backlog"]["severity"] == "warning"
    assert codes["worker_pending_shard_update_backlog"]["details"]["workers"] == [
        {
            "server_id": "server-a",
            "pending": 5,
            "failed": 2,
            "total_backlog": 7,
        }
    ]


def test_job_preflight_requires_output_and_manifest_roots_to_be_writable(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "git_ref": "main-aaaaaaa",
                "script_version": "ocr-agent-worker-v1",
                "shared_paths": [
                    {
                        "path": "/shared/input",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": False,
                    },
                    {
                        "path": "/shared/output",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": False,
                    },
                    {
                        "path": "/shared/.ocr_platform",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": False,
                    },
                ],
            },
        },
    )

    response = client.post(
        "/api/jobs/preflight",
        json={
            "input_dir": "/shared/input/batch-a",
            "output_dir": "/shared/output/batch-a",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/.ocr_platform/manifests",
            "allowed_server_ids": ["server-a"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    codes = {issue["code"]: issue for issue in payload["issues"]}
    assert payload["ok"] is False
    assert "output_path_not_writable" in codes
    assert codes["output_path_not_writable"]["severity"] == "error"
    assert codes["output_path_not_writable"]["details"]["path"] == "/shared/output/batch-a"
    assert "manifest_root_not_writable" in codes
    assert codes["manifest_root_not_writable"]["severity"] == "error"
    assert codes["manifest_root_not_writable"]["details"]["path"] == "/shared/.ocr_platform/manifests"


def test_job_preflight_requires_every_eligible_worker_to_write_outputs(tmp_path):
    client = make_client(tmp_path)
    for server_id, output_writable in (("server-a", True), ("server-b", False)):
        client.post(
            f"/api/servers/{server_id}/heartbeat",
            json={
                "status": "idle",
                "capabilities": {
                    "git_ref": "main-aaaaaaa",
                    "script_version": "ocr-agent-worker-v1",
                    "shared_paths": [
                        {
                            "path": "/shared",
                            "exists": True,
                            "is_dir": True,
                            "readable": True,
                            "writable": output_writable,
                        }
                    ],
                },
            },
        )

    response = client.post(
        "/api/jobs/preflight",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "manifest_root": "/shared/.ocr_platform/manifests",
            "allowed_server_ids": ["server-a", "server-b"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    codes = {issue["code"]: issue for issue in payload["issues"]}
    assert payload["ok"] is False
    assert codes["output_path_not_writable"]["details"]["unwritable_workers"] == ["server-b"]
    assert codes["manifest_root_not_writable"]["details"]["unwritable_workers"] == ["server-b"]


def test_job_preflight_checks_inferred_manifest_root_writability(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "git_ref": "main-aaaaaaa",
                "script_version": "ocr-agent-worker-v1",
                "shared_paths": [
                    {
                        "path": "/shared",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": False,
                    },
                    {
                        "path": "/output",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    },
                ],
            },
        },
    )

    response = client.post(
        "/api/jobs/preflight",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/output/batch-a",
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
            "allowed_server_ids": ["server-a"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    codes = {issue["code"]: issue for issue in payload["issues"]}
    assert payload["ok"] is False
    assert codes["manifest_root_not_writable"]["details"]["path"] == "/shared/.ocr_platform/manifests"
    assert codes["manifest_root_not_writable"]["details"]["inferred"] is True


def test_per_job_api_key_override_is_only_visible_to_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS", "1")
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "extra_args": {"api_key": "job-secret", "file_concurrency": 2},
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert "api_key" not in job["extra_args"]
    assert job["extra_args"]["file_concurrency"] == 2
    claimed = client.post("/api/agents/server-a/next-job").json()
    assert claimed["extra_args"]["api_key"] == "job-secret"
    with session_factory() as session:
        stored = session.get(Job, job["id"])
        assert '"api_key": "job-secret"' in stored.extra_args_json


def test_per_job_api_key_can_be_resolved_from_environment_without_storing_secret(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OCR_JOB_DOTSOCR_API_KEY", "env-job-secret")
    client, session_factory = make_client_with_session(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "extra_args": {
                "api_key_env_var": "OCR_JOB_DOTSOCR_API_KEY",
                "file_concurrency": 2,
            },
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert "api_key" not in job["extra_args"]
    assert job["extra_args"] == {
        "api_key_env_var": "OCR_JOB_DOTSOCR_API_KEY",
        "file_concurrency": 2,
    }
    claimed = client.post("/api/agents/server-a/next-job").json()
    assert claimed["extra_args"]["api_key"] == "env-job-secret"
    assert "api_key_env_var" not in claimed["extra_args"]
    with session_factory() as session:
        stored = session.get(Job, job["id"])
        assert "env-job-secret" not in stored.extra_args_json
        assert '"api_key_env_var": "OCR_JOB_DOTSOCR_API_KEY"' in stored.extra_args_json


def test_per_job_api_key_env_var_is_validated_before_job_creation(tmp_path, monkeypatch):
    monkeypatch.delenv("OCR_JOB_DOTSOCR_API_KEY", raising=False)
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "extra_args": {"api_key_env_var": "OCR_JOB_DOTSOCR_API_KEY"},
        },
    )

    assert response.status_code == 400
    assert "api_key_env_var" in response.json()["detail"]
    assert "OCR_JOB_DOTSOCR_API_KEY" in response.json()["detail"]


def test_saved_key_guard_rejects_per_job_plain_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS", "1")
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "extra_args": {"api_key": "job-secret"},
        },
    )

    assert response.status_code == 400
    assert "saved job api_key is disabled" in response.json()["detail"]
    assert "api_key_env_var" in response.json()["detail"]


def test_create_job_rejects_secret_like_extra_args_outside_dedicated_api_key_fields(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )

    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/in",
            "output_dir": "/shared/out",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "extra_args": {
                "access_token": "job-token",
                "file_concurrency": 2,
            },
        },
    )

    assert response.status_code == 400
    assert "access_token" in response.json()["detail"]


def test_mineru_default_profile_uses_medium_gray_tuning(tmp_path):
    client = make_client(tmp_path)

    profiles = client.get("/api/model-profiles")

    assert profiles.status_code == 200
    mineru = next(item for item in profiles.json() if item["id"] == "mineru_v25")
    assert mineru["page_concurrency"] == 4
    assert mineru["extra_args"]["file_concurrency"] == 4
    assert mineru["extra_args"]["api_concurrency_start"] == 8
    assert mineru["extra_args"]["api_concurrency_max"] == 8
    assert mineru["extra_args"]["block_concurrency"] == 8
    assert mineru["extra_args"]["mineru_layout_reserved_api_slots"] == 2
    assert mineru["extra_args"]["mineru_recognition_api_concurrency"] == 6
    assert mineru["extra_args"]["num_cpu_workers"] == 16


def test_paddleocr_vl_default_profile_uses_medium_gray_tuning(tmp_path):
    client = make_client(tmp_path)

    profiles = client.get("/api/model-profiles")

    assert profiles.status_code == 200
    paddle = next(item for item in profiles.json() if item["id"] == "paddleocr_vl_local")
    assert paddle["page_concurrency"] == 4
    assert paddle["extra_args"]["file_concurrency"] == 4
    assert paddle["extra_args"]["api_concurrency_start"] == 8
    assert paddle["extra_args"]["api_concurrency_max"] == 8
    assert paddle["extra_args"]["block_concurrency"] == 8
    assert paddle["extra_args"]["paddle_layout_concurrency"] == 2
    assert paddle["extra_args"]["paddle_block_backpressure_high_watermark"] == 24
    assert paddle["extra_args"]["paddle_block_backpressure_low_watermark"] == 8
    assert paddle["extra_args"]["num_cpu_workers"] == 16


def test_server_heartbeat_updates_runtime_status_and_summary_counts(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/servers/register",
        json={
            "id": "server-a",
            "name": "Server A",
            "host": "10.0.0.1",
            "capacity_slots": 2,
            "capabilities": {"engines": ["dotsocr"]},
        },
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

    heartbeat = client.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "busy",
            "current_job_id": job["id"],
            "capabilities": {"shared_roots": ["/shared"]},
        },
    )

    assert heartbeat.status_code == 200
    server = heartbeat.json()
    assert server["id"] == "server-a"
    assert server["status"] == "busy"
    assert server["last_heartbeat_at"] is not None
    assert server["is_stale"] is False
    assert server["active_jobs"] == 1
    assert server["running_shards"] == 0
    assert server["capabilities"]["engines"] == ["dotsocr"]
    assert server["capabilities"]["shared_roots"] == ["/shared"]


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
    from ocr_platform.control import service

    monkeypatch.setattr(service, "JOB_FILE_DETAIL_LIMIT", 2)
    monkeypatch.setattr(service, "JOB_EVENT_DETAIL_LIMIT", 3)
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
    from ocr_platform.control import service

    monkeypatch.setattr(service, "JOB_FILE_DETAIL_LIMIT", 2)
    monkeypatch.setattr(service, "JOB_EVENT_DETAIL_LIMIT", 10)
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
    from ocr_platform.control import service

    monkeypatch.setattr(service, "JOB_FILE_DETAIL_LIMIT", 10)
    monkeypatch.setattr(service, "JOB_EVENT_DETAIL_LIMIT", 3)
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
    from ocr_platform.control import service

    monkeypatch.setattr(service, "SERVER_STALE_AFTER_SECONDS", 1)
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
    from ocr_platform.control import service

    monkeypatch.setattr(service, "JOB_FILE_DETAIL_LIMIT", 0)
    monkeypatch.setattr(service, "JOB_EVENT_DETAIL_LIMIT", 0)
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
    from ocr_platform.control import service

    monkeypatch.setattr(service, "JOB_EVENT_DETAIL_LIMIT", 0)
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
        payload = service.json_loads_object(rows[0].payload_json)
        assert payload["scanned_files"] == 150


def test_recent_failed_files_uses_counter_samples_when_detail_rows_are_disabled(monkeypatch, tmp_path):
    from ocr_platform.control import service

    monkeypatch.setattr(service, "JOB_FILE_DETAIL_LIMIT", 0)
    monkeypatch.setattr(service, "JOB_EVENT_DETAIL_LIMIT", 0)
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
    from ocr_platform.control import service

    monkeypatch.setattr(service, "JOB_FILE_DETAIL_LIMIT", 0)
    monkeypatch.setattr(service, "JOB_EVENT_DETAIL_LIMIT", 0)
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
    from ocr_platform.control import service

    monkeypatch.setattr(service, "JOB_FILE_DETAIL_LIMIT", 0)
    monkeypatch.setattr(service, "JOB_EVENT_DETAIL_LIMIT", 0)
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
    from ocr_platform.control import service

    monkeypatch.setattr(service, "JOB_FILE_DETAIL_LIMIT", 0)
    monkeypatch.setattr(service, "JOB_EVENT_DETAIL_LIMIT", 0)
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
    from ocr_platform.control import service

    monkeypatch.setattr(service, "JOB_LOG_DETAIL_LIMIT", 2)
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
    from ocr_platform.control import service

    monkeypatch.setattr(service, "JOB_LOG_DETAIL_LIMIT", 0)
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
