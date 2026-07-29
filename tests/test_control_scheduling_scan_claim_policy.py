from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event as sa_event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from ocr_platform.control import scheduling
from ocr_platform.control.database import init_db
from ocr_platform.control.domains.common import (
    POOL_SERVER_ID,
    SHARD_LEASE_SECONDS,
)
from ocr_platform.control.domains.manifests import commands as manifest_commands
from ocr_platform.control.domains.manifests import use_cases as manifest_use_cases
from ocr_platform.control.models import Job, ScanUnit, Server, utcnow


ROOT = Path(__file__).resolve().parents[1]


def _session_factory():
    engine = create_engine("sqlite://", future=True)
    init_db(engine)
    return engine, sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )


def _seed_servers(session) -> None:
    shared_paths = json.dumps(
        {
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
        sort_keys=True,
    )
    session.add_all(
        [
            Server(
                id=POOL_SERVER_ID,
                name="Server Pool",
                host="pool",
                status="online",
                capacity_slots=0,
                capabilities_json='{"pool":true}',
                last_heartbeat_at=utcnow(),
            ),
            Server(
                id="server-a",
                name="server-a",
                host="host-a",
                status="online",
                capabilities_json=shared_paths,
                last_heartbeat_at=utcnow(),
            ),
            Server(
                id="server-b",
                name="server-b",
                host="host-b",
                status="online",
                capabilities_json=shared_paths,
                last_heartbeat_at=utcnow(),
            ),
        ]
    )


def _seed_scan_unit(
    session,
    *,
    suffix: str,
    status: str,
    allowed_server_ids: list[str] | None = None,
    attempt_count: int = 0,
) -> tuple[Job, ScanUnit]:
    job = Job(
        id=f"job-{suffix}",
        input_dir=f"/shared/{suffix}",
        output_dir=f"/shared/output-{suffix}",
        engine="dotsocr",
        input_mode="distributed_remote_folder_snapshot",
        assigned_server_id=POOL_SERVER_ID,
        allowed_server_ids_json=json.dumps(allowed_server_ids or []),
        status="queued",
    )
    unit = ScanUnit(
        job_id=job.id,
        path=job.input_dir,
        status=status,
        attempt_count=attempt_count,
        failure_category="old_failure",
        error_message="old error",
    )
    session.add_all([job, unit])
    session.flush()
    return job, unit


def test_scan_claim_selector_and_cas_own_exact_database_policy() -> None:
    statement = scheduling._claimable_scan_unit_id_select(
        limit=25,
        after_id=7,
        statuses={"stale"},
    )
    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "JOIN jobs ON scan_units.job_id = jobs.id" in compiled
    assert "scan_units.status IN ('stale')" in compiled
    assert f"jobs.assigned_server_id = '{POOL_SERVER_ID}'" in compiled
    assert "scan_units.id > 7" in compiled
    assert "ORDER BY scan_units.id ASC" in compiled
    assert "LIMIT 25" in compiled
    assert "FOR UPDATE SKIP LOCKED" in compiled

    engine, session_factory = _session_factory()
    fixed_now = datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc)
    try:
        with session_factory.begin() as session:
            _seed_servers(session)
            pending_job, pending = _seed_scan_unit(
                session,
                suffix="pending",
                status="pending",
                attempt_count=3,
            )
            stale_job, stale = _seed_scan_unit(
                session,
                suffix="stale",
                status="stale",
                attempt_count=4,
            )
            running_job, running = _seed_scan_unit(
                session,
                suffix="running",
                status="running",
                attempt_count=5,
            )
            ids = (pending.id, stale.id, running.id)

        with session_factory.begin() as session:
            scheduling._claim_scan_unit_candidate(
                session,
                ids[0],
                "server-a",
                claim_statuses={"pending"},
                now=fixed_now,
            )
            scheduling._claim_scan_unit_candidate(
                session,
                ids[1],
                "server-a",
                claim_statuses={"stale"},
                now=fixed_now,
            )

        with session_factory() as session:
            pending = session.get(ScanUnit, ids[0])
            stale = session.get(ScanUnit, ids[1])
            running = session.get(ScanUnit, ids[2])
            for unit, attempt_count in ((pending, 4), (stale, 5)):
                assert unit.status == "running"
                assert unit.assigned_server_id == "server-a"
                assert unit.attempt_count == attempt_count
                assert unit.started_at == fixed_now
                assert unit.lease_expires_at == (
                    fixed_now
                    + timedelta(seconds=SHARD_LEASE_SECONDS)
                )
                assert unit.failure_category is None
                assert unit.error_message is None
            assert (
                running.status,
                running.assigned_server_id,
                running.attempt_count,
                running.failure_category,
                running.error_message,
            ) == ("running", None, 5, "old_failure", "old error")
            assert [
                session.get(Job, job_id).status
                for job_id in (
                    pending_job.id,
                    stale_job.id,
                    running_job.id,
                )
            ] == ["queued", "queued", "queued"]

        with pytest.raises(
            scheduling._ScanUnitClaimCollision,
            match="compare-and-set race",
        ):
            with session_factory.begin() as session:
                scheduling._claim_scan_unit_candidate(
                    session,
                    ids[2],
                    "server-a",
                    claim_statuses={"pending", "stale"},
                    now=fixed_now,
                )
        with session_factory() as session:
            assert session.get(ScanUnit, ids[2]).attempt_count == 5
    finally:
        engine.dispose()


def test_scan_claim_application_keeps_stale_priority_and_ineligible_skip(
) -> None:
    engine, session_factory = _session_factory()
    try:
        with session_factory.begin() as session:
            _seed_servers(session)
            pending_job, pending = _seed_scan_unit(
                session,
                suffix="pending-eligible",
                status="pending",
            )
            blocked_job, blocked = _seed_scan_unit(
                session,
                suffix="stale-blocked",
                status="stale",
                allowed_server_ids=["server-b"],
                attempt_count=1,
            )
            stale_job, stale = _seed_scan_unit(
                session,
                suffix="stale-eligible",
                status="stale",
                attempt_count=2,
            )
            ids = (pending.id, blocked.id, stale.id)

        with session_factory() as session:
            first = manifest_commands.claim_next_scan_unit(
                session,
                "server-a",
            )
            first_id = first.id
            first_started_at = first.started_at
            first_lease = first.lease_expires_at
        assert first_id == ids[2]
        assert first_lease == (
            first_started_at
            + timedelta(seconds=SHARD_LEASE_SECONDS)
        )

        with session_factory() as session:
            blocked = session.get(ScanUnit, ids[1])
            pending = session.get(ScanUnit, ids[0])
            assert (
                blocked.status,
                blocked.assigned_server_id,
                blocked.attempt_count,
                blocked.failure_category,
                blocked.error_message,
            ) == ("stale", None, 1, "old_failure", "old error")
            assert pending.status == "pending"
            assert session.get(Job, blocked_job.id).status == "queued"
            assert session.get(Job, pending_job.id).status == "queued"
            claimed_job = session.get(Job, stale_job.id)
            assert claimed_job.status == "running"
            assert claimed_job.started_at == first_started_at

        with session_factory() as session:
            second = manifest_commands.claim_next_scan_unit(
                session,
                "server-a",
            )
            assert second.id == ids[0]
    finally:
        engine.dispose()


def test_scan_claim_collision_restarts_at_stale_phase() -> None:
    engine, session_factory = _session_factory()
    phase_calls: list[tuple[set[str], bool, object]] = []
    claim_calls = 0
    original_phase = manifest_use_cases.claim_next_scan_unit_phase
    original_claim = scheduling._claim_scan_unit_candidate

    def record_phase(*args, **kwargs):
        phase_calls.append(
            (
                set(kwargs["claim_statuses"]),
                bool(kwargs["reconcile"]),
                kwargs["now"],
            )
        )
        return original_phase(*args, **kwargs)

    def collide_once(*args, **kwargs):
        nonlocal claim_calls
        claim_calls += 1
        if claim_calls == 1:
            raise scheduling._ScanUnitClaimCollision("injected collision")
        return original_claim(*args, **kwargs)

    try:
        with session_factory.begin() as session:
            _seed_servers(session)
            _, pending = _seed_scan_unit(
                session,
                suffix="collision",
                status="pending",
            )
            pending_id = pending.id

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                    manifest_use_cases,
                    "claim_next_scan_unit_phase",
                record_phase,
            )
            monkeypatch.setattr(
                scheduling,
                "_claim_scan_unit_candidate",
                collide_once,
            )
            with session_factory() as session:
                claimed = manifest_commands.claim_next_scan_unit(
                    session,
                    "server-a",
                )

        assert claimed.id == pending_id
        assert claim_calls == 2
        assert [
            (statuses, reconcile)
            for statuses, reconcile, _ in phase_calls
        ] == [
            ({"stale"}, True),
            ({"pending"}, False),
            ({"stale"}, True),
            ({"pending"}, False),
        ]
        assert phase_calls[0][2] is None
        assert phase_calls[1][2] is not None
        assert phase_calls[2][2] is None
        assert phase_calls[3][2] is not None
        with session_factory() as session:
            unit = session.get(ScanUnit, pending_id)
            assert unit.status == "running"
            assert unit.attempt_count == 1
    finally:
        engine.dispose()


def test_scan_claim_job_and_unit_roll_back_together_on_commit_failure(
) -> None:
    engine, session_factory = _session_factory()
    try:
        with session_factory.begin() as session:
            _seed_servers(session)
            job, pending = _seed_scan_unit(
                session,
                suffix="atomic",
                status="pending",
            )
            job_id = job.id
            pending_id = pending.id

        with session_factory() as session:
            def fail_commit(current_session):
                raise RuntimeError("injected scan claim commit failure")

            sa_event.listen(
                session,
                "before_commit",
                fail_commit,
                once=True,
            )
            with pytest.raises(
                RuntimeError,
                match="injected scan claim commit failure",
            ):
                manifest_commands.claim_next_scan_unit(
                    session,
                    "server-a",
                )
            assert session.in_transaction() is False

        with session_factory() as session:
            job = session.get(Job, job_id)
            unit = session.get(ScanUnit, pending_id)
            assert job.status == "queued"
            assert job.started_at is None
            assert unit.status == "pending"
            assert unit.assigned_server_id is None
            assert unit.attempt_count == 0
            assert unit.started_at is None
            assert unit.lease_expires_at is None
            assert unit.failure_category == "old_failure"
            assert unit.error_message == "old error"
    finally:
        engine.dispose()


def test_scan_claim_policy_ownership_and_control_flow_are_static() -> None:
    scheduling_path = ROOT / "ocr_platform" / "control" / "scheduling.py"
    core_path = (
        ROOT
        / "ocr_platform"
        / "control"
        / "domains"
        / "manifests"
        / "core.py"
    )
    commands_path = core_path.with_name("commands.py")
    use_cases_path = core_path.with_name("use_cases.py")

    def function_source(path: Path, name: str) -> str:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        return ast.get_source_segment(source, function)

    selector = function_source(
        scheduling_path,
        "_claimable_scan_unit_id_select",
    )
    assert "select(ScanUnit)" in selector
    assert ".join(Job, ScanUnit.job_id == Job.id)" in selector
    assert "Job.assigned_server_id == POOL_SERVER_ID" in selector
    assert 'Job.status.in_({"queued", "running"})' in selector
    assert "ScanUnit.id > after_id" in selector
    assert ".order_by(ScanUnit.id.asc())" in selector
    assert ".limit(limit)" in selector
    assert ".with_for_update(skip_locked=True)" in selector

    cas = function_source(
        scheduling_path,
        "_claim_scan_unit_candidate",
    )
    assert "update(ScanUnit)" in cas
    assert "ScanUnit.status.in_(claim_statuses)" in cas
    assert 'status="running"' in cas
    assert "attempt_count=ScanUnit.attempt_count + 1" in cas
    assert "started_at=now" in cas
    assert "lease_expires_at=scan_unit_lease_deadline(now)" in cas
    assert "failure_category=None" in cas
    assert "error_message=None" in cas
    assert "result.rowcount != 1" in cas
    assert "_ScanUnitClaimCollision" in cas
    for source in (selector, cas):
        assert "session.commit(" not in source
        assert "session.rollback(" not in source
        assert "session.flush(" not in source
    assert "job.status" not in cas

    wrapper = function_source(
        core_path,
        "_claimable_scan_unit_id_select",
    )
    assert (
        "from ...scheduling import "
        "_claimable_scan_unit_id_select as target"
    ) in wrapper
    assert "select(ScanUnit)" not in wrapper

    phase = function_source(use_cases_path, "_claim_next_scan_unit_phase")
    assert "session.get(Server, server_id)" in phase
    assert "server.archived_at is not None" in phase
    assert "reconcile_expired_scan_unit_leases(session, now=now)" in phase
    assert "scheduling_policy._claimable_scan_unit_id_select(" in phase
    assert "after_id = max(candidate_ids)" in phase
    assert "server_is_allowed_for_job(job, server_id)" in phase
    assert "server_can_access_input_dir(" in phase
    assert "scheduling_policy._claim_scan_unit_candidate(" in phase
    assert 'job.status == "queued"' in phase
    assert 'job.status = "running"' in phase
    assert "job.started_at = now" in phase
    assert "update(ScanUnit)" not in phase
    assert 'status="running"' not in phase
    assert "session.commit(" not in phase
    assert "session.rollback(" not in phase

    command = function_source(commands_path, "claim_next_scan_unit")
    stale_phase = command.index('({"stale"}, True)')
    pending_phase = command.index('({"pending"}, False)')
    assert stale_phase < pending_phase
    assert "with session.begin():" in command
    assert "raise _ScanUnitClaimPhaseEnded(" in command
    assert "except _scheduling._ScanUnitClaimCollision:" in command
    assert command.index(
        "except _scheduling._ScanUnitClaimCollision:"
    ) < command.index("restart = True")
    assert command.index("restart = True") < command.index("break")

    core_source = core_path.read_text(encoding="utf-8")
    assert "class _ScanUnitClaimCollision" not in core_source
