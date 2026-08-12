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
        control_database,
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
    assert payload["known_migrations"][-1] == "0020_model_profile_certification"
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
        "0020_model_profile_certification",
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


def test_source_offer_prefers_immutable_wheel_build_provenance(tmp_path, monkeypatch):
    monkeypatch.delenv("OCR_PLATFORM_SOURCE_REVISION", raising=False)
    monkeypatch.delenv("OCR_PLATFORM_SOURCE_URL", raising=False)
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {
            "source_revision": "abc123def456",
            "build_timestamp": "2026-07-17T06:00:00Z",
            "dirty": False,
        },
    )
    client = make_client(tmp_path)

    payload = client.get("/source.json").json()

    assert payload["source_revision"] == "abc123def456"
    assert payload["source_revision_origin"] == "build"
    assert payload["build_timestamp"] == "2026-07-17T06:00:00Z"
    assert payload["build_dirty"] is False
    assert payload["release_build"] is True


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


def test_fresh_control_with_zero_workers_is_never_reported_as_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "control-secret")
    client = make_client(tmp_path)

    payload = client.get(
        "/api/system/diagnostics",
        headers={"Authorization": "Bearer control-secret"},
    ).json()

    assert payload["workers"] == {
        "total": 0,
        "ready": 0,
        "stale": 0,
        "with_shared_roots": 0,
        "resource_constrained": 0,
    }
    no_workers = next(
        issue for issue in payload["issues"] if issue["code"] == "no_workers"
    )
    assert no_workers["severity"] == "error"
    assert payload["ok"] is False


def test_zero_worker_doctor_blocks_ready_on_the_same_evidence_as_job_preflight(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "control-secret")
    client = make_client(tmp_path)
    auth = {"Authorization": "Bearer control-secret"}
    shared_root = tmp_path / "shared"
    (shared_root / "input").mkdir(parents=True)
    (shared_root / "output").mkdir(parents=True)

    diagnostics = client.get("/api/system/diagnostics", headers=auth).json()
    preflight = client.post(
        "/api/jobs/preflight",
        headers=auth,
        json={
            "input_dir": str(shared_root / "input"),
            "output_dir": str(shared_root / "output"),
            "engine": "dotsocr",
            "input_mode": "distributed_remote_folder_snapshot",
        },
    ).json()

    assert diagnostics["ok"] is False
    assert preflight["ok"] is False
    assert preflight["eligible_workers"] == 0
    doctor_errors = {
        issue["code"] for issue in diagnostics["issues"] if issue["severity"] == "error"
    }
    preflight_errors = {
        issue["code"] for issue in preflight["issues"] if issue["severity"] == "error"
    }
    assert "no_workers" in doctor_errors
    assert "no_eligible_workers" in preflight_errors


def test_diagnostics_ok_recovers_once_a_ready_worker_registers(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "control-secret")
    monkeypatch.setattr(
        control_database,
        "describe_database_status",
        lambda bind: {
            "dialect": "postgresql",
            "schema_migrations_table_exists": True,
            "migration_checksum_column_exists": True,
            "is_current": True,
            "checksum_mismatches": [],
            "missing_checksums": [],
            "missing_migrations": [],
            "unexpected_migrations": [],
        },
    )
    client = make_client(tmp_path)
    auth = {"Authorization": "Bearer control-secret"}
    shared = str(tmp_path / "shared")

    before = client.get("/api/system/diagnostics", headers=auth).json()
    register = client.post(
        "/api/servers/register",
        headers=auth,
        json={
            "id": "worker-a",
            "name": "Worker A",
            "host": "10.0.0.10",
            "capacity_slots": 1,
            "capabilities": {
                "shared_roots": [shared],
                "shared_paths": [
                    {
                        "path": shared,
                        "exists": True,
                        "readable": True,
                        "writable": True,
                    }
                ],
            },
        },
    )
    assert register.status_code == 200
    after = client.get("/api/system/diagnostics", headers=auth).json()

    assert before["ok"] is False
    assert {issue["code"] for issue in before["issues"]} >= {"no_workers"}
    assert after["workers"]["ready"] == 1
    assert not any(issue["severity"] == "error" for issue in after["issues"])
    assert after["ok"] is True


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
    client, session_factory = make_client_with_session(tmp_path)

    profiles = client.get("/api/model-profiles")

    assert profiles.status_code == 200
    dotsocr = next(item for item in profiles.json() if item["id"] == "dotsocr_15")
    assert dotsocr["engine"] == "dotsocr"
    assert dotsocr["requires_api_key"] is True
    assert dotsocr["has_api_key"] is False
    assert "api_key" not in dotsocr
    assert dotsocr["certification"]["status"] == "contract_only"
    assert dotsocr["certification"]["enforcement"] == "off"

    with session_factory() as session:
        assert session.get(ModelProfileCertification, "dotsocr_15") is None

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
    assert payload["certification"]["status"] == "contract_only"
    assert payload["certification"]["enforcement"] == "off"

    with session_factory() as session:
        assert session.get(ModelProfileCertification, "dotsocr_15") is None


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
    client, session_factory = make_client_with_session(tmp_path)

    response = client.put(
        "/api/model-profiles/blocked-default",
        json={
            "label": "DotsOCR production",
            "engine": "dotsocr",
            "requires_api_key": True,
            "api_key": "profile-secret",
            "is_default": True,
        },
    )

    assert response.status_code == 400
    assert "api_key_env_var" in response.json()["detail"]
    with session_factory() as session:
        assert session.get(ModelProfile, "blocked-default") is None
        assert session.get(ModelProfile, "dotsocr_15").is_default is True


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
    disabled_client = TestClient(
        create_app(session_factory=session_factory)
    )

    blocked = disabled_client.put(
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
        control_database,
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
    issue = database_migration_preflight_issue(
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
        control_database,
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
