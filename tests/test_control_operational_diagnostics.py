from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from ocr_platform.control import database
from ocr_platform.control.app import create_app
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.domains.diagnostics import operations
from ocr_platform.control.domains.diagnostics.operations import (
    alerts_diagnostics,
    audit_diagnostics,
    capacity_diagnostics,
)
from ocr_platform.control.domains.diagnostics.queries import (
    system_diagnostics,
    system_operational_diagnostics,
)
from ocr_platform.control.limits import ControlLimits
from ocr_platform.control.models import (
    Job,
    JobCounter,
    JobEvent,
    JobFile,
    Manifest,
    Server,
    ShardAttempt,
    WorkShard,
)
from ocr_platform.control.remote_workers import RemoteWorkerExecutor
from ocr_platform.control.settings import ControlSettings


NOW = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)
API_TOKEN = "operational-diagnostics-token"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
LEGACY_KEYS = {
    "ok",
    "service",
    "database",
    "api_auth",
    "workers",
    "issues",
}


def _runtime(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'diagnostics.db'}"
    session_factory, engine = create_session_factory(database_url)
    init_db(engine)
    settings = ControlSettings(
        database_url=database_url,
        api_token=API_TOKEN,
    )
    app = create_app(
        session_factory=session_factory,
        settings=settings,
    )
    return TestClient(app), session_factory, engine, settings


def _capabilities(
    *,
    constrained: bool = False,
    spool: dict[str, object] | None = None,
    shard_updates: dict[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {
        "shared_paths": [
            {
                "path": "/private/customer/shared",
                "exists": True,
                "readable": True,
                "writable": True,
            }
        ],
        "resource_pressure": {
            "constrained": constrained,
        },
    }
    if spool is not None:
        payload["event_spool"] = spool
    if shard_updates is not None:
        payload["pending_shard_updates"] = shard_updates
    return json.dumps(payload)


def _server(
    server_id: str,
    *,
    capacity: int = 1,
    status: str = "online",
    heartbeat: datetime | None = NOW,
    constrained: bool = False,
    archived: bool = False,
    spool: dict[str, object] | None = None,
    shard_updates: dict[str, object] | None = None,
) -> Server:
    return Server(
        id=server_id,
        name=f"{server_id}-name",
        host=f"{server_id}.private.invalid",
        status=status,
        capacity_slots=capacity,
        last_heartbeat_at=heartbeat,
        archived_at=NOW if archived else None,
        capabilities_json=_capabilities(
            constrained=constrained,
            spool=spool,
            shard_updates=shard_updates,
        ),
    )


def _job_manifest(
    session,
    *,
    job_id: str,
    server_id: str,
    status: str = "running",
    integrity_status: str | None = None,
    integrity_report: str = "{}",
) -> tuple[Job, Manifest]:
    job = Job(
        id=job_id,
        input_dir=f"/private/customer/{job_id}/input",
        output_dir=f"/private/customer/{job_id}/output",
        engine="dotsocr",
        assigned_server_id=server_id,
        status=status,
    )
    session.add(job)
    session.flush()
    manifest = Manifest(
        job_id=job.id,
        input_mode="existing_manifest",
        manifest_path=f"/private/customer/{job_id}/manifest.jsonl",
        status="ready",
        worker_integrity_status=integrity_status,
        worker_integrity_report_json=integrity_report,
    )
    session.add(manifest)
    session.flush()
    return job, manifest


def _shard(
    *,
    job: Job,
    manifest: Manifest,
    index: int,
    status: str,
    server_id: str | None = None,
    lease_expires_at: datetime | None = None,
) -> WorkShard:
    return WorkShard(
        job_id=job.id,
        manifest_id=manifest.id,
        shard_index=index,
        shard_path=f"/private/customer/{job.id}/shard-{index}.jsonl",
        status=status,
        assigned_server_id=server_id,
        lease_expires_at=lease_expires_at,
    )


def _seed_rate(
    session,
    *,
    page_count: int,
    total_pages: int | None = 110,
    completed_pages: int = 10,
) -> None:
    session.add(_server("rate-worker"))
    job, manifest = _job_manifest(
        session,
        job_id="rate-job",
        server_id="rate-worker",
    )
    manifest.file_count = 1
    if total_pages is not None:
        session.add(
            JobCounter(
                job_id=job.id,
                total_pages=total_pages,
                completed_pages=completed_pages,
                started_files=1,
            )
        )
    session.add_all(
        [
            JobEvent(
                job_id=job.id,
                event_type="page_done",
                file_path=f"/private/customer/document-{page_no}.pdf",
                page_no=page_no,
                created_at=NOW - timedelta(seconds=page_no),
                payload_json="{}",
            )
            for page_no in range(1, page_count + 1)
        ]
    )
    session.commit()


def _database_drift_status() -> dict[str, object]:
    return {
        "dialect": "sqlite",
        "schema_migrations_table_exists": False,
        "migration_checksum_column_exists": False,
        "known_migrations": [],
        "applied_migrations": [],
        "missing_migrations": ["0001"],
        "unexpected_migrations": [],
        "checksum_mismatches": [],
        "missing_checksums": [],
        "latest_applied_migration": None,
        "is_current": False,
    }


def _database_current_status() -> dict[str, object]:
    return {
        **_database_drift_status(),
        "schema_migrations_table_exists": True,
        "migration_checksum_column_exists": True,
        "missing_migrations": [],
        "unexpected_migrations": [],
        "is_current": True,
    }


def test_in_memory_sqlite_http_keeps_all_sections_available_and_quiet(
    monkeypatch,
):
    session_factory, engine = create_session_factory("sqlite://")
    init_db(engine)
    settings = ControlSettings(
        database_url="sqlite://",
        api_token=API_TOKEN,
    )
    app = create_app(
        session_factory=session_factory,
        settings=settings,
    )
    monkeypatch.setattr(
        database,
        "describe_database_status",
        lambda _bind: _database_current_status(),
    )

    response = TestClient(app).get(
        "/api/system/diagnostics",
        headers=AUTH,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capacity"]["available"] is True
    assert payload["audit"]["available"] is True
    assert payload["alerts"]["available"] is True
    assert payload["capacity"]["recommendation_codes"] == []
    assert payload["alerts"]["active"] == []


def test_operational_endpoint_preserves_legacy_six_fields_exactly(tmp_path):
    client, session_factory, _, settings = _runtime(tmp_path)
    with session_factory() as session:
        legacy = system_diagnostics(
            session,
            strict_production=True,
            settings=settings,
        )
        operational = system_operational_diagnostics(
            session,
            strict_production=True,
            settings=settings,
            now=NOW,
        )

    assert {key: operational[key] for key in LEGACY_KEYS} == legacy
    assert set(operational) == LEGACY_KEYS | {
        "capacity",
        "audit",
        "alerts",
    }
    response = client.get("/api/system/diagnostics", headers=AUTH)
    assert response.status_code == 200
    assert LEGACY_KEYS | {"capacity", "audit", "alerts"} == set(
        response.json()
    )


def test_capacity_counts_ready_and_available_slots_in_bulk(tmp_path):
    _, session_factory, _, _ = _runtime(tmp_path)
    with session_factory() as session:
        session.add_all(
            [
                _server("ready-a", capacity=4),
                _server("ready-b", capacity=2, status="busy"),
                _server(
                    "stale",
                    capacity=20,
                    heartbeat=NOW - timedelta(hours=1),
                ),
                _server("archived", capacity=20, archived=True),
                _server("offline", capacity=20, status="offline"),
                _server("constrained", capacity=20, constrained=True),
                _server("negative", capacity=-5),
            ]
        )
        job, manifest = _job_manifest(
            session,
            job_id="capacity-job",
            server_id="ready-a",
        )
        session.add_all(
            [
                _shard(
                    job=job,
                    manifest=manifest,
                    index=1,
                    status="running",
                    server_id="ready-a",
                ),
                _shard(
                    job=job,
                    manifest=manifest,
                    index=2,
                    status="running",
                    server_id="ready-b",
                ),
                _shard(
                    job=job,
                    manifest=manifest,
                    index=3,
                    status="running",
                    server_id="ready-b",
                ),
                _shard(
                    job=job,
                    manifest=manifest,
                    index=4,
                    status="pending",
                ),
                _shard(
                    job=job,
                    manifest=manifest,
                    index=5,
                    status="retrying",
                ),
                _shard(
                    job=job,
                    manifest=manifest,
                    index=6,
                    status="stale",
                ),
                _shard(
                    job=job,
                    manifest=manifest,
                    index=7,
                    status="running",
                    server_id="stale",
                    lease_expires_at=NOW - timedelta(seconds=1),
                ),
            ]
        )
        terminal, terminal_manifest = _job_manifest(
            session,
            job_id="terminal-residue",
            server_id="ready-a",
            status="succeeded",
        )
        session.add(
            _shard(
                job=terminal,
                manifest=terminal_manifest,
                index=1,
                status="pending",
            )
        )
        session.commit()

        capacity = capacity_diagnostics(session, now=NOW)

    assert capacity["ready_worker_slots"] == 6
    assert capacity["available_worker_slots"] == 3
    assert capacity["pending_shard_queue_depth"] == 3
    assert capacity["running_shards"] == 4
    assert capacity["stale_leases"] == 1


def test_capacity_clamps_overcapacity_worker_to_zero_available(tmp_path):
    _, session_factory, _, _ = _runtime(tmp_path)
    with session_factory() as session:
        session.add(_server("overcapacity", capacity=1))
        job, manifest = _job_manifest(
            session,
            job_id="overcapacity-job",
            server_id="overcapacity",
        )
        session.add_all(
            [
                _shard(
                    job=job,
                    manifest=manifest,
                    index=index,
                    status="running",
                    server_id="overcapacity",
                )
                for index in (1, 2)
            ]
        )
        session.commit()

        capacity = capacity_diagnostics(session, now=NOW)

    assert capacity["ready_worker_slots"] == 1
    assert capacity["available_worker_slots"] == 0


def test_stopping_job_occupies_slots_and_reports_stale_lease_not_queue(
    tmp_path,
):
    _, session_factory, _, _ = _runtime(tmp_path)
    with session_factory() as session:
        session.add(_server("stopping-worker", capacity=2))
        job, manifest = _job_manifest(
            session,
            job_id="stopping-job",
            server_id="stopping-worker",
            status="stopping",
        )
        session.add_all(
            [
                _shard(
                    job=job,
                    manifest=manifest,
                    index=1,
                    status="running",
                    server_id="stopping-worker",
                    lease_expires_at=NOW - timedelta(seconds=1),
                ),
                _shard(
                    job=job,
                    manifest=manifest,
                    index=2,
                    status="pending",
                ),
            ]
        )
        session.commit()

        capacity = capacity_diagnostics(session, now=NOW)

    assert capacity["ready_worker_slots"] == 2
    assert capacity["available_worker_slots"] == 1
    assert capacity["running_shards"] == 1
    assert capacity["stale_leases"] == 1
    assert capacity["pending_shard_queue_depth"] == 0


@pytest.mark.parametrize(
    ("page_count", "confidence", "eta"),
    [
        (0, "none", None),
        (9, "none", None),
        (10, "low", 36_000),
        (100, "medium", 3_600),
    ],
)
def test_capacity_rate_thresholds_and_eta(
    tmp_path,
    page_count,
    confidence,
    eta,
):
    _, session_factory, _, _ = _runtime(tmp_path)
    with session_factory() as session:
        _seed_rate(session, page_count=page_count)
        capacity = capacity_diagnostics(session, now=NOW)

    assert capacity["sample_pages"] == page_count
    assert capacity["observed_pages_per_hour"] == float(page_count)
    assert capacity["confidence"] == confidence
    assert capacity["estimated_drain_seconds"] == eta


def test_capacity_disables_eta_for_unknown_remaining_or_truncated_window(
    tmp_path,
    monkeypatch,
):
    _, session_factory, _, _ = _runtime(tmp_path)
    with session_factory() as session:
        _seed_rate(session, page_count=10, total_pages=None)
        unknown = capacity_diagnostics(session, now=NOW)
    assert unknown["sample_pages"] == 10
    assert unknown["confidence"] == "none"
    assert unknown["estimated_drain_seconds"] is None

    other_dir = tmp_path / "truncated"
    other_dir.mkdir()
    _, session_factory, _, _ = _runtime(other_dir)
    monkeypatch.setattr(operations, "EVIDENCE_ROW_LIMIT", 10)
    with session_factory() as session:
        _seed_rate(session, page_count=11)
        truncated = capacity_diagnostics(session, now=NOW)
    assert truncated["sample_pages"] == 10
    assert truncated["confidence"] == "none"
    assert truncated["estimated_drain_seconds"] is None


def test_capacity_disables_eta_when_event_details_are_disabled(
    tmp_path,
    monkeypatch,
):
    _, session_factory, _, _ = _runtime(tmp_path)
    monkeypatch.setattr(operations, "PERSIST_JOB_EVENT_DETAILS", False)
    with session_factory() as session:
        _seed_rate(session, page_count=10)
        capacity = capacity_diagnostics(session, now=NOW)
    assert capacity["sample_pages"] == 10
    assert capacity["confidence"] == "none"
    assert capacity["estimated_drain_seconds"] is None


@pytest.mark.parametrize("retention_limit", [5, 10])
def test_capacity_disables_eta_when_retention_limit_cannot_cover_window_limit(
    tmp_path,
    monkeypatch,
    retention_limit,
):
    _, session_factory, _, _ = _runtime(tmp_path)
    monkeypatch.setattr(operations, "EVIDENCE_ROW_LIMIT", 10)
    monkeypatch.setattr(operations, "JOB_EVENT_DETAIL_LIMIT", retention_limit)
    with session_factory() as session:
        _seed_rate(session, page_count=10)
        capacity = capacity_diagnostics(session, now=NOW)
    assert capacity["sample_pages"] == 10
    assert capacity["confidence"] == "none"
    assert capacity["estimated_drain_seconds"] is None


def test_capacity_evidence_limit_covers_zero_n_and_n_plus_one(
    tmp_path,
    monkeypatch,
):
    _, session_factory, _, _ = _runtime(tmp_path)
    monkeypatch.setattr(operations, "EVIDENCE_ROW_LIMIT", 2)
    with session_factory() as session:
        session.add_all(
            [
                _server(f"bounded-worker-{index}")
                for index in range(3)
            ]
        )
        session.commit()

        with pytest.raises(operations.EvidenceLimitExceeded):
            capacity_diagnostics(session, now=NOW)
        with pytest.raises(operations.EvidenceLimitExceeded):
            capacity_diagnostics(
                session,
                now=NOW,
                limits=ControlLimits(
                    diagnostics_evidence_row_limit=-1,
                ),
            )
        with pytest.raises(operations.EvidenceLimitExceeded):
            capacity_diagnostics(
                session,
                now=NOW,
                limits=ControlLimits(
                    diagnostics_evidence_row_limit=2,
                ),
            )
        capacity = capacity_diagnostics(
            session,
            now=NOW,
            limits=ControlLimits(
                diagnostics_evidence_row_limit=3,
            ),
        )

    assert capacity["ready_worker_slots"] == 3


def test_audit_explicit_evidence_limit_overrides_direct_global(
    tmp_path,
    monkeypatch,
):
    _, session_factory, _, _ = _runtime(tmp_path)
    monkeypatch.setattr(operations, "EVIDENCE_ROW_LIMIT", 0)
    with session_factory() as session:
        session.add(_server("audit-evidence-worker"))
        _job_manifest(
            session,
            job_id="audit-evidence-job",
            server_id="audit-evidence-worker",
        )
        session.commit()

        with pytest.raises(operations.EvidenceLimitExceeded):
            audit_diagnostics(session, now=NOW)
        audit = audit_diagnostics(
            session,
            now=NOW,
            limits=ControlLimits(
                diagnostics_evidence_row_limit=1,
            ),
        )

    assert audit["available"] is True


def test_capacity_freezes_direct_evidence_limit_before_helpers(
    tmp_path,
    monkeypatch,
):
    _, session_factory, _, _ = _runtime(tmp_path)
    seen: list[int] = []
    original = operations._worker_rows
    monkeypatch.setattr(operations, "EVIDENCE_ROW_LIMIT", 3)

    def drift_global_after_entry(session, *, evidence_limit):
        seen.append(evidence_limit)
        monkeypatch.setattr(operations, "EVIDENCE_ROW_LIMIT", 0)
        return original(
            session,
            evidence_limit=evidence_limit,
        )

    monkeypatch.setattr(
        operations,
        "_worker_rows",
        drift_global_after_entry,
    )
    with session_factory() as session:
        capacity_diagnostics(session, now=NOW)

    assert seen == [3]
    assert operations.EVIDENCE_ROW_LIMIT == 0


def test_capacity_rate_deduplicates_page_identity(tmp_path):
    _, session_factory, _, _ = _runtime(tmp_path)
    with session_factory() as session:
        _seed_rate(session, page_count=10)
        session.add(
            JobEvent(
                job_id="rate-job",
                event_type="page_done",
                file_path="/private/customer/document-1.pdf",
                page_no=1,
                created_at=NOW,
                payload_json='{"private":"ignored"}',
            )
        )
        session.commit()
        capacity = capacity_diagnostics(session, now=NOW)

    assert capacity["sample_pages"] == 10
    assert capacity["observed_pages_per_hour"] == 10.0
    assert capacity["estimated_drain_seconds"] == 36_000


def test_audit_and_alerts_report_only_bounded_persisted_evidence(tmp_path):
    _, session_factory, _, _ = _runtime(tmp_path)
    secret = "private-customer-secret"
    with session_factory() as session:
        session.add_all(
            [
                _server(
                    "audit-worker",
                    heartbeat=NOW - timedelta(hours=1),
                    spool={
                        "dir": f"/private/{secret}/spool",
                        "pending_events": 3,
                        "failed_events": 2,
                        "dropped_events": 1,
                        "pending_logs": 4,
                        "failed_logs": 1,
                        "dropped_logs": 2,
                    },
                    shard_updates={"pending": 5, "failed": 1},
                ),
                _server("constrained-alert-worker", constrained=True),
            ]
        )
        job, manifest = _job_manifest(
            session,
            job_id="audit-job",
            server_id="audit-worker",
            integrity_status="failed",
            integrity_report=f'{{"private":"{secret}"}}',
        )
        shard = _shard(
            job=job,
            manifest=manifest,
            index=1,
            status="running",
            server_id="audit-worker",
            lease_expires_at=NOW - timedelta(seconds=1),
        )
        session.add(shard)
        session.flush()
        session.add_all(
            [
                ShardAttempt(
                    job_id=job.id,
                    shard_id=shard.id,
                    attempt_number=1,
                    server_id="audit-worker",
                    status="failed",
                    failure_category=secret,
                ),
                JobEvent(
                    job_id=job.id,
                    event_type="page_done",
                    file_path=f"/private/{secret}/document.pdf",
                    page_no=1,
                    created_at=NOW,
                    payload_json=json.dumps(
                        {
                            "stages": [
                                {
                                    "stage": "layout",
                                    "status": "failed",
                                    "failure_category": secret,
                                }
                            ],
                            "fallback": {
                                "used": True,
                                "source_stage": "layout",
                                "reason": secret,
                            },
                        }
                    ),
                ),
                JobFile(
                    job_id=job.id,
                    file_path=f"/private/{secret}/declared.pdf",
                    filename="declared.pdf",
                    status="success",
                    output_path=f"/private/{secret}/declared.md",
                ),
                JobFile(
                    job_id=job.id,
                    file_path=f"/private/{secret}/missing.pdf",
                    filename="missing.pdf",
                    status="success",
                    output_path=None,
                ),
            ]
        )
        session.commit()

        capacity = capacity_diagnostics(session, now=NOW)
        audit = audit_diagnostics(session, now=NOW)
        alerts = alerts_diagnostics(
            session,
            database_status=_database_drift_status(),
            capacity=capacity,
            audit=audit,
            now=NOW,
        )

    assert audit["manifest_integrity"] == {
        "status_counts": {"failed": 1},
        "reports_present": 1,
        "reports_missing": 0,
    }
    assert audit["shard_attempts"]["status_counts"] == {"failed": 1}
    assert audit["shard_attempts"]["failure_category_counts"] == {
        "other": 1
    }
    assert audit["execution"]["stage_status_counts"] == {
        "layout.failed": 1
    }
    assert audit["execution"]["stage_failure_category_counts"] == {
        "other": 1
    }
    assert audit["execution"]["fallback_category_counts"] == {"other": 1}
    assert audit["artifacts"] == {
        "declared_records": 1,
        "status_counts": {"success": 1},
        "missing_declared_records": 1,
        "completed_jobs": 0,
    }
    assert audit["output_audit"] == {
        "status": "not_reported",
        "evidence_available": False,
    }
    active = {item["code"]: item for item in alerts["active"]}
    assert active["event_spool_backlog"]["count"] == 6
    assert active["log_spool_backlog"]["count"] == 7
    assert active["shard_update_spool_backlog"]["count"] == 6
    for code in (
        "migration_drift",
        "stale_shard_lease",
        "stage_failures",
        "fallback_usage",
        "manifest_integrity_attention",
        "artifact_records_missing",
        "output_audit_not_reported",
        "stale_worker_heartbeat",
        "worker_resource_pressure",
    ):
        assert code in active
    assert all(
        set(item)
        == {"code", "severity", "count", "recommendation_code"}
        for item in [*alerts["active"], *alerts["templates"]]
    )
    assert {
        item["recommendation_code"]
        for item in alerts["templates"]
    } == operations.RECOMMENDATION_CODES
    rendered = json.dumps({"audit": audit, "alerts": alerts})
    assert secret not in rendered
    assert "/private/" not in rendered


def test_migration_alert_counts_unexpected_rows_without_name_leak():
    secret = "private-unexpected-migration-name"
    status = _database_drift_status()
    status["missing_migrations"] = []
    status["schema_migrations_table_exists"] = True
    status["unexpected_migrations"] = [secret]

    alerts = operations.migration_alerts(status)

    assert alerts == [
        {
            "code": "migration_drift",
            "severity": "error",
            "count": 1,
            "recommendation_code": "apply_pending_migrations",
        }
    ]
    assert secret not in json.dumps(alerts)


def test_output_audit_template_is_inactive_without_jobs_or_artifacts(tmp_path):
    _, session_factory, _, _ = _runtime(tmp_path)
    with session_factory() as session:
        capacity = capacity_diagnostics(session, now=NOW)
        audit = audit_diagnostics(session, now=NOW)
        alerts = alerts_diagnostics(
            session,
            database_status={
                **_database_drift_status(),
                "schema_migrations_table_exists": True,
                "missing_migrations": [],
                "is_current": True,
            },
            capacity=capacity,
            audit=audit,
            now=NOW,
        )

    assert audit["output_audit"] == {
        "status": "not_reported",
        "evidence_available": False,
    }
    assert "output_audit_not_reported" in {
        item["code"] for item in alerts["templates"]
    }
    assert "output_audit_not_reported" not in {
        item["code"] for item in alerts["active"]
    }


@pytest.mark.parametrize(
    ("builder_name", "failed_section"),
    [
        ("capacity_diagnostics", "capacity"),
        ("audit_diagnostics", "audit"),
        ("alerts_diagnostics", "alerts"),
    ],
)
def test_sections_fail_independently_without_leaking_exception_text(
    tmp_path,
    monkeypatch,
    builder_name,
    failed_section,
):
    _, session_factory, _, settings = _runtime(tmp_path)
    secret = "private-section-exception"
    monkeypatch.setattr(
        operations,
        builder_name,
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError(secret)
        ),
    )
    with session_factory() as session:
        payload = system_operational_diagnostics(
            session,
            strict_production=True,
            settings=settings,
            now=NOW,
        )

    assert payload[failed_section]["available"] is False
    assert payload[failed_section]["code"] == (
        f"{failed_section}_diagnostics_unavailable"
    )
    for section in {"capacity", "audit", "alerts"} - {failed_section}:
        assert payload[section]["available"] is True
    assert secret not in json.dumps(payload)


def test_malformed_capabilities_reports_payloads_and_page_keys_are_safe(
    tmp_path,
):
    client, session_factory, _, _ = _runtime(tmp_path)
    with session_factory() as session:
        server = _server("malformed-worker")
        server.capabilities_json = "{"
        session.add(server)
        job, _ = _job_manifest(
            session,
            job_id="malformed-job",
            server_id=server.id,
            integrity_status="failed",
            integrity_report="{",
        )
        session.add_all(
            [
                JobCounter(
                    job_id=job.id,
                    total_pages=100,
                    completed_pages=0,
                ),
                JobEvent(
                    job_id=job.id,
                    event_type="page_done",
                    file_path=None,
                    page_no=None,
                    created_at=NOW,
                    payload_json="{",
                ),
            ]
        )
        session.commit()

        capacity = capacity_diagnostics(session, now=NOW)
        audit = audit_diagnostics(session, now=NOW)

    assert capacity["sample_pages"] == 0
    assert capacity["confidence"] == "none"
    assert capacity["estimated_drain_seconds"] is None
    assert audit["manifest_integrity"]["reports_present"] == 1
    assert audit["execution"]["stage_status_counts"] == {}
    response = client.get("/api/system/diagnostics", headers=AUTH)
    assert response.status_code == 200


def test_schema_drift_sections_fail_closed_and_keep_migration_alert(
    tmp_path,
    monkeypatch,
):
    client, _, engine, _ = _runtime(tmp_path)
    monkeypatch.setattr(
        database,
        "describe_database_status",
        lambda _bind: _database_drift_status(),
    )
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE servers"))

    response = client.get("/api/system/diagnostics", headers=AUTH)

    assert response.status_code == 200
    payload = response.json()
    assert payload["capacity"]["available"] is False
    assert payload["alerts"]["available"] is False
    assert payload["alerts"]["active"] == [
        {
            "code": "migration_drift",
            "severity": "error",
            "count": 1,
            "recommendation_code": "apply_pending_migrations",
        }
    ]
    assert "no such table" not in response.text.lower()
    assert "servers" not in payload["alerts"]


def test_sqlite_missing_audit_table_does_not_disable_other_sections(
    tmp_path,
    monkeypatch,
):
    client, _, engine, _ = _runtime(tmp_path)
    monkeypatch.setattr(
        database,
        "describe_database_status",
        lambda _bind: _database_current_status(),
    )
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE shard_attempts"))

    response = client.get("/api/system/diagnostics", headers=AUTH)

    assert response.status_code == 200
    payload = response.json()
    assert payload["capacity"]["available"] is True
    assert payload["audit"] == {
        "available": False,
        "code": "audit_diagnostics_unavailable",
    }
    assert payload["alerts"]["available"] is True


def test_operational_diagnostics_never_accesses_files_or_remote_executor(
    tmp_path,
):
    client, _, _, _ = _runtime(tmp_path)
    with (
        patch.object(
            database,
            "describe_database_status",
            return_value=_database_drift_status(),
        ),
        patch.object(
            Path,
            "open",
            side_effect=AssertionError("filesystem access"),
        ),
        patch.object(
            Path,
            "read_text",
            side_effect=AssertionError("filesystem access"),
        ),
        patch.object(
            Path,
            "glob",
            side_effect=AssertionError("filesystem access"),
        ),
        patch.object(
            RemoteWorkerExecutor,
            "_run_ssh",
            side_effect=AssertionError("remote executor called"),
        ),
    ):
        response = client.get("/api/system/diagnostics", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["audit"]["output_audit"] == {
        "status": "not_reported",
        "evidence_available": False,
    }


def test_operational_diagnostics_is_select_only_without_flush_or_commit(
    tmp_path,
    monkeypatch,
):
    _, session_factory, engine, settings = _runtime(tmp_path)
    statements: list[str] = []

    def capture(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        statements.append(str(statement).strip())

    monkeypatch.setattr(
        database,
        "describe_database_status",
        lambda _bind: _database_drift_status(),
    )
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with (
            patch.object(
                Session,
                "commit",
                side_effect=AssertionError("diagnostics committed"),
            ),
            patch.object(
                Session,
                "flush",
                side_effect=AssertionError("diagnostics flushed"),
            ),
        ):
            with session_factory() as session:
                payload = system_operational_diagnostics(
                    session,
                    strict_production=True,
                    settings=settings,
                    now=NOW,
                )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert payload["capacity"]["available"] is True
    assert statements
    assert all(statement.upper().startswith("SELECT") for statement in statements)
    rate_query = next(
        statement
        for statement in statements
        if "job_events.file_path" in statement
    ).lower()
    assert "job_events.created_at >=" in rate_query
    assert "job_events.created_at <=" in rate_query
    assert "payload_json" not in rate_query
    assert " limit " in f" {' '.join(rate_query.split())} "
