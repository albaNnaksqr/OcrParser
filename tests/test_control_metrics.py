from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from ocr_parser.contracts.observability import (
    ENGINE_LABEL_VALUES,
    FAILURE_CATEGORY_LABEL_VALUES,
    FALLBACK_CATEGORY_LABEL_VALUES,
    STAGE_LABEL_VALUES,
    STATUS_LABEL_VALUES,
)
from ocr_platform.control.app import create_app
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.domains.diagnostics import metrics as control_metrics
from ocr_platform.control.domains.diagnostics.metrics import (
    ALLOWED_LABEL_KEYS,
    FAMILY_BY_NAME,
    PROMETHEUS_CONTENT_TYPE,
    encode_prometheus,
    metrics_snapshot,
)
from ocr_platform.control.models import (
    Job,
    JobEvent,
    JobFile,
    Manifest,
    Server,
    WorkShard,
)
from ocr_platform.control.readiness import DatabaseReadiness
from ocr_platform.control.settings import ControlSettings


API_TOKEN = "metrics-test-token"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
MALICIOUS_JOB_ID = "customer-job-secret"
MALICIOUS_ENGINE = "customer-engine-secret"
MALICIOUS_PATH = "/private/customer/acme/document.pdf"
MALICIOUS_ERROR = "customer-secret-error-text"
MALICIOUS_CATEGORY = "customer-category-secret"


def _client(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'metrics.db'}"
    session_factory, engine = create_session_factory(database_url)
    init_db(engine)
    app = create_app(
        session_factory=session_factory,
        settings=ControlSettings(
            database_url=database_url,
            api_token=API_TOKEN,
        ),
    )
    return TestClient(app), app, session_factory, engine


def _seed_hostile_persisted_evidence(session_factory) -> None:
    with session_factory() as session:
        server = Server(
            id="metrics-worker",
            name="Customer Secret Worker",
            host="private.customer.internal",
            status="customer-worker-status",
            capacity_slots=3,
            capabilities_json="{",
        )
        session.add(server)
        session.flush()
        job = Job(
            id=MALICIOUS_JOB_ID,
            input_dir=MALICIOUS_PATH,
            output_dir="/private/customer/acme/output",
            engine=MALICIOUS_ENGINE,
            assigned_server_id=server.id,
            status="customer-job-status",
        )
        session.add(job)
        session.flush()
        manifest = Manifest(
            job_id=job.id,
            input_mode="existing_manifest",
            input_root="/private/customer/acme",
            manifest_path="/private/customer/acme/manifest.jsonl",
            status="ready",
        )
        session.add(manifest)
        session.flush()
        session.add(
            WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=1,
                shard_path="/private/customer/acme/shard.jsonl",
                status="pending",
            )
        )
        session.add_all(
            [
                JobEvent(
                    job_id=job.id,
                    event_type="page_done",
                    file_path=MALICIOUS_PATH,
                    page_no=1,
                    status="customer-page-status",
                    payload_json=json.dumps(
                        {
                            "error": MALICIOUS_ERROR,
                            "stages": [
                                {
                                    "stage": "customer-stage-secret",
                                    "status": "customer-stage-status",
                                    "failure_category": MALICIOUS_CATEGORY,
                                }
                            ],
                            "fallback": {
                                "used": True,
                                "source_stage": "customer-source-stage",
                                "reason": "customer-fallback-secret",
                            },
                        }
                    ),
                ),
                JobEvent(
                    job_id=job.id,
                    event_type="page_done",
                    page_no=2,
                    payload_json="{",
                ),
                JobEvent(
                    job_id=job.id,
                    event_type="file_failed",
                    failure_category=MALICIOUS_CATEGORY,
                    payload_json=json.dumps(
                        {
                            "file_path": MALICIOUS_PATH,
                            "error": MALICIOUS_ERROR,
                        }
                    ),
                ),
                JobEvent(
                    job_id=job.id,
                    event_type="file_failed",
                    failure_category="unknown",
                    payload_json="{}",
                ),
                JobFile(
                    job_id=job.id,
                    file_path=MALICIOUS_PATH,
                    filename="customer-document.pdf",
                    status="customer-artifact-status",
                    output_path="/private/customer/acme/output/result.md",
                    error=MALICIOUS_ERROR,
                    failure_category=MALICIOUS_CATEGORY,
                ),
            ]
        )
        session.commit()


def _label_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for block in re.findall(r"\{([^{}]+)\}", text):
        pairs.extend(re.findall(r'([a-z_]+)="([^"]*)"', block))
    return pairs


def _seed_job(
    session,
    *,
    job_id: str,
    server_id: str,
    status: str = "running",
    engine: str = "dotsocr",
) -> Job:
    job = Job(
        id=job_id,
        input_dir=f"/fixtures/{job_id}",
        output_dir=f"/outputs/{job_id}",
        engine=engine,
        assigned_server_id=server_id,
        status=status,
    )
    session.add(job)
    session.flush()
    return job


def _page_done_event(
    *,
    job_id: str,
    created_at: datetime,
    stage_status: str = "success",
) -> JobEvent:
    return JobEvent(
        job_id=job_id,
        event_type="page_done",
        created_at=created_at,
        payload_json=json.dumps(
            {
                "stages": [
                    {
                        "stage": "layout",
                        "status": stage_status,
                    }
                ],
                "fallback": {
                    "used": False,
                    "reason": None,
                    "source_stage": None,
                },
            }
        ),
    )


def test_metrics_requires_existing_api_token_semantics(tmp_path):
    client, _, _, _ = _client(tmp_path)

    missing = client.get("/api/system/metrics")
    wrong = client.get(
        "/api/system/metrics",
        headers={"Authorization": "Bearer wrong"},
    )
    allowed = client.get("/api/system/metrics", headers=AUTH)

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.json() == wrong.json() == {
        "detail": "Missing or invalid API token"
    }
    assert allowed.status_code == 200


def test_metrics_content_type_format_and_output_are_deterministic(tmp_path):
    client, app, session_factory, _ = _client(tmp_path)
    _seed_hostile_persisted_evidence(session_factory)

    first = client.get("/api/system/metrics", headers=AUTH)
    second = client.get("/api/system/metrics", headers=AUTH)

    assert first.status_code == 200
    assert first.headers["content-type"] == PROMETHEUS_CONTENT_TYPE
    success_content = app.openapi()["paths"]["/api/system/metrics"]["get"][
        "responses"
    ]["200"]["content"]
    assert set(success_content) == {"text/plain"}
    assert first.text == second.text
    parsed = list(text_string_to_metric_families(first.text))
    assert {family.name for family in parsed} == set(FAMILY_BY_NAME)
    for family_name in FAMILY_BY_NAME:
        assert f"# HELP {family_name} " in first.text
        assert f"# TYPE {family_name} gauge" in first.text


def test_trace_window_is_empty_and_not_truncated_without_events(tmp_path):
    _, _, session_factory, _ = _client(tmp_path)
    now = datetime(2026, 7, 28, 3, 0, tzinfo=timezone.utc)

    with session_factory() as session:
        rendered = encode_prometheus(metrics_snapshot(session, now=now))

    assert "\nocr_platform_trace_window_truncated 0\n" in rendered
    assert "\nocr_platform_stage_outcomes{" not in rendered
    assert "\nocr_platform_fallbacks{" not in rendered


def test_trace_window_includes_cutoff_and_excludes_older_events(tmp_path):
    _, _, session_factory, _ = _client(tmp_path)
    now = datetime(2026, 7, 28, 3, 0, tzinfo=timezone.utc)
    cutoff = now - control_metrics.TRACE_WINDOW
    with session_factory() as session:
        session.add(
            Server(
                id="trace-worker",
                name="Trace Worker",
                host="trace-worker",
            )
        )
        _seed_job(
            session,
            job_id="trace-boundary-job",
            server_id="trace-worker",
        )
        session.add_all(
            [
                _page_done_event(
                    job_id="trace-boundary-job",
                    created_at=cutoff - timedelta(microseconds=1),
                ),
                _page_done_event(
                    job_id="trace-boundary-job",
                    created_at=cutoff,
                ),
                _page_done_event(
                    job_id="trace-boundary-job",
                    created_at=now + timedelta(microseconds=1),
                ),
            ]
        )
        session.commit()

        rendered = encode_prometheus(metrics_snapshot(session, now=now))

    assert (
        'ocr_platform_stage_outcomes{engine="dotsocr",stage="layout",'
        'status="success",failure_category="none"} 1'
    ) in rendered
    assert "\nocr_platform_trace_window_truncated 0\n" in rendered


def test_trace_window_uses_newest_hard_limit_and_reports_truncation(
    tmp_path,
    monkeypatch,
):
    _, _, session_factory, _ = _client(tmp_path)
    now = datetime(2026, 7, 28, 3, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(control_metrics, "TRACE_EVENT_LIMIT", 2)
    with session_factory() as session:
        session.add(
            Server(
                id="limited-trace-worker",
                name="Limited Trace Worker",
                host="limited-trace-worker",
            )
        )
        _seed_job(
            session,
            job_id="limited-trace-job",
            server_id="limited-trace-worker",
        )
        session.add_all(
            [
                _page_done_event(
                    job_id="limited-trace-job",
                    created_at=now - timedelta(seconds=3),
                    stage_status="skipped",
                ),
                _page_done_event(
                    job_id="limited-trace-job",
                    created_at=now - timedelta(seconds=2),
                    stage_status="failed",
                ),
                _page_done_event(
                    job_id="limited-trace-job",
                    created_at=now - timedelta(seconds=1),
                    stage_status="success",
                ),
            ]
        )
        session.commit()

        rendered = encode_prometheus(metrics_snapshot(session, now=now))

    assert "\nocr_platform_trace_window_truncated 1\n" in rendered
    assert 'status="success",failure_category="none"} 1' in rendered
    assert 'status="failed",failure_category="none"} 1' in rendered
    assert 'status="skipped"' not in rendered


def test_negative_worker_slots_do_not_cancel_positive_slots(tmp_path):
    client, _, session_factory, _ = _client(tmp_path)
    with session_factory() as session:
        session.add_all(
            [
                Server(
                    id="positive-slots",
                    name="Positive Slots",
                    host="worker-a",
                    status="online",
                    capacity_slots=5,
                ),
                Server(
                    id="negative-slots",
                    name="Negative Slots",
                    host="worker-b",
                    status="online",
                    capacity_slots=-9,
                ),
            ]
        )
        session.commit()

    response = client.get("/api/system/metrics", headers=AUTH)

    assert response.status_code == 200
    assert "\nocr_platform_worker_slots 5\n" in response.text
    assert (
        '\nocr_platform_worker_slots_by_status{status="online"} 5\n'
        in response.text
    )


def test_shard_queue_excludes_terminal_parent_jobs(tmp_path):
    client, _, session_factory, _ = _client(tmp_path)
    with session_factory() as session:
        session.add(
            Server(
                id="queue-worker",
                name="Queue Worker",
                host="queue-worker",
            )
        )
        for job_position, job_status in enumerate(
            (
                "queued",
                "running",
                "stopping",
                "succeeded",
                "failed",
                "stopped",
                "unexpected-status",
            ),
            start=1,
        ):
            job = _seed_job(
                session,
                job_id=f"queue-{job_status}",
                server_id="queue-worker",
                status=job_status,
            )
            manifest = Manifest(
                job_id=job.id,
                input_mode="existing_manifest",
                manifest_path=f"/fixtures/{job.id}.jsonl",
                status="ready",
            )
            session.add(manifest)
            session.flush()
            for shard_position, shard_status in enumerate(
                ("pending", "retrying", "stale"),
                start=1,
            ):
                session.add(
                    WorkShard(
                        job_id=job.id,
                        manifest_id=manifest.id,
                        shard_index=job_position * 10 + shard_position,
                        shard_path=(
                            f"/fixtures/{job.id}-{shard_status}.jsonl"
                        ),
                        status=shard_status,
                    )
                )
        session.commit()

    response = client.get("/api/system/metrics", headers=AUTH)

    assert response.status_code == 200
    for shard_status in ("pending", "retrying", "stale"):
        assert (
            'ocr_platform_shard_queue{engine="dotsocr",'
            f'status="{shard_status}"}} 2'
        ) in response.text


def test_metrics_never_exposes_high_cardinality_or_hostile_values(tmp_path):
    client, _, session_factory, _ = _client(tmp_path)
    _seed_hostile_persisted_evidence(session_factory)

    response = client.get("/api/system/metrics", headers=AUTH)

    assert response.status_code == 200
    for forbidden in (
        MALICIOUS_JOB_ID,
        MALICIOUS_ENGINE,
        MALICIOUS_PATH,
        MALICIOUS_ERROR,
        MALICIOUS_CATEGORY,
        "Customer Secret Worker",
        "private.customer.internal",
        "customer-stage-secret",
        "customer-fallback-secret",
    ):
        assert forbidden not in response.text
    labels = _label_pairs(response.text)
    assert {key for key, _ in labels}.issubset(ALLOWED_LABEL_KEYS)
    allowed_values = {
        "engine": ENGINE_LABEL_VALUES,
        "stage": STAGE_LABEL_VALUES,
        "status": STATUS_LABEL_VALUES,
        "failure_category": FAILURE_CATEGORY_LABEL_VALUES,
        "fallback_category": FALLBACK_CATEGORY_LABEL_VALUES,
    }
    for key, value in labels:
        assert value in allowed_values[key]
    assert 'engine="other"' in response.text
    assert 'stage="other"' in response.text
    assert 'status="other"' in response.text
    assert 'failure_category="other"' in response.text
    assert 'failure_category="unknown"' in response.text
    assert 'fallback_category="other"' in response.text


def test_metrics_get_is_select_only_without_flush_or_commit(tmp_path):
    client, _, session_factory, engine = _client(tmp_path)
    _seed_hostile_persisted_evidence(session_factory)
    statements: list[str] = []

    def record_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        statements.append(str(statement).strip())

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        with (
            patch.object(
                Session,
                "commit",
                side_effect=AssertionError("metrics committed"),
            ),
            patch.object(
                Session,
                "flush",
                side_effect=AssertionError("metrics flushed"),
            ),
        ):
            response = client.get("/api/system/metrics", headers=AUTH)
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert response.status_code == 200, response.text
    assert statements
    assert all(statement.upper().startswith("SELECT") for statement in statements)
    trace_statement = next(
        statement
        for statement in statements
        if "job_events.payload_json" in statement
        and "job_events.event_type" in statement
    )
    normalized_trace_statement = " ".join(trace_statement.lower().split())
    assert "job_events.created_at >=" in normalized_trace_statement
    assert "job_events.created_at <=" in normalized_trace_statement
    assert "order by job_events.created_at desc" in normalized_trace_statement
    assert "job_events.id desc" in normalized_trace_statement
    assert " limit " in f" {normalized_trace_statement} "


def test_metrics_is_readiness_allowlisted_but_still_authenticated(tmp_path):
    client, app, _, _ = _client(tmp_path)
    app.state.database_readiness_probe.check = lambda **_: DatabaseReadiness(
        ready=False,
        reason="migrations_pending",
    )

    missing = client.get("/api/system/metrics")
    allowed = client.get("/api/system/metrics", headers=AUTH)

    assert missing.status_code == 401
    assert allowed.status_code == 200


def test_metrics_database_failure_is_fixed_and_redacted(
    tmp_path,
    monkeypatch,
):
    client, _, _, _ = _client(tmp_path)
    monkeypatch.setattr(
        "ocr_platform.control.domains.diagnostics.router.render_control_metrics",
        lambda _session: (_ for _ in ()).throw(
            RuntimeError("private database customer detail")
        ),
    )

    response = client.get("/api/system/metrics", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "control_database_status_unavailable"
    )
    assert "private database customer detail" not in response.text


def test_metrics_missing_table_is_fail_closed_under_schema_drift(tmp_path):
    client, app, _, engine = _client(tmp_path)
    app.state.database_readiness_probe.check = lambda **_: DatabaseReadiness(
        ready=False,
        reason="migrations_pending",
    )
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE job_events"))

    response = client.get("/api/system/metrics", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "control_database_status_unavailable"
    )
    assert "job_events" not in response.text
    assert "no such table" not in response.text
