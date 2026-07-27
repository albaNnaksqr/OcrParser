from __future__ import annotations

import ast
import importlib
import inspect
import json
from collections import Counter
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import ocr_platform.control.domains.jobs.core as jobs_core
import ocr_platform.control.domains.diagnostics.operations as diagnostics_operations
import ocr_platform.control.domains.workers.core as workers_core
import ocr_platform.control.limits as limits_module
from ocr_platform.control.app import create_app
from ocr_platform.control.bootstrap import build_control_runtime
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.limits import ControlLimits, legacy_control_limits
from ocr_platform.control.models import (
    JobCounter,
    JobEvent,
    JobFile,
    JobLog,
    Manifest,
    WorkShard,
)
from ocr_platform.control.schemas import (
    JobCreateRequest,
    JobEventRequest,
)
from ocr_platform.control.settings import ControlSettings


def _session_factory(tmp_path, name: str):
    database_url = f"sqlite:///{tmp_path / name}"
    settings = ControlSettings(database_url=database_url)
    session_factory, engine = create_session_factory(
        database_url,
        settings=settings,
    )
    init_db(engine)
    return session_factory, engine, settings


def _app_with_limits(tmp_path, name: str, limits: ControlLimits):
    session_factory, engine, settings = _session_factory(tmp_path, name)
    runtime = build_control_runtime(
        settings=settings,
        limits=limits,
        session_factory=session_factory,
    )
    app = create_app(runtime=runtime)
    return app, session_factory, engine


def _create_job(client: TestClient) -> str:
    registration = client.post(
        "/api/servers/register",
        json={
            "id": "server-a",
            "name": "Server A",
            "host": "localhost",
        },
    )
    assert registration.status_code == 200
    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def _compatibility_service():
    module_name = ".".join(("ocr_platform", "control", "service"))
    return importlib.import_module(module_name)


def test_control_limits_parse_existing_environment_and_are_frozen() -> None:
    limits = ControlLimits.from_environment(
        {
            "OCR_JOB_FILE_DETAIL_LIMIT": "0",
            "OCR_JOB_EVENT_DETAIL_LIMIT": "-1",
            "OCR_JOB_LOG_DETAIL_LIMIT": "not-an-int",
            "OCR_JOB_FAILED_FILE_SAMPLE_LIMIT": "7",
            "OCR_JOB_SUMMARY_ATTENTION_SHARD_LIMIT": "2",
            "OCR_MANIFEST_INTEGRITY_ISSUE_SAMPLE_LIMIT": "0",
            "OCR_RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED": "9",
            "OCR_DIAGNOSTICS_EVIDENCE_ROW_LIMIT": "9",
            "OCR_METRICS_TRACE_EVENT_LIMIT": "9",
        }
    )

    assert limits.job_file_detail_limit == 0
    assert limits.job_event_detail_limit == 50_000
    assert limits.job_log_detail_limit == 10_000
    assert limits.job_failed_file_sample_limit == 7
    assert limits.job_recent_error_sample_limit == 7
    assert limits.job_summary_attention_shard_limit == 2
    assert limits.retained_control_event_limit_when_details_disabled == 1
    assert not hasattr(limits, "manifest_integrity_issue_sample_limit")
    assert not hasattr(limits, "diagnostics_evidence_row_limit")
    assert not hasattr(limits, "metrics_trace_event_limit")
    assert limits.persist_job_file_details is False
    assert limits.persist_job_event_details is True
    assert not hasattr(limits, "__dict__")
    with pytest.raises(FrozenInstanceError):
        limits.job_file_detail_limit = 1  # type: ignore[misc]


def test_control_limits_only_contain_nonscheduling_numeric_policy() -> None:
    field_names = {field.name for field in fields(ControlLimits)}

    assert field_names == {
        "job_file_detail_limit",
        "job_event_detail_limit",
        "job_log_detail_limit",
        "job_failed_file_sample_limit",
        "job_recent_error_sample_limit",
        "job_summary_attention_shard_limit",
        "retained_control_event_limit_when_details_disabled",
    }
    assert not {
        "stale_seconds",
        "server_stale_seconds",
        "lease_seconds",
        "claim_batch_size",
        "retained_control_event_types",
    } & field_names


def test_legacy_control_limits_preserve_monkeypatched_negative_integers(
    monkeypatch,
) -> None:
    monkeypatch.setattr(limits_module, "JOB_FILE_DETAIL_LIMIT", -1)
    monkeypatch.setattr(limits_module, "JOB_EVENT_DETAIL_LIMIT", 0)
    monkeypatch.setattr(limits_module, "JOB_LOG_DETAIL_LIMIT", "invalid")
    monkeypatch.setattr(
        limits_module,
        "JOB_FAILED_FILE_SAMPLE_LIMIT",
        -3,
    )
    monkeypatch.setattr(
        limits_module,
        "JOB_RECENT_ERROR_SAMPLE_LIMIT",
        -4,
    )

    limits = legacy_control_limits()

    assert limits.job_file_detail_limit == -1
    assert limits.job_event_detail_limit == 0
    assert limits.job_log_detail_limit == 10_000
    assert limits.job_failed_file_sample_limit == -3
    assert limits.job_recent_error_sample_limit == -4
    assert limits.persist_job_file_details is True
    assert limits.persist_job_event_details is False


def test_control_runtime_prefers_explicit_limits_and_freezes_legacy_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine, settings = _session_factory(
        tmp_path,
        "runtime.db",
    )
    explicit = ControlLimits(job_file_detail_limit=3)
    explicit_runtime = build_control_runtime(
        settings=settings,
        limits=explicit,
        session_factory=session_factory,
    )
    assert explicit_runtime.limits is explicit

    monkeypatch.setattr(limits_module, "JOB_FILE_DETAIL_LIMIT", 0)
    first_runtime = build_control_runtime(
        settings=settings,
        session_factory=session_factory,
    )
    monkeypatch.setattr(limits_module, "JOB_FILE_DETAIL_LIMIT", 9)
    second_runtime = build_control_runtime(
        settings=settings,
        session_factory=session_factory,
    )

    assert first_runtime.limits.job_file_detail_limit == 0
    assert second_runtime.limits.job_file_detail_limit == 9
    assert first_runtime.limits.job_file_detail_limit == 0
    assert first_runtime.owns_engine is False
    engine.dispose()


def test_two_apps_keep_isolated_limits_and_ignore_late_service_patches(
    tmp_path,
    monkeypatch,
) -> None:
    service = _compatibility_service()
    first_factory, first_engine, first_settings = _session_factory(
        tmp_path,
        "first.db",
    )
    second_factory, second_engine, second_settings = _session_factory(
        tmp_path,
        "second.db",
    )
    monkeypatch.setattr(service, "JOB_FILE_DETAIL_LIMIT", 0)
    monkeypatch.setattr(service, "JOB_EVENT_DETAIL_LIMIT", 0)
    monkeypatch.setattr(service, "JOB_LOG_DETAIL_LIMIT", 0)
    first_app = create_app(
        runtime=build_control_runtime(
            settings=first_settings,
            session_factory=first_factory,
        )
    )
    monkeypatch.setattr(service, "JOB_FILE_DETAIL_LIMIT", 2)
    monkeypatch.setattr(service, "JOB_EVENT_DETAIL_LIMIT", 2)
    monkeypatch.setattr(service, "JOB_LOG_DETAIL_LIMIT", 2)
    second_app = create_app(
        runtime=build_control_runtime(
            settings=second_settings,
            session_factory=second_factory,
        )
    )
    monkeypatch.setattr(service, "JOB_FILE_DETAIL_LIMIT", 99)
    monkeypatch.setattr(service, "JOB_EVENT_DETAIL_LIMIT", 99)
    monkeypatch.setattr(service, "JOB_LOG_DETAIL_LIMIT", 99)

    assert first_app.state.control_limits.job_file_detail_limit == 0
    assert second_app.state.control_limits.job_file_detail_limit == 2

    for app in (first_app, second_app):
        client = TestClient(app)
        job_id = _create_job(client)
        for index in range(3):
            event = {
                "type": "file_started",
                "payload": {
                    "file_path": f"/shared/input/{index}.pdf",
                    "filename": f"{index}.pdf",
                    "total_pages": 1,
                },
            }
            assert client.post(
                f"/api/jobs/{job_id}/events",
                json=event,
            ).status_code == 200
            assert client.post(
                f"/api/jobs/{job_id}/logs",
                json={
                    "server_id": "worker-a",
                    "stream": "stdout",
                    "line": f"line {index}",
                },
            ).status_code == 200

    with first_factory() as session:
        assert session.query(JobFile).count() == 0
        assert session.query(JobEvent).count() == 0
        assert session.query(JobLog).count() == 0
    with second_factory() as session:
        assert session.query(JobFile).count() == 2
        assert session.query(JobEvent).count() == 2
        assert session.query(JobLog).count() == 2
    first_engine.dispose()
    second_engine.dispose()


def test_negative_one_keeps_unlimited_file_event_and_log_details(
    tmp_path,
) -> None:
    app, session_factory, engine = _app_with_limits(
        tmp_path,
        "unlimited.db",
        ControlLimits(
            job_file_detail_limit=-1,
            job_event_detail_limit=-1,
            job_log_detail_limit=-1,
        ),
    )
    client = TestClient(app)
    job_id = _create_job(client)
    for index in range(5):
        assert client.post(
            f"/api/jobs/{job_id}/events",
            json={
                "type": "file_started",
                "payload": {
                    "file_path": f"/shared/input/{index}.pdf",
                    "filename": f"{index}.pdf",
                },
            },
        ).status_code == 200
        assert client.post(
            f"/api/jobs/{job_id}/logs",
            json={
                "server_id": "worker-a",
                "stream": "stdout",
                "line": f"line {index}",
            },
        ).status_code == 200

    with session_factory() as session:
        assert session.query(JobFile).filter_by(job_id=job_id).count() == 5
        assert session.query(JobEvent).filter_by(job_id=job_id).count() == 5
        assert session.query(JobLog).filter_by(job_id=job_id).count() == 5
    engine.dispose()


def test_explicit_zero_and_small_limits_bound_details_and_samples(
    tmp_path,
) -> None:
    limits = ControlLimits(
        job_file_detail_limit=0,
        job_event_detail_limit=0,
        job_log_detail_limit=0,
        job_failed_file_sample_limit=1,
        job_recent_error_sample_limit=1,
        retained_control_event_limit_when_details_disabled=0,
    )
    app, session_factory, engine = _app_with_limits(
        tmp_path,
        "bounded.db",
        limits,
    )
    client = TestClient(app)
    job_id = _create_job(client)
    for index in range(2):
        assert client.post(
            f"/api/jobs/{job_id}/events",
            json={
                "type": "file_failed",
                "payload": {
                    "file_path": f"/shared/input/{index}.pdf",
                    "filename": f"{index}.pdf",
                    "error": f"failure {index}",
                    "failure_category": "inference_error",
                },
            },
        ).status_code == 200
    assert client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "manifest_scan_progress",
            "payload": {"status": "running", "scanned_files": 1},
        },
    ).status_code == 200
    assert client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "job_failed",
            "payload": {
                "error": "job failed",
                "failure_category": "inference_error",
            },
        },
    ).status_code == 200
    assert client.post(
        f"/api/jobs/{job_id}/logs",
        json={
            "server_id": "worker-a",
            "stream": "stderr",
            "line": "discarded",
        },
    ).status_code == 200

    with session_factory() as session:
        counter = session.get(JobCounter, job_id)
        assert len(json.loads(counter.recent_failed_files_json)) == 1
        assert len(json.loads(counter.recent_errors_json)) == 1
        assert session.query(JobFile).filter_by(job_id=job_id).count() == 0
        assert session.query(JobEvent).filter_by(job_id=job_id).count() == 0
        assert session.query(JobLog).filter_by(job_id=job_id).count() == 0
    engine.dispose()


def test_diagnostics_audit_uses_runtime_event_retention_snapshot(
    tmp_path,
) -> None:
    disabled_app, _, disabled_engine = _app_with_limits(
        tmp_path,
        "audit-disabled.db",
        ControlLimits(job_event_detail_limit=0),
    )
    unlimited_app, _, unlimited_engine = _app_with_limits(
        tmp_path,
        "audit-unlimited.db",
        ControlLimits(job_event_detail_limit=-1),
    )

    disabled = TestClient(disabled_app).get(
        "/api/system/diagnostics"
    ).json()
    unlimited = TestClient(unlimited_app).get(
        "/api/system/diagnostics"
    ).json()

    assert disabled["audit"]["execution"]["evidence_truncated"] is True
    assert unlimited["audit"]["execution"]["evidence_truncated"] is False
    disabled_engine.dispose()
    unlimited_engine.dispose()


def test_direct_audit_operations_preserve_local_global_compatibility(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine, _ = _session_factory(
        tmp_path,
        "audit-direct.db",
    )
    monkeypatch.setattr(
        diagnostics_operations,
        "PERSIST_JOB_EVENT_DETAILS",
        False,
    )
    monkeypatch.setattr(
        diagnostics_operations,
        "JOB_EVENT_DETAIL_LIMIT",
        -1,
    )
    with session_factory() as session:
        legacy = diagnostics_operations.audit_diagnostics(session)
        explicit = diagnostics_operations.audit_diagnostics(
            session,
            limits=ControlLimits(job_event_detail_limit=-1),
        )
        _, legacy_capacity_complete = (
            diagnostics_operations._throughput_evidence(
                session,
                now=datetime.now(timezone.utc),
            )
        )
        _, explicit_capacity_complete = (
            diagnostics_operations._throughput_evidence(
                session,
                now=datetime.now(timezone.utc),
                limits=ControlLimits(job_event_detail_limit=-1),
            )
        )

    assert legacy["execution"]["evidence_truncated"] is True
    assert explicit["execution"]["evidence_truncated"] is False
    assert legacy_capacity_complete is False
    assert explicit_capacity_complete is True
    engine.dispose()


def test_summary_attention_limit_uses_runtime_snapshot(tmp_path) -> None:
    app, session_factory, engine = _app_with_limits(
        tmp_path,
        "summary.db",
        ControlLimits(job_summary_attention_shard_limit=1),
    )
    client = TestClient(app)
    response = client.post(
        "/api/jobs",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
            "input_mode": "remote_folder_snapshot",
        },
    )
    assert response.status_code == 200
    job_id = response.json()["id"]
    with session_factory() as session:
        manifest = Manifest(
            job_id=job_id,
            input_mode="remote_folder_snapshot",
            input_root="/shared/input",
            manifest_path="/shared/manifests/manifest.jsonl",
            file_count=3,
            total_bytes=3,
        )
        session.add(manifest)
        session.flush()
        for index in range(3):
            session.add(
                WorkShard(
                    job_id=job_id,
                    manifest_id=manifest.id,
                    shard_index=index,
                    shard_path=f"/shared/shards/{index}.jsonl",
                    status="running",
                    file_count=1,
                )
            )
        session.commit()

    summary = client.get(f"/api/jobs/{job_id}/summary")

    assert summary.status_code == 200
    assert len(summary.json()["attention_shards"]) == 1
    engine.dispose()


def test_preflight_high_detail_warning_uses_runtime_limits(tmp_path) -> None:
    high_app, _, high_engine = _app_with_limits(
        tmp_path,
        "high.db",
        ControlLimits(job_file_detail_limit=100_001),
    )
    low_app, _, low_engine = _app_with_limits(
        tmp_path,
        "low.db",
        ControlLimits(job_file_detail_limit=100_000),
    )
    request = {
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
    }

    high_codes = {
        issue["code"]
        for issue in TestClient(high_app).post(
            "/api/jobs/preflight",
            json=request,
        ).json()["issues"]
    }
    low_codes = {
        issue["code"]
        for issue in TestClient(low_app).post(
            "/api/jobs/preflight",
            json=request,
        ).json()["issues"]
    }

    assert "high_detail_row_limits" in high_codes
    assert "high_detail_row_limits" not in low_codes
    high_engine.dispose()
    low_engine.dispose()


def test_explicit_router_limits_do_not_reread_legacy_globals(
    tmp_path,
    monkeypatch,
) -> None:
    app, _, engine = _app_with_limits(
        tmp_path,
        "explicit.db",
        ControlLimits(
            job_file_detail_limit=1,
            job_event_detail_limit=1,
            job_log_detail_limit=1,
        ),
    )
    client = TestClient(app)
    job_id = _create_job(client)

    def fail_legacy_read():
        raise AssertionError("hot path reread legacy control limits")

    monkeypatch.setattr(
        jobs_core,
        "__legacy_control_limits",
        fail_legacy_read,
    )
    monkeypatch.setattr(
        workers_core,
        "__legacy_control_limits",
        fail_legacy_read,
    )
    assert client.post(
        f"/api/jobs/{job_id}/events",
        json={
            "type": "file_started",
            "payload": {
                "file_path": "/shared/input/a.pdf",
                "filename": "a.pdf",
            },
        },
    ).status_code == 200
    assert client.post(
        f"/api/jobs/{job_id}/logs",
        json={
            "server_id": "worker-a",
            "stream": "stdout",
            "line": "one",
        },
    ).status_code == 200
    assert client.get(f"/api/jobs/{job_id}/summary").status_code == 200
    assert client.post(
        "/api/jobs/preflight",
        json={
            "input_dir": "/shared/input",
            "output_dir": "/shared/output",
            "engine": "dotsocr",
        },
    ).status_code == 200
    engine.dispose()


def test_direct_python_entries_resolve_one_legacy_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine, _ = _session_factory(tmp_path, "direct.db")
    app = create_app(session_factory=session_factory)
    job_id = _create_job(TestClient(app))
    job_limits_calls = 0
    worker_limits_calls = 0

    def job_limits():
        nonlocal job_limits_calls
        job_limits_calls += 1
        return ControlLimits()

    def worker_limits():
        nonlocal worker_limits_calls
        worker_limits_calls += 1
        return ControlLimits()

    monkeypatch.setattr(jobs_core, "__legacy_control_limits", job_limits)
    monkeypatch.setattr(workers_core, "__legacy_control_limits", worker_limits)
    with session_factory() as session:
        jobs_core.record_event(
            session,
            job_id,
            JobEventRequest(
                type="file_started",
                payload={
                    "file_path": "/shared/input/direct.pdf",
                    "filename": "direct.pdf",
                },
            ),
        )
        assert job_limits_calls == 1
        job_limits_calls = 0
        jobs_core.list_job_summaries(session)
        assert job_limits_calls == 1
        job_limits_calls = 0
        workers_core.preflight_job(
            session,
            JobCreateRequest(
                input_dir="/shared/input",
                output_dir="/shared/output",
                engine="dotsocr",
            ),
        )
        assert worker_limits_calls == 1
    engine.dispose()


def test_limits_module_only_exports_snapshot_api() -> None:
    assert limits_module.__all__ == [
        "ControlLimits",
        "legacy_control_limits",
    ]
    assert limits_module.ControlLimits is ControlLimits


def test_job_detail_hot_paths_keep_direct_session_call_baseline() -> None:
    module = ast.parse(inspect.getsource(jobs_core))
    functions = {
        node.name: node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def session_calls(function_name: str) -> Counter[str]:
        return Counter(
            call.func.attr
            for call in ast.walk(functions[function_name])
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "session"
            and call.func.attr in {
                "execute",
                "flush",
                "commit",
                "rollback",
                "refresh",
            }
        )

    assert session_calls("record_event") == {
        "flush": 2,
        "commit": 1,
        "refresh": 1,
    }
    assert session_calls("record_log") == {
        "execute": 2,
        "flush": 1,
        "commit": 2,
        "refresh": 1,
    }


def test_diagnostics_event_retention_globals_are_fallback_only() -> None:
    module = ast.parse(inspect.getsource(diagnostics_operations))
    consumers: dict[str, set[str]] = {
        "JOB_EVENT_DETAIL_LIMIT": set(),
        "PERSIST_JOB_EVENT_DETAILS": set(),
    }
    for function in (
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        for name in ast.walk(function):
            if isinstance(name, ast.Name) and name.id in consumers:
                consumers[name.id].add(function.name)

    assert consumers == {
        "JOB_EVENT_DETAIL_LIMIT": {
            "_throughput_evidence",
            "_trace_audit",
        },
        "PERSIST_JOB_EVENT_DETAILS": {
            "_throughput_evidence",
            "_trace_audit",
        },
    }
