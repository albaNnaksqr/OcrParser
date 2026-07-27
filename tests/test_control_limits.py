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
import ocr_platform.control.domains.manifests.core as manifests_core
import ocr_platform.control.domains.manifests.queries as manifest_queries
import ocr_platform.control.domains.diagnostics.metrics as diagnostics_metrics
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
    ScanUnit,
    Server,
    WorkShard,
)
from ocr_platform.control.schemas import (
    JobCreateRequest,
    JobEventRequest,
    ManifestIntegrityResponse,
    ManifestIntegrityShardIssue,
    ScanUnitCompleteRequest,
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


def _create_missing_shard_manifest(
    client: TestClient,
    session_factory,
    tmp_path,
    *,
    shard_count: int,
) -> tuple[str, int]:
    response = client.post(
        "/api/jobs",
        json={
            "input_dir": str(tmp_path / "input"),
            "output_dir": str(tmp_path / "output"),
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
            input_root=str(tmp_path / "input"),
            manifest_path=str(
                tmp_path / "missing" / job_id / "manifest.jsonl"
            ),
            file_count=shard_count,
            total_bytes=shard_count,
            status="ready",
        )
        session.add(manifest)
        session.flush()
        for index in range(1, shard_count + 1):
            session.add(
                WorkShard(
                    job_id=job_id,
                    manifest_id=manifest.id,
                    shard_index=index,
                    shard_path=str(
                        tmp_path
                        / "missing"
                        / job_id
                        / f"shard-{index:06d}.jsonl"
                    ),
                    status="pending",
                    file_count=1,
                )
            )
        session.commit()
        return job_id, manifest.id


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
    assert limits.manifest_integrity_issue_sample_limit == 0
    assert limits.diagnostics_evidence_row_limit == 10_000
    assert limits.metrics_trace_event_limit == 10_000
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
        "diagnostics_evidence_row_limit",
        "metrics_trace_event_limit",
        "manifest_integrity_issue_sample_limit",
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
    monkeypatch.setattr(
        limits_module,
        "MANIFEST_INTEGRITY_ISSUE_SAMPLE_LIMIT",
        -5,
    )

    limits = legacy_control_limits()

    assert limits.job_file_detail_limit == -1
    assert limits.job_event_detail_limit == 0
    assert limits.job_log_detail_limit == 10_000
    assert limits.job_failed_file_sample_limit == -3
    assert limits.job_recent_error_sample_limit == -4
    assert limits.manifest_integrity_issue_sample_limit == -5
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


def test_two_apps_isolate_evidence_and_trace_limits_from_global_drift(
    tmp_path,
    monkeypatch,
) -> None:
    zero_app, _, zero_engine = _app_with_limits(
        tmp_path,
        "observability-zero.db",
        ControlLimits(
            job_event_detail_limit=-1,
            diagnostics_evidence_row_limit=0,
            metrics_trace_event_limit=0,
        ),
    )
    two_app, _, two_engine = _app_with_limits(
        tmp_path,
        "observability-two.db",
        ControlLimits(
            job_event_detail_limit=-1,
            diagnostics_evidence_row_limit=2,
            metrics_trace_event_limit=2,
        ),
    )
    zero_client = TestClient(zero_app)
    two_client = TestClient(two_app)
    for client in (zero_client, two_client):
        job_id = _create_job(client)
        assert client.post(
            f"/api/jobs/{job_id}/events",
            json={
                "type": "page_done",
                "payload": {
                    "file_path": "/shared/input/a.pdf",
                    "filename": "a.pdf",
                    "page_no": 1,
                    "status": "success",
                    "stages": [
                        {
                            "stage": "recognition",
                            "status": "success",
                        }
                    ],
                },
            },
        ).status_code == 200

    monkeypatch.setattr(
        diagnostics_operations,
        "EVIDENCE_ROW_LIMIT",
        99,
    )
    monkeypatch.setattr(
        diagnostics_metrics,
        "TRACE_EVENT_LIMIT",
        99,
    )
    zero_diagnostics = zero_client.get(
        "/api/system/diagnostics"
    ).json()
    two_diagnostics = two_client.get(
        "/api/system/diagnostics"
    ).json()
    zero_metrics = zero_client.get("/api/system/metrics").text
    two_metrics = two_client.get("/api/system/metrics").text

    assert zero_diagnostics["capacity"] == {
        "available": False,
        "code": "capacity_diagnostics_unavailable",
    }
    assert two_diagnostics["capacity"]["available"] is True
    assert zero_diagnostics["audit"]["execution"][
        "evidence_truncated"
    ] is True
    assert two_diagnostics["audit"]["execution"][
        "evidence_truncated"
    ] is False
    assert "\nocr_platform_trace_window_truncated 1\n" in zero_metrics
    assert "ocr_platform_stage_outcomes{" not in zero_metrics
    assert "\nocr_platform_trace_window_truncated 0\n" in two_metrics
    assert "ocr_platform_stage_outcomes{" in two_metrics
    zero_engine.dispose()
    two_engine.dispose()


@pytest.mark.parametrize(
    ("sample_limit", "expected_samples"),
    [(0, 0), (2, 2), (-1, 0)],
)
def test_manifest_integrity_runtime_limit_bounds_samples_not_counts(
    tmp_path,
    sample_limit,
    expected_samples,
) -> None:
    app, session_factory, engine = _app_with_limits(
        tmp_path,
        f"manifest-{sample_limit}.db",
        ControlLimits(
            manifest_integrity_issue_sample_limit=sample_limit,
        ),
    )
    client = TestClient(app)
    job_id, _ = _create_missing_shard_manifest(
        client,
        session_factory,
        tmp_path / str(sample_limit),
        shard_count=3,
    )

    response = client.get(
        f"/api/jobs/{job_id}/manifest/integrity"
    )
    freeze = client.get(
        f"/api/jobs/{job_id}/manifest/freeze-report"
    )

    assert response.status_code == 200
    report = response.json()
    assert report["bad_shard_count"] == 3
    assert len(report["bad_shards"]) == expected_samples
    assert freeze.status_code == 200
    assert freeze.json()["report"]["integrity_bad_shard_count"] == 3
    assert len(
        freeze.json()["report"]["integrity_issue_samples"]
    ) == expected_samples
    engine.dispose()


def test_manifest_integrity_freeze_summary_caps_runtime_limit_at_five() -> None:
    report = ManifestIntegrityResponse(
        job_id="job-a",
        ok=False,
        status="failed",
        bad_shard_count=7,
        bad_shards=[
            ManifestIntegrityShardIssue(
                shard_id=index,
                shard_index=index,
                shard_path=f"/missing/{index}.jsonl",
                expected_file_count=1,
                reason="file_missing",
            )
            for index in range(1, 8)
        ],
    )

    two = manifests_core._manifest_integrity_freeze_summary(
        report,
        limits=ControlLimits(
            manifest_integrity_issue_sample_limit=2,
        ),
    )
    fifty = manifests_core._manifest_integrity_freeze_summary(
        report,
        limits=ControlLimits(),
    )

    assert two["integrity_issue_count"] == 9
    assert len(two["integrity_issue_samples"]) == 2
    assert fifty["integrity_issue_count"] == 9
    assert len(fifty["integrity_issue_samples"]) == 5


@pytest.mark.parametrize(
    ("sample_limit", "expected_samples"),
    [(0, 0), (2, 2), (50, 5), (-1, 0)],
)
def test_frozen_manifest_report_reapplies_runtime_sample_limit(
    tmp_path,
    sample_limit,
    expected_samples,
) -> None:
    app, session_factory, engine = _app_with_limits(
        tmp_path,
        f"frozen-{sample_limit}.db",
        ControlLimits(
            manifest_integrity_issue_sample_limit=sample_limit,
        ),
    )
    client = TestClient(app)
    job_id, manifest_id = _create_missing_shard_manifest(
        client,
        session_factory,
        tmp_path / str(sample_limit),
        shard_count=0,
    )
    samples = [
        {
            "kind": "shard",
            "shard_id": index,
            "shard_path": f"/historical/{index}.jsonl",
        }
        for index in range(1, 6)
    ]
    with session_factory() as session:
        manifest = session.get(Manifest, manifest_id)
        manifest.frozen_at = datetime.now(timezone.utc)
        manifest.freeze_report_json = json.dumps(
            {
                "frozen": True,
                "integrity_status": "failed",
                "integrity_issue_count": 9,
                "integrity_issue_samples": samples,
                "unrelated": {"preserved": True},
            }
        )
        session.commit()

    response = client.get(
        f"/api/jobs/{job_id}/manifest/freeze-report"
    )

    assert response.status_code == 200
    report = response.json()["report"]
    assert report["integrity_status"] == "failed"
    assert report["integrity_issue_count"] == 9
    assert len(report["integrity_issue_samples"]) == expected_samples
    assert report["unrelated"] == {"preserved": True}
    summary = client.get(f"/api/jobs/{job_id}/summary")
    assert summary.status_code == 200
    assert "integrity_issue_samples" not in summary.json()
    assert "/historical/" not in summary.text
    engine.dispose()


@pytest.mark.parametrize(
    "malformed_samples",
    [
        None,
        "/historical/private/path.jsonl",
        {"path": "/historical/private/path.jsonl"},
    ],
)
def test_frozen_manifest_report_redacts_nonlist_historical_samples(
    tmp_path,
    malformed_samples,
) -> None:
    app, session_factory, engine = _app_with_limits(
        tmp_path,
        "frozen-malformed.db",
        ControlLimits(manifest_integrity_issue_sample_limit=2),
    )
    client = TestClient(app)
    job_id, manifest_id = _create_missing_shard_manifest(
        client,
        session_factory,
        tmp_path,
        shard_count=0,
    )
    with session_factory() as session:
        manifest = session.get(Manifest, manifest_id)
        manifest.frozen_at = datetime.now(timezone.utc)
        manifest.freeze_report_json = json.dumps(
            {
                "frozen": True,
                "integrity_status": "failed",
                "integrity_issue_count": 7,
                "integrity_issue_samples": malformed_samples,
            }
        )
        session.commit()

    response = client.get(
        f"/api/jobs/{job_id}/manifest/freeze-report"
    )

    assert response.status_code == 200
    report = response.json()["report"]
    assert report["integrity_issue_count"] == 7
    assert report["integrity_issue_samples"] == []
    assert "/historical/private/path.jsonl" not in response.text
    engine.dispose()


def test_manifest_integrity_legacy_fallback_and_explicit_override(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine, settings = _session_factory(
        tmp_path,
        "manifest-direct.db",
    )
    app = create_app(
        runtime=build_control_runtime(
            settings=settings,
            session_factory=session_factory,
            limits=ControlLimits(),
        )
    )
    job_id, _ = _create_missing_shard_manifest(
        TestClient(app),
        session_factory,
        tmp_path,
        shard_count=3,
    )
    service = _compatibility_service()
    monkeypatch.setattr(
        service,
        "MANIFEST_INTEGRITY_ISSUE_SAMPLE_LIMIT",
        1,
    )

    with session_factory() as session:
        legacy = manifests_core.get_manifest_integrity_report(
            session,
            job_id,
        )
        explicit = manifests_core.get_manifest_integrity_report(
            session,
            job_id,
            limits=ControlLimits(
                manifest_integrity_issue_sample_limit=2,
            ),
        )

    assert legacy.bad_shard_count == 3
    assert len(legacy.bad_shards) == 1
    assert explicit.bad_shard_count == 3
    assert len(explicit.bad_shards) == 2
    assert ControlLimits.from_environment(
        {"OCR_MANIFEST_INTEGRITY_ISSUE_SAMPLE_LIMIT": "-1"}
    ).manifest_integrity_issue_sample_limit == 50
    engine.dispose()


def test_two_apps_isolate_manifest_integrity_limit_from_global_drift(
    tmp_path,
    monkeypatch,
) -> None:
    zero_app, zero_factory, zero_engine = _app_with_limits(
        tmp_path,
        "manifest-zero.db",
        ControlLimits(manifest_integrity_issue_sample_limit=0),
    )
    two_app, two_factory, two_engine = _app_with_limits(
        tmp_path,
        "manifest-two.db",
        ControlLimits(manifest_integrity_issue_sample_limit=2),
    )
    zero_client = TestClient(zero_app)
    two_client = TestClient(two_app)
    zero_job_id, _ = _create_missing_shard_manifest(
        zero_client,
        zero_factory,
        tmp_path / "zero",
        shard_count=3,
    )
    two_job_id, _ = _create_missing_shard_manifest(
        two_client,
        two_factory,
        tmp_path / "two",
        shard_count=3,
    )
    monkeypatch.setattr(
        _compatibility_service(),
        "MANIFEST_INTEGRITY_ISSUE_SAMPLE_LIMIT",
        99,
    )

    zero_report = zero_client.get(
        f"/api/jobs/{zero_job_id}/manifest/integrity"
    ).json()
    two_report = two_client.get(
        f"/api/jobs/{two_job_id}/manifest/integrity"
    ).json()

    assert zero_report["bad_shard_count"] == 3
    assert zero_report["bad_shards"] == []
    assert two_report["bad_shard_count"] == 3
    assert len(two_report["bad_shards"]) == 2
    zero_engine.dispose()
    two_engine.dispose()


def test_worker_manifest_integrity_write_and_historical_read_are_bounded(
    tmp_path,
) -> None:
    limits = ControlLimits(manifest_integrity_issue_sample_limit=2)
    app, session_factory, engine = _app_with_limits(
        tmp_path,
        "manifest-worker.db",
        limits,
    )
    client = TestClient(app)
    heartbeat = client.post(
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
    assert heartbeat.status_code == 200
    job_id, manifest_id = _create_missing_shard_manifest(
        client,
        session_factory,
        tmp_path,
        shard_count=0,
    )
    issues = [
        {
            "shard_id": index,
            "shard_index": index,
            "shard_path": f"/shared/missing/{index}.jsonl",
            "expected_file_count": 1,
            "reason": "file_missing",
        }
        for index in range(1, 4)
    ]
    with session_factory() as session:
        manifest = session.get(Manifest, manifest_id)
        manifest.manifest_path = (
            f"/shared/manifests/{job_id}/manifest.jsonl"
        )
        manifest.worker_integrity_status = "running"
        manifest.worker_integrity_server_id = "server-a"
        session.commit()

    complete = client.post(
        (
            f"/api/manifest-integrity/{manifest_id}/complete"
            "?server_id=server-a"
        ),
        json={
            "report": {
                "job_id": job_id,
                "manifest_id": manifest_id,
                "ok": False,
                "status": "failed",
                "bad_shard_count": 3,
                "bad_shards": issues,
            }
        },
    )

    assert complete.status_code == 200
    with session_factory() as session:
        manifest = session.get(Manifest, manifest_id)
        stored = json.loads(manifest.worker_integrity_report_json)
        assert stored["bad_shard_count"] == 3
        assert len(stored["bad_shards"]) == 2
        stored["bad_shards"] = issues
        manifest.worker_integrity_report_json = json.dumps(stored)
        session.commit()

    historical = client.get(
        f"/api/jobs/{job_id}/manifest/integrity"
    ).json()
    assert historical["source"] == "worker"
    assert historical["bad_shard_count"] == 3
    assert len(historical["bad_shards"]) == 2
    with session_factory() as session:
        manifest = session.get(Manifest, manifest_id)
        malformed = json.loads(
            manifest.worker_integrity_report_json
        )
        malformed["bad_shards"] = None
        manifest.worker_integrity_report_json = json.dumps(malformed)
        session.commit()

    fallback = client.get(
        f"/api/jobs/{job_id}/manifest/integrity"
    )
    assert fallback.status_code == 200
    assert fallback.json()["status"] == "not_accessible_from_control"
    engine.dispose()


def test_manifest_router_and_nested_freeze_use_one_runtime_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    limits = ControlLimits(manifest_integrity_issue_sample_limit=2)
    app, session_factory, engine = _app_with_limits(
        tmp_path,
        "manifest-identity.db",
        limits,
    )
    captured: list[ControlLimits] = []

    def fake_report(session, job_id, *, limits=None):
        captured.append(limits)
        return ManifestIntegrityResponse(
            job_id=job_id,
            ok=False,
            status="missing_manifest",
        )

    monkeypatch.setattr(
        manifest_queries,
        "get_manifest_integrity_report",
        fake_report,
    )
    response = TestClient(app).get(
        "/api/jobs/missing/manifest/integrity"
    )
    assert response.status_code == 200
    assert captured == [limits]

    job_id, _ = _create_missing_shard_manifest(
        TestClient(app),
        session_factory,
        tmp_path,
        shard_count=1,
    )
    legacy_calls = 0

    def legacy_limits():
        nonlocal legacy_calls
        legacy_calls += 1
        return limits

    monkeypatch.setattr(
        manifests_core,
        "__legacy_control_limits",
        legacy_limits,
    )
    with session_factory() as session:
        manifests_core.get_manifest_freeze_report(session, job_id)

    assert legacy_calls == 1
    engine.dispose()


def test_static_create_and_scan_completion_propagate_runtime_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine, _ = _session_factory(
        tmp_path,
        "manifest-paths.db",
    )
    limits = ControlLimits(manifest_integrity_issue_sample_limit=2)
    captured: list[ControlLimits] = []
    original_report = manifests_core.get_manifest_integrity_report

    def observed_report(session, job_id, *, limits=None):
        captured.append(limits)
        return original_report(
            session,
            job_id,
            limits=limits,
        )

    def fail_legacy_read():
        raise AssertionError("nested manifest path reread legacy limits")

    monkeypatch.setattr(
        manifests_core,
        "get_manifest_integrity_report",
        observed_report,
    )
    monkeypatch.setattr(
        manifests_core,
        "__legacy_control_limits",
        fail_legacy_read,
    )
    input_dir = tmp_path / "static-input"
    input_dir.mkdir()
    (input_dir / "a.pdf").write_bytes(b"%PDF-1.4\n")
    with session_factory() as session:
        session.add(
            Server(
                id="server-a",
                name="Server A",
                host="localhost",
            )
        )
        session.commit()
        jobs_core.create_job(
            session,
            JobCreateRequest(
                input_dir=str(input_dir),
                output_dir=str(tmp_path / "static-output"),
                engine="dotsocr",
                assigned_server_id="server-a",
                input_mode="folder_snapshot",
                manifest_root=str(tmp_path / "static-manifests"),
            ),
            limits=limits,
        )
        distributed = jobs_core.create_job(
            session,
            JobCreateRequest(
                input_dir=str(tmp_path / "distributed-input"),
                output_dir=str(tmp_path / "distributed-output"),
                engine="dotsocr",
                input_mode="distributed_remote_folder_snapshot",
                manifest_root=str(
                    tmp_path / "distributed-manifests"
                ),
            ),
            limits=limits,
        )
        unit = (
            session.query(ScanUnit)
            .filter_by(job_id=distributed.id)
            .one()
        )
        unit.status = "running"
        session.commit()
        manifests_core.complete_scan_unit(
            session,
            unit.id,
            ScanUnitCompleteRequest(),
            limits=limits,
        )

    assert captured == [limits, limits]
    engine.dispose()


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

    assert legacy["execution"]["evidence_truncated"] is True
    assert explicit["execution"]["evidence_truncated"] is False
    assert diagnostics_operations._resolve_event_retention(None) == (
        False,
        -1,
    )
    assert diagnostics_operations._resolve_event_retention(
        ControlLimits(job_event_detail_limit=-1)
    ) == (True, -1)
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


def test_evidence_and_trace_limits_keep_fixed_runtime_defaults_and_fallbacks(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        diagnostics_operations,
        "EVIDENCE_ROW_LIMIT",
        7,
    )
    monkeypatch.setattr(
        diagnostics_metrics,
        "TRACE_EVENT_LIMIT",
        9,
    )

    legacy = legacy_control_limits()

    assert legacy.diagnostics_evidence_row_limit == 10_000
    assert legacy.metrics_trace_event_limit == 10_000
    assert diagnostics_operations._resolve_evidence_limit(None) == 7
    assert diagnostics_metrics._resolve_trace_limit(None) == 9
    explicit = ControlLimits(
        diagnostics_evidence_row_limit=-1,
        metrics_trace_event_limit=-1,
    )
    assert diagnostics_operations._resolve_evidence_limit(explicit) == 0
    assert diagnostics_metrics._resolve_trace_limit(explicit) == 0


def test_evidence_row_boundary_is_zero_n_and_n_plus_one() -> None:
    assert diagnostics_operations._bounded_rows(
        [],
        evidence_limit=0,
    ) == []
    assert diagnostics_operations._bounded_rows(
        [1, 2],
        evidence_limit=2,
    ) == [1, 2]
    with pytest.raises(diagnostics_operations.EvidenceLimitExceeded):
        diagnostics_operations._bounded_rows(
            [1, 2, 3],
            evidence_limit=2,
        )


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


def test_manifest_limit_paths_keep_direct_session_call_baseline() -> None:
    def session_calls(module, function_names):
        tree = ast.parse(inspect.getsource(module))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            )
            and node.name in function_names
        }
        return {
            name: dict(
                Counter(
                    call.func.attr
                    for call in ast.walk(function)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "session"
                    and call.func.attr
                    in {
                        "execute",
                        "flush",
                        "commit",
                        "rollback",
                        "refresh",
                        "get",
                        "add",
                    }
                )
            )
            for name, function in functions.items()
        }

    assert session_calls(jobs_core, {"create_job"}) == {
        "create_job": {
            "add": 1,
            "refresh": 1,
            "get": 3,
            "flush": 1,
            "commit": 1,
            "rollback": 1,
        }
    }
    assert session_calls(
        manifests_core,
        {
            "_create_static_shards_for_job",
            "complete_scan_unit",
            "_build_manifest_freeze_report",
            "freeze_manifest_if_scan_complete",
            "get_manifest_freeze_report",
            "complete_worker_manifest_integrity_check",
            "get_manifest_integrity_report",
        },
    ) == {
        "_create_static_shards_for_job": {
            "add": 2,
            "flush": 2,
        },
        "complete_scan_unit": {
            "flush": 1,
            "commit": 2,
            "refresh": 2,
            "add": 2,
            "execute": 2,
        },
        "_build_manifest_freeze_report": {"execute": 1},
        "freeze_manifest_if_scan_complete": {"execute": 2},
        "get_manifest_freeze_report": {"execute": 1},
        "complete_worker_manifest_integrity_check": {
            "get": 1,
            "commit": 1,
        },
        "get_manifest_integrity_report": {"execute": 6},
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
        "JOB_EVENT_DETAIL_LIMIT": {"_resolve_event_retention"},
        "PERSIST_JOB_EVENT_DETAILS": {"_resolve_event_retention"},
    }


def test_diagnostics_and_metrics_limit_globals_are_fallback_only() -> None:
    expected = (
        (
            diagnostics_operations,
            "EVIDENCE_ROW_LIMIT",
            {"_resolve_evidence_limit"},
        ),
        (
            diagnostics_metrics,
            "TRACE_EVENT_LIMIT",
            {"_resolve_trace_limit"},
        ),
    )
    for module, global_name, expected_consumers in expected:
        tree = ast.parse(inspect.getsource(module))
        consumers = {
            function.name
            for function in tree.body
            if isinstance(
                function,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            )
            if any(
                isinstance(name, ast.Name)
                and name.id == global_name
                for name in ast.walk(function)
            )
        }
        assert consumers == expected_consumers


def test_diagnostics_and_metrics_keep_select_call_baseline() -> None:
    def session_calls(module) -> dict[str, dict[str, int]]:
        tree = ast.parse(inspect.getsource(module))
        result: dict[str, dict[str, int]] = {}
        for function in tree.body:
            if not isinstance(
                function,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            calls = Counter(
                call.func.attr
                for call in ast.walk(function)
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
            if calls:
                result[function.name] = dict(calls)
        return result

    assert session_calls(diagnostics_operations) == {
        "_worker_rows": {"execute": 1},
        "_running_shards_by_server": {"execute": 1},
        "_queue_and_lease_counts": {"execute": 2},
        "_throughput_evidence": {"execute": 1},
        "_remaining_pages": {"execute": 1},
        "_trace_audit": {"execute": 1},
        "audit_diagnostics": {"execute": 5},
    }
    assert session_calls(diagnostics_metrics) == {
        "_worker_samples": {"execute": 1},
        "_shard_queue_samples": {"execute": 1},
        "_failure_samples": {"execute": 1},
        "_trace_samples": {"execute": 1},
        "_artifact_samples": {"execute": 1},
    }
