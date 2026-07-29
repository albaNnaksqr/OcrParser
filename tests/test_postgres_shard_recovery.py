from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import delete, event as sa_event, select, text

from ocr_platform.control import scheduling as scheduling_core
from ocr_platform.control.database import create_session_factory
from ocr_platform.control.domains.common import POOL_SERVER_ID
from ocr_platform.control.domains.jobs.commands import request_stop
from ocr_platform.control.domains.manifests.commands import (
    claim_next_pending_shard,
    claim_next_scan_unit,
    update_work_shard,
)
from ocr_platform.control.domains.workers.commands import (
    heartbeat_server,
    register_server,
)
from ocr_platform.control.domains.manifests import use_cases as manifest_use_cases
from ocr_platform.control.domains.workers import core as workers_core
from ocr_platform.control.models import (
    Job,
    Manifest,
    ScanUnit,
    Server,
    ShardAttempt,
    WorkShard,
    utcnow,
)
from ocr_platform.control.schemas import (
    ServerHeartbeatRequest,
    ServerRegisterRequest,
    WorkShardUpdateRequest,
)


POSTGRES_URL = os.environ.get("OCR_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="OCR_TEST_POSTGRES_URL is required for PostgreSQL shard recovery tests",
)
FUTURE_TIMEOUT_SECONDS = 15
LEASE_ERROR = "shard lease expired after maximum attempts"


@dataclass(frozen=True)
class PostgresShardCase:
    session_factory: object
    engine: object
    job_id: str
    server_ids: tuple[str, ...]
    shard_ids: tuple[int, ...]


@dataclass(frozen=True)
class PostgresScanUnitCase:
    session_factory: object
    engine: object
    job_id: str
    server_ids: tuple[str, ...]
    scan_unit_id: int


@dataclass(frozen=True)
class PostgresScanClaimCase:
    session_factory: object
    engine: object
    job_ids: tuple[str, ...]
    server_ids: tuple[str, ...]
    scan_unit_ids: tuple[int, ...]


@contextmanager
def _postgres_session(session_factory, engine):
    with engine.connect() as connection:
        backend_pid = int(
            connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
        )
        connection.commit()

        def apply_local_timeouts(current_connection):
            current_connection.exec_driver_sql(
                "SET LOCAL lock_timeout = '5s'"
            )
            current_connection.exec_driver_sql(
                "SET LOCAL statement_timeout = '12s'"
            )

        sa_event.listen(connection, "begin", apply_local_timeouts)
        try:
            with session_factory(bind=connection) as session:
                assert not session.in_transaction()
                yield session, backend_pid
        finally:
            sa_event.remove(connection, "begin", apply_local_timeouts)


def _wait_until_blocked(engine, backend_pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            blocked = connection.execute(
                text(
                    "SELECT cardinality(pg_blocking_pids(:backend_pid)) > 0"
                ),
                {"backend_pid": backend_pid},
            ).scalar_one()
        if blocked:
            return
        time.sleep(0.02)
    raise AssertionError(f"backend {backend_pid} did not enter a lock wait")


def _seed_case(
    *,
    max_attempts: int,
    shard_count: int = 1,
    server_count: int = 3,
    assigned_server_id: str | None = None,
    shard_status: str = "running",
) -> PostgresShardCase:
    session_factory, engine = create_session_factory(POSTGRES_URL)
    suffix = uuid.uuid4().hex
    server_ids = tuple(
        f"pg-recovery-{suffix}-{index}" for index in range(server_count)
    )
    job_id = str(uuid.uuid4())
    with _postgres_session(session_factory, engine) as (session, _):
        session.add_all(
            [
                Server(
                    id=server_id,
                    name=server_id,
                    host="localhost",
                    status="online",
                    capabilities_json=json.dumps(
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
                        }
                    ),
                    last_heartbeat_at=utcnow(),
                )
                for server_id in server_ids
            ]
        )
        if assigned_server_id == POOL_SERVER_ID:
            workers_core.ensure_pool_server(session)
        job = Job(
            id=job_id,
            input_dir="/shared/input",
            output_dir="/shared/output",
            engine="dotsocr",
            input_mode="remote_folder_snapshot",
            assigned_server_id=assigned_server_id or server_ids[0],
            status="running",
            max_shard_attempts=max_attempts,
            started_at=utcnow(),
        )
        session.add(job)
        manifest = Manifest(
            job_id=job_id,
            input_mode="remote_folder_snapshot",
            input_root="/shared/input",
            manifest_path=f"/shared/{suffix}/manifest.jsonl",
            file_count=shard_count,
            total_bytes=shard_count,
            status="ready",
        )
        session.add(manifest)
        session.flush()
        shards = []
        for shard_index in range(1, shard_count + 1):
            shard = WorkShard(
                job_id=job_id,
                manifest_id=manifest.id,
                shard_index=shard_index,
                shard_path=f"/shared/{suffix}/shard-{shard_index}.jsonl",
                status=shard_status,
                assigned_server_id=(
                    server_ids[min(shard_index - 1, len(server_ids) - 1)]
                    if shard_status == "running"
                    else None
                ),
                attempt_count=1 if shard_status == "running" else 0,
                file_count=1,
                started_at=utcnow() if shard_status == "running" else None,
                lease_expires_at=utcnow() if shard_status == "running" else None,
            )
            session.add(shard)
            session.flush()
            if shard_status == "running":
                session.add(
                    ShardAttempt(
                        job_id=job_id,
                        shard_id=shard.id,
                        attempt_number=1,
                        server_id=shard.assigned_server_id,
                        status="running",
                        started_at=shard.started_at,
                    )
                )
            shards.append(shard)
        session.commit()
        shard_ids = tuple(shard.id for shard in shards)
    return PostgresShardCase(
        session_factory=session_factory,
        engine=engine,
        job_id=job_id,
        server_ids=server_ids,
        shard_ids=shard_ids,
    )


def _cleanup_case(case: PostgresShardCase) -> None:
    try:
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            session.execute(delete(Job).where(Job.id == case.job_id))
            session.execute(
                delete(Server).where(Server.id.in_(case.server_ids))
            )
            session.commit()
    finally:
        case.engine.dispose()


def _seed_scan_unit_case() -> PostgresScanUnitCase:
    session_factory, engine = create_session_factory(POSTGRES_URL)
    suffix = uuid.uuid4().hex
    server_ids = tuple(f"pg-scan-{suffix}-{index}" for index in range(2))
    job_id = str(uuid.uuid4())
    with _postgres_session(session_factory, engine) as (session, _):
        session.add_all(
            [
                Server(
                    id=server_id,
                    name=server_id,
                    host="localhost",
                    status="online",
                    capabilities_json=json.dumps(
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
                        }
                    ),
                    last_heartbeat_at=utcnow(),
                )
                for server_id in server_ids
            ]
        )
        workers_core.ensure_pool_server(session)
        session.add(
            Job(
                id=job_id,
                input_dir="/shared/input",
                output_dir="/shared/output",
                engine="dotsocr",
                input_mode="distributed_remote_folder_snapshot",
                assigned_server_id=POOL_SERVER_ID,
                status="running",
                started_at=utcnow(),
            )
        )
        unit = ScanUnit(
            job_id=job_id,
            path="/shared/input",
            status="running",
            assigned_server_id=server_ids[0],
            attempt_count=1,
            started_at=utcnow(),
            lease_expires_at=utcnow() - timedelta(seconds=1),
        )
        session.add(unit)
        session.commit()
        scan_unit_id = unit.id
    return PostgresScanUnitCase(
        session_factory=session_factory,
        engine=engine,
        job_id=job_id,
        server_ids=server_ids,
        scan_unit_id=scan_unit_id,
    )


def _cleanup_scan_unit_case(case: PostgresScanUnitCase) -> None:
    try:
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            session.execute(delete(Job).where(Job.id == case.job_id))
            session.execute(
                delete(Server).where(Server.id.in_(case.server_ids))
            )
            session.commit()
    finally:
        case.engine.dispose()


def _seed_scan_claim_case(
    unit_specs: list[dict],
    *,
    first_server_path: str = "/shared",
) -> PostgresScanClaimCase:
    session_factory, engine = create_session_factory(POSTGRES_URL)
    suffix = uuid.uuid4().hex[:12]
    server_ids = tuple(
        f"psc-{suffix}-{index}" for index in range(2)
    )
    job_indexes = sorted(
        {int(spec.get("job_index", 0)) for spec in unit_specs} or {0}
    )
    job_ids = tuple(
        f"pscj-{suffix}-{index}" for index in job_indexes
    )
    assert all(
        len(identifier) <= 36
        for identifier in (*server_ids, *job_ids)
    )
    job_id_by_index = dict(zip(job_indexes, job_ids))
    with _postgres_session(session_factory, engine) as (session, _):
        for index, server_id in enumerate(server_ids):
            shared_path = first_server_path if index == 0 else "/shared"
            session.add(
                Server(
                    id=server_id,
                    name=server_id,
                    host="localhost",
                    status="online",
                    capabilities_json=json.dumps(
                        {
                            "shared_paths": [
                                {
                                    "path": shared_path,
                                    "exists": True,
                                    "is_dir": True,
                                    "readable": True,
                                    "writable": True,
                                }
                            ]
                        }
                    ),
                    last_heartbeat_at=utcnow(),
                )
            )
        workers_core.ensure_pool_server(session)
        for job_index in job_indexes:
            session.add(
                Job(
                    id=job_id_by_index[job_index],
                    input_dir="/shared/input",
                    output_dir="/shared/output",
                    engine="dotsocr",
                    input_mode="distributed_remote_folder_snapshot",
                    assigned_server_id=POOL_SERVER_ID,
                    status="queued",
                )
            )
        units = []
        for spec in unit_specs:
            assigned_server_id = spec.get("assigned_server_id")
            if "assigned_server_index" in spec:
                assigned_server_id = server_ids[
                    int(spec["assigned_server_index"])
                ]
            unit = ScanUnit(
                job_id=job_id_by_index[int(spec.get("job_index", 0))],
                path=spec["path"],
                status=spec["status"],
                assigned_server_id=assigned_server_id,
                attempt_count=int(spec.get("attempt_count", 0)),
                started_at=spec.get("started_at"),
                lease_expires_at=spec.get("lease_expires_at"),
            )
            session.add(unit)
            units.append(unit)
        session.commit()
        scan_unit_ids = tuple(unit.id for unit in units)
    return PostgresScanClaimCase(
        session_factory=session_factory,
        engine=engine,
        job_ids=job_ids,
        server_ids=server_ids,
        scan_unit_ids=scan_unit_ids,
    )


def _cleanup_scan_claim_case(case: PostgresScanClaimCase) -> None:
    try:
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            session.execute(delete(Job).where(Job.id.in_(case.job_ids)))
            session.execute(
                delete(Server).where(Server.id.in_(case.server_ids))
            )
            session.commit()
    finally:
        case.engine.dispose()


def _claim(case: PostgresShardCase, server_id: str):
    with _postgres_session(
        case.session_factory,
        case.engine,
    ) as (session, _):
        shard = claim_next_pending_shard(
            session,
            case.job_id,
            server_id,
        )
        if shard is None:
            return None
        return shard.id, shard.attempt_count, shard.assigned_server_id


def _run_with_paused_commit(case, operation):
    entered_commit = threading.Event()
    release_commit = threading.Event()

    def primary():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):

            def paused_commit(current_session):
                entered_commit.set()
                if not release_commit.wait(FUTURE_TIMEOUT_SECONDS):
                    raise TimeoutError("timed out waiting to release commit")

            sa_event.listen(
                session,
                "before_commit",
                paused_commit,
                once=True,
            )
            return operation(session)

    return entered_commit, release_commit, primary


def test_postgres_scan_unit_expiry_wins_against_busy_heartbeat():
    case = _seed_scan_unit_case()

    def reconcile_and_commit(session):
        scheduling_core.reconcile_expired_scan_unit_leases(
            session,
            job_id=case.job_id,
        )
        session.commit()
        return True

    entered, release, primary = _run_with_paused_commit(
        case,
        reconcile_and_commit,
    )
    heartbeat_started = threading.Event()

    def heartbeat():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            heartbeat_started.set()
            return heartbeat_server(
                session,
                case.server_ids[0],
                ServerHeartbeatRequest(
                    status="busy",
                    current_job_id=case.job_id,
                ),
            ).id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            expired = executor.submit(primary)
            assert entered.wait(FUTURE_TIMEOUT_SECONDS)
            heartbeated = executor.submit(heartbeat)
            assert heartbeat_started.wait(FUTURE_TIMEOUT_SECONDS)
            assert heartbeated.result(timeout=3) == case.server_ids[0]
            release.set()
            assert expired.result(timeout=FUTURE_TIMEOUT_SECONDS) is True
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            unit = session.get(ScanUnit, case.scan_unit_id)
            assert unit.status == "stale"
            assert unit.lease_expires_at is None
            assert unit.failure_category == "lease_expired"
    finally:
        release.set()
        _cleanup_scan_unit_case(case)


def test_postgres_scan_unit_claim_reclaims_after_concurrent_expiry():
    case = _seed_scan_unit_case()

    def reconcile_and_commit(session):
        scheduling_core.reconcile_expired_scan_unit_leases(
            session,
            job_id=case.job_id,
        )
        session.commit()
        return True

    entered, release, primary = _run_with_paused_commit(
        case,
        reconcile_and_commit,
    )
    claim_pid: list[int] = []
    claim_started = threading.Event()

    def claim():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, backend_pid):
            claim_pid.append(backend_pid)
            claim_started.set()
            unit = claim_next_scan_unit(session, case.server_ids[1])
            if unit is None:
                return None
            return unit.id, unit.attempt_count, unit.assigned_server_id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            expired = executor.submit(primary)
            assert entered.wait(FUTURE_TIMEOUT_SECONDS)
            claimed = executor.submit(claim)
            assert claim_started.wait(FUTURE_TIMEOUT_SECONDS)
            _wait_until_blocked(case.engine, claim_pid[0])
            release.set()
            assert expired.result(timeout=FUTURE_TIMEOUT_SECONDS) is True
            assert claimed.result(timeout=FUTURE_TIMEOUT_SECONDS) == (
                case.scan_unit_id,
                2,
                case.server_ids[1],
            )
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            unit = session.get(ScanUnit, case.scan_unit_id)
            assert unit.status == "running"
            assert unit.attempt_count == 2
            assert unit.assigned_server_id == case.server_ids[1]
            assert unit.lease_expires_at is not None
    finally:
        release.set()
        _cleanup_scan_unit_case(case)


def test_postgres_pending_scan_units_skip_locked_across_claim_batches():
    unit_count = manifest_use_cases.SCAN_UNIT_CLAIM_BATCH_SIZE + 1
    case = _seed_scan_claim_case(
        [
            {
                "job_index": index,
                "path": f"/shared/input/{index}",
                "status": "pending",
            }
            for index in range(unit_count)
        ]
    )
    entered, release, primary = _run_with_paused_commit(
        case,
        lambda session: claim_next_scan_unit(
            session,
            case.server_ids[0],
        ),
    )

    def second_claim():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            unit = claim_next_scan_unit(session, case.server_ids[1])
            return None if unit is None else unit.id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(primary)
            assert entered.wait(FUTURE_TIMEOUT_SECONDS)
            started = time.monotonic()
            second = executor.submit(second_claim)
            second_id = second.result(timeout=3)
            assert second_id == case.scan_unit_ids[-1]
            assert time.monotonic() - started < 3
            release.set()
            first_unit = first.result(timeout=FUTURE_TIMEOUT_SECONDS)
            assert first_unit.id in case.scan_unit_ids
            assert first_unit.id != second_id
    finally:
        release.set()
        _cleanup_scan_claim_case(case)


def test_postgres_stale_scan_pages_release_locks_at_phase_boundary(
    monkeypatch,
):
    case = _seed_scan_claim_case(
        [
            {
                "job_index": index,
                "path": f"/shared/blocked/{index}",
                "status": "stale",
                "attempt_count": 1,
            }
            for index in range(3)
        ],
        first_server_path="/shared/allowed",
    )
    monkeypatch.setattr(
        manifest_use_cases,
        "SCAN_UNIT_CLAIM_BATCH_SIZE",
        2,
    )
    stale_phase_scanned = threading.Event()
    release_stale_phase = threading.Event()
    waiter_started = threading.Event()
    waiter_pid: list[int] = []
    original_phase = manifest_use_cases.claim_next_scan_unit_phase

    def pause_after_stale_scan(*args, **kwargs):
        outcome = original_phase(*args, **kwargs)
        if kwargs["claim_statuses"] == {"stale"} and outcome[0] is None:
            stale_phase_scanned.set()
            if not release_stale_phase.wait(FUTURE_TIMEOUT_SECONDS):
                raise TimeoutError("timed out waiting to release stale phase")
        return outcome

    monkeypatch.setattr(
        manifest_use_cases,
        "claim_next_scan_unit_phase",
        pause_after_stale_scan,
    )

    def claim():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            return claim_next_scan_unit(session, case.server_ids[0])

    def wait_for_first_unit():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, backend_pid):
            waiter_pid.append(backend_pid)
            waiter_started.set()
            unit = session.execute(
                select(ScanUnit)
                .where(ScanUnit.id == case.scan_unit_ids[0])
                .with_for_update()
            ).scalar_one()
            return unit.id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = executor.submit(claim)
            assert stale_phase_scanned.wait(FUTURE_TIMEOUT_SECONDS)
            waiter = executor.submit(wait_for_first_unit)
            assert waiter_started.wait(FUTURE_TIMEOUT_SECONDS)
            _wait_until_blocked(case.engine, waiter_pid[0])
            release_stale_phase.set()
            assert claimed.result(timeout=FUTURE_TIMEOUT_SECONDS) is None
            assert waiter.result(timeout=FUTURE_TIMEOUT_SECONDS) == (
                case.scan_unit_ids[0]
            )
    finally:
        release_stale_phase.set()
        _cleanup_scan_claim_case(case)


@pytest.mark.parametrize("include_pending", [False, True])
def test_postgres_pending_phase_does_not_persist_global_reconciliation(
    include_pending,
):
    expired_at = utcnow() - timedelta(minutes=1)
    unit_specs = [
        {
            "path": "/shared/blocked/expired",
            "status": "running",
            "assigned_server_index": 1,
            "attempt_count": 1,
            "started_at": expired_at - timedelta(minutes=1),
            "lease_expires_at": expired_at,
        }
    ]
    if include_pending:
        unit_specs.append(
            {
                "path": "/shared/allowed/pending",
                "status": "pending",
            }
        )
    case = _seed_scan_claim_case(
        unit_specs,
        first_server_path="/shared/allowed",
    )

    try:
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            claimed = claim_next_scan_unit(session, case.server_ids[0])
            if include_pending:
                assert claimed is not None
                assert claimed.id == case.scan_unit_ids[1]
            else:
                assert claimed is None

        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            expired = session.get(ScanUnit, case.scan_unit_ids[0])
            assert expired.status == "running"
            assert expired.assigned_server_id == case.server_ids[1]
            assert expired.lease_expires_at == expired_at
    finally:
        _cleanup_scan_claim_case(case)


def test_postgres_stale_success_commits_global_reconciliation():
    expired_at = utcnow() - timedelta(minutes=1)
    case = _seed_scan_claim_case(
        [
            {
                "path": "/shared/allowed/claimable",
                "status": "running",
                "assigned_server_index": 1,
                "attempt_count": 1,
                "lease_expires_at": expired_at,
            },
            {
                "path": "/shared/blocked/reconciled",
                "status": "running",
                "assigned_server_index": 1,
                "attempt_count": 1,
                "lease_expires_at": expired_at,
            },
        ],
        first_server_path="/shared/allowed",
    )

    try:
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            claimed = claim_next_scan_unit(session, case.server_ids[0])
            assert claimed is not None
            assert claimed.id == case.scan_unit_ids[0]
            assert claimed.status == "running"
            assert claimed.attempt_count == 2

        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            reconciled = session.get(ScanUnit, case.scan_unit_ids[1])
            assert reconciled.status == "stale"
            assert reconciled.lease_expires_at is None
            assert reconciled.failure_category == "lease_expired"
    finally:
        _cleanup_scan_claim_case(case)


def test_postgres_scan_claim_commit_failure_is_atomic():
    case = _seed_scan_claim_case(
        [{"path": "/shared/input", "status": "pending"}]
    )

    try:
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
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
                claim_next_scan_unit(session, case.server_ids[0])
            assert session.in_transaction() is False

        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            unit = session.get(ScanUnit, case.scan_unit_ids[0])
            job = session.get(Job, case.job_ids[0])
            assert unit.status == "pending"
            assert unit.assigned_server_id is None
            assert unit.attempt_count == 0
            assert job.status == "queued"
            assert job.started_at is None
    finally:
        _cleanup_scan_claim_case(case)


def test_postgres_two_pending_shards_claim_with_skip_locked():
    case = _seed_case(
        max_attempts=3,
        shard_count=2,
        shard_status="pending",
    )
    entered, release, primary = _run_with_paused_commit(
        case,
        lambda session: claim_next_pending_shard(
            session,
            case.job_id,
            case.server_ids[0],
        ),
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(primary)
            assert entered.wait(FUTURE_TIMEOUT_SECONDS)
            started = time.monotonic()
            second = executor.submit(_claim, case, case.server_ids[1])
            second_claim = second.result(timeout=3)
            elapsed = time.monotonic() - started
            assert second_claim is not None
            assert second_claim[0] == case.shard_ids[1]
            assert elapsed < 3
            release.set()
            first_claim = first.result(timeout=FUTURE_TIMEOUT_SECONDS)
            assert first_claim.id == case.shard_ids[0]
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            assert session.execute(
                select(ShardAttempt).where(
                    ShardAttempt.job_id == case.job_id
                )
            ).scalars().all()
    finally:
        release.set()
        _cleanup_case(case)


def test_postgres_single_pending_shard_second_claimer_returns_fast_none():
    case = _seed_case(max_attempts=3, shard_status="pending")
    entered, release, primary = _run_with_paused_commit(
        case,
        lambda session: claim_next_pending_shard(
            session,
            case.job_id,
            case.server_ids[0],
        ),
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(primary)
            assert entered.wait(FUTURE_TIMEOUT_SECONDS)
            started = time.monotonic()
            second = executor.submit(_claim, case, case.server_ids[1])
            assert second.result(timeout=3) is None
            assert time.monotonic() - started < 3
            release.set()
            assert first.result(timeout=FUTURE_TIMEOUT_SECONDS).id == (
                case.shard_ids[0]
            )
    finally:
        release.set()
        _cleanup_case(case)


def test_postgres_claim_key_share_prevents_terminal_update_deadlock(monkeypatch):
    case = _seed_case(max_attempts=3, shard_status="pending")
    claim_holds_shard = threading.Event()
    release_claim = threading.Event()
    terminal_started = threading.Event()
    terminal_pid: list[int] = []
    original_create_attempt = (
        scheduling_core._add_running_shard_attempt_snapshot
    )

    def paused_create_attempt(session, shard, server_id):
        claim_holds_shard.set()
        if not release_claim.wait(FUTURE_TIMEOUT_SECONDS):
            raise TimeoutError("timed out waiting to create shard attempt")
        return original_create_attempt(session, shard, server_id)

    monkeypatch.setattr(
        "ocr_platform.control.scheduling."
        "_add_running_shard_attempt_snapshot",
        paused_create_attempt,
    )

    def claim():
        return _claim(case, case.server_ids[0])

    def terminal_update():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, backend_pid):
            terminal_pid.append(backend_pid)
            terminal_started.set()
            return update_work_shard(
                session,
                case.shard_ids[0],
                WorkShardUpdateRequest(
                    status="succeeded",
                    assigned_server_id=case.server_ids[0],
                    attempt_count=1,
                    processed_files=1,
                ),
            ).status

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = executor.submit(claim)
            assert claim_holds_shard.wait(FUTURE_TIMEOUT_SECONDS)
            terminal = executor.submit(terminal_update)
            assert terminal_started.wait(FUTURE_TIMEOUT_SECONDS)
            _wait_until_blocked(case.engine, terminal_pid[0])
            release_claim.set()
            claim_result = claimed.result(timeout=FUTURE_TIMEOUT_SECONDS)
            assert claim_result == (
                case.shard_ids[0],
                1,
                case.server_ids[0],
            )
            assert terminal.result(timeout=FUTURE_TIMEOUT_SECONDS) == "succeeded"

        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            shard = session.get(WorkShard, case.shard_ids[0])
            attempts = list(
                session.execute(
                    select(ShardAttempt)
                    .where(ShardAttempt.shard_id == case.shard_ids[0])
                    .order_by(ShardAttempt.attempt_number)
                ).scalars()
            )
            assert shard.status == "succeeded"
            assert shard.attempt_count == 1
            assert [
                (attempt.attempt_number, attempt.status)
                for attempt in attempts
            ] == [(1, "succeeded")]
    finally:
        release_claim.set()
        _cleanup_case(case)


def test_postgres_same_terminal_shard_replay_is_idempotent():
    case = _seed_case(max_attempts=3)
    request = WorkShardUpdateRequest(
        status="succeeded",
        assigned_server_id=case.server_ids[0],
        attempt_count=1,
        processed_files=1,
    )
    entered, release, primary = _run_with_paused_commit(
        case,
        lambda session: update_work_shard(
            session,
            case.shard_ids[0],
            request,
        ),
    )
    replay_started = threading.Event()
    replay_pid: list[int] = []

    def replay():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, backend_pid):
            replay_pid.append(backend_pid)
            replay_started.set()
            return update_work_shard(
                session,
                case.shard_ids[0],
                request,
            ).status

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(primary)
            assert entered.wait(FUTURE_TIMEOUT_SECONDS)
            second = executor.submit(replay)
            assert replay_started.wait(FUTURE_TIMEOUT_SECONDS)
            _wait_until_blocked(case.engine, replay_pid[0])
            release.set()
            assert first.result(
                timeout=FUTURE_TIMEOUT_SECONDS
            ).status == "succeeded"
            assert second.result(
                timeout=FUTURE_TIMEOUT_SECONDS
            ) == "succeeded"

        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            shard = session.get(WorkShard, case.shard_ids[0])
            attempts = list(
                session.scalars(
                    select(ShardAttempt).where(
                        ShardAttempt.shard_id == case.shard_ids[0]
                    )
                )
            )
            assert shard.status == "succeeded"
            assert shard.processed_files == 1
            assert len(attempts) == 1
            assert attempts[0].status == "succeeded"
            assert attempts[0].processed_files == 1
    finally:
        release.set()
        _cleanup_case(case)


def test_postgres_32_shards_8_workers_claim_and_succeed_without_deadlock():
    shard_count = 32
    worker_count = 8
    case = _seed_case(
        max_attempts=3,
        shard_count=shard_count,
        server_count=worker_count,
        shard_status="pending",
    )
    deadline = time.monotonic() + 30

    def worker(server_id: str) -> list[int]:
        completed: list[int] = []
        while time.monotonic() < deadline:
            claimed = _claim(case, server_id)
            if claimed is None:
                with _postgres_session(
                    case.session_factory,
                    case.engine,
                ) as (session, _):
                    remaining = session.execute(
                        select(WorkShard.id)
                        .where(WorkShard.job_id == case.job_id)
                        .where(
                            WorkShard.status.in_(
                                {"pending", "retrying", "stale", "running"}
                            )
                        )
                        .limit(1)
                    ).scalar_one_or_none()
                if remaining is None:
                    return completed
                time.sleep(0.01)
                continue
            shard_id, attempt_count, _ = claimed
            with _postgres_session(
                case.session_factory,
                case.engine,
            ) as (session, _):
                result = update_work_shard(
                    session,
                    shard_id,
                    WorkShardUpdateRequest(
                        status="succeeded",
                        assigned_server_id=server_id,
                        attempt_count=attempt_count,
                        processed_files=1,
                    ),
                )
                assert result.status == "succeeded"
            completed.append(shard_id)
        raise TimeoutError("shard claim stress did not finish before deadline")

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(worker, server_id)
                for server_id in case.server_ids
            ]
            completed = [
                shard_id
                for future in futures
                for shard_id in future.result(timeout=FUTURE_TIMEOUT_SECONDS * 3)
            ]

        assert len(completed) == shard_count
        assert len(set(completed)) == shard_count
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            shards = list(
                session.execute(
                    select(WorkShard)
                    .where(WorkShard.job_id == case.job_id)
                    .order_by(WorkShard.shard_index)
                ).scalars()
            )
            attempts = list(
                session.execute(
                    select(ShardAttempt)
                    .where(ShardAttempt.job_id == case.job_id)
                ).scalars()
            )
            assert len(shards) == shard_count
            assert all(shard.status == "succeeded" for shard in shards)
            assert all(shard.attempt_count == 1 for shard in shards)
            assert len(attempts) == shard_count
            assert len(
                {(attempt.shard_id, attempt.attempt_number) for attempt in attempts}
            ) == shard_count
    finally:
        _cleanup_case(case)


def test_postgres_progress_updates_on_different_shards_do_not_share_job_lock():
    case = _seed_case(max_attempts=3, shard_count=2)
    entered, release, primary = _run_with_paused_commit(
        case,
        lambda session: update_work_shard(
            session,
            case.shard_ids[0],
            WorkShardUpdateRequest(
                status="running",
                assigned_server_id=case.server_ids[0],
                attempt_count=1,
                processed_files=1,
            ),
        ),
    )

    def update_second():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            return update_work_shard(
                session,
                case.shard_ids[1],
                WorkShardUpdateRequest(
                    status="running",
                    assigned_server_id=case.server_ids[1],
                    attempt_count=1,
                    processed_files=2,
                ),
            ).processed_files

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(primary)
            assert entered.wait(FUTURE_TIMEOUT_SECONDS)
            started = time.monotonic()
            second = executor.submit(update_second)
            assert second.result(timeout=3) == 2
            assert time.monotonic() - started < 3
            release.set()
            assert first.result(timeout=FUTURE_TIMEOUT_SECONDS).processed_files == 1
    finally:
        release.set()
        _cleanup_case(case)


def test_postgres_at_cap_dual_claimers_fail_once():
    case = _seed_case(max_attempts=1)
    entered, release, primary = _run_with_paused_commit(
        case,
        lambda session: claim_next_pending_shard(
            session,
            case.job_id,
            case.server_ids[1],
        ),
    )
    second_started = threading.Event()

    def second_claim():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            second_started.set()
            return claim_next_pending_shard(
                session,
                case.job_id,
                case.server_ids[2],
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(primary)
            assert entered.wait(FUTURE_TIMEOUT_SECONDS)
            second = executor.submit(second_claim)
            assert second_started.wait(FUTURE_TIMEOUT_SECONDS)
            release.set()
            assert first.result(timeout=FUTURE_TIMEOUT_SECONDS) is None
            assert second.result(timeout=FUTURE_TIMEOUT_SECONDS) is None
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            shard = session.get(WorkShard, case.shard_ids[0])
            attempts = session.execute(
                select(ShardAttempt).where(
                    ShardAttempt.shard_id == shard.id
                )
            ).scalars().all()
            assert shard.status == "failed"
            assert shard.attempt_count == 1
            assert shard.error_message == LEASE_ERROR
            assert len(attempts) == 1
            assert attempts[0].status == "failed"
            assert session.get(Job, case.job_id).status == "failed"
    finally:
        release.set()
        _cleanup_case(case)


def test_postgres_below_cap_dual_claimers_create_one_unique_attempt():
    case = _seed_case(max_attempts=2)
    entered, release, primary = _run_with_paused_commit(
        case,
        lambda session: claim_next_pending_shard(
            session,
            case.job_id,
            case.server_ids[1],
        ),
    )
    second_started = threading.Event()

    def second_claim():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            second_started.set()
            return claim_next_pending_shard(
                session,
                case.job_id,
                case.server_ids[2],
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(primary)
            assert entered.wait(FUTURE_TIMEOUT_SECONDS)
            second = executor.submit(second_claim)
            assert second_started.wait(FUTURE_TIMEOUT_SECONDS)
            release.set()
            claimed = first.result(timeout=FUTURE_TIMEOUT_SECONDS)
            assert claimed is not None
            assert claimed.attempt_count == 2
            assert second.result(timeout=FUTURE_TIMEOUT_SECONDS) is None
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            attempts = session.execute(
                select(ShardAttempt)
                .where(ShardAttempt.shard_id == case.shard_ids[0])
                .order_by(ShardAttempt.attempt_number)
            ).scalars().all()
            assert [item.attempt_number for item in attempts] == [1, 2]
            assert [item.status for item in attempts] == ["stale", "running"]
    finally:
        release.set()
        _cleanup_case(case)


@pytest.mark.parametrize("use_pool_path", [False, True])
def test_postgres_no_candidate_reconciliation_persists_after_session_close(
    use_pool_path,
):
    pool_server = POOL_SERVER_ID if use_pool_path else None
    case = _seed_case(
        max_attempts=1,
        assigned_server_id=pool_server,
    )
    try:
        if use_pool_path:
            with _postgres_session(
                case.session_factory,
                case.engine,
            ) as (session, _):
                workers_core.ensure_pool_server(session)
                session.commit()
            with _postgres_session(
                case.session_factory,
                case.engine,
            ) as (session, _):
                assert workers_core.claim_next_pool_job(
                    session,
                    case.server_ids[1],
                ) is None
        else:
            assert _claim(case, case.server_ids[1]) is None
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            assert session.get(WorkShard, case.shard_ids[0]).status == "failed"
            assert session.get(Job, case.job_id).status == "failed"
    finally:
        _cleanup_case(case)


def test_postgres_stop_serializes_before_lease_exhaustion():
    case = _seed_case(max_attempts=1)
    entered, release, primary = _run_with_paused_commit(
        case,
        lambda session: request_stop(session, case.job_id),
    )
    second_pid: list[int] = []
    second_started = threading.Event()

    def exhaust():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, backend_pid):
            second_pid.append(backend_pid)
            second_started.set()
            result = workers_core.reconcile_expired_shard_leases(
                session,
                job_id=case.job_id,
            )
            session.commit()
            return result

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            stopped = executor.submit(primary)
            assert entered.wait(FUTURE_TIMEOUT_SECONDS)
            exhausted = executor.submit(exhaust)
            assert second_started.wait(FUTURE_TIMEOUT_SECONDS)
            _wait_until_blocked(case.engine, second_pid[0])
            release.set()
            assert stopped.result(timeout=FUTURE_TIMEOUT_SECONDS).status == "stopping"
            assert exhausted.result(timeout=FUTURE_TIMEOUT_SECONDS) is None
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            assert session.get(WorkShard, case.shard_ids[0]).status == "stopped"
            assert session.get(Job, case.job_id).status == "stopped"
    finally:
        release.set()
        _cleanup_case(case)


def test_postgres_reregister_and_reconcile_complete_without_deadlock(
    monkeypatch,
):
    case = _seed_case(max_attempts=1)
    reconcile_at_attempt_lock = threading.Event()
    release_reconcile = threading.Event()
    original_latest_attempt = scheduling_core._latest_current_shard_attempt

    def pause_before_attempt_lock(session, shard, *, for_update=False):
        reconcile_at_attempt_lock.set()
        if not release_reconcile.wait(FUTURE_TIMEOUT_SECONDS):
            raise TimeoutError("timed out waiting to release reconcile")
        return original_latest_attempt(
            session,
            shard,
            for_update=for_update,
        )

    monkeypatch.setattr(
        scheduling_core,
        "_latest_current_shard_attempt",
        pause_before_attempt_lock,
    )
    reregister_pid: list[int] = []
    reregister_started = threading.Event()

    def reregister():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, backend_pid):
            reregister_pid.append(backend_pid)
            reregister_started.set()
            return register_server(
                session,
                ServerRegisterRequest(
                    id=case.server_ids[0],
                    name=case.server_ids[0],
                    host="localhost",
                ),
            ).id

    def reconcile():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            workers_core.reconcile_expired_shard_leases(
                session,
                job_id=case.job_id,
            )
            session.commit()
            return True

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            reconciled = executor.submit(reconcile)
            assert reconcile_at_attempt_lock.wait(FUTURE_TIMEOUT_SECONDS)
            reregistered = executor.submit(reregister)
            assert reregister_started.wait(FUTURE_TIMEOUT_SECONDS)
            _wait_until_blocked(case.engine, reregister_pid[0])
            release_reconcile.set()
            assert reconciled.result(timeout=FUTURE_TIMEOUT_SECONDS) is True
            assert reregistered.result(timeout=FUTURE_TIMEOUT_SECONDS)
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            assert session.get(WorkShard, case.shard_ids[0]).status in {
                "stale",
                "failed",
            }
    finally:
        release_reconcile.set()
        _cleanup_case(case)


def test_postgres_heartbeat_waits_for_server_before_reregistered_shards(
    monkeypatch,
):
    case = _seed_case(max_attempts=3)
    with _postgres_session(
        case.session_factory,
        case.engine,
    ) as (session, _):
        shard = session.get(WorkShard, case.shard_ids[0])
        shard.lease_expires_at = utcnow() + timedelta(hours=1)
        session.commit()
    reregister_holds_server = threading.Event()
    release_reregister = threading.Event()
    original_fence = workers_core._fence_running_work_for_restarted_server

    def paused_fence(session, server_id, *, now):
        reregister_holds_server.set()
        if not release_reregister.wait(FUTURE_TIMEOUT_SECONDS):
            raise TimeoutError("timed out waiting to release re-register")
        return original_fence(session, server_id, now=now)

    monkeypatch.setattr(
        workers_core,
        "_fence_running_work_for_restarted_server",
        paused_fence,
    )
    heartbeat_pid: list[int] = []
    heartbeat_started = threading.Event()

    def reregister():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            return register_server(
                session,
                ServerRegisterRequest(
                    id=case.server_ids[0],
                    name=case.server_ids[0],
                    host="localhost",
                ),
            ).id

    def heartbeat():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, backend_pid):
            heartbeat_pid.append(backend_pid)
            heartbeat_started.set()
            return heartbeat_server(
                session,
                case.server_ids[0],
                ServerHeartbeatRequest(
                    status="busy",
                    current_job_id=case.job_id,
                ),
            ).id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            reregistered = executor.submit(reregister)
            assert reregister_holds_server.wait(FUTURE_TIMEOUT_SECONDS)
            heartbeated = executor.submit(heartbeat)
            assert heartbeat_started.wait(FUTURE_TIMEOUT_SECONDS)
            _wait_until_blocked(case.engine, heartbeat_pid[0])
            release_reregister.set()
            assert reregistered.result(timeout=FUTURE_TIMEOUT_SECONDS)
            assert heartbeated.result(timeout=FUTURE_TIMEOUT_SECONDS)
    finally:
        release_reregister.set()
        _cleanup_case(case)


def test_postgres_late_success_first_prevents_lease_exhaustion():
    case = _seed_case(max_attempts=1)
    entered, release, primary = _run_with_paused_commit(
        case,
        lambda session: update_work_shard(
            session,
            case.shard_ids[0],
            WorkShardUpdateRequest(
                status="succeeded",
                assigned_server_id=case.server_ids[0],
                attempt_count=1,
                processed_files=1,
            ),
        ),
    )
    exhaust_pid: list[int] = []
    exhaust_started = threading.Event()

    def exhaust():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, backend_pid):
            exhaust_pid.append(backend_pid)
            exhaust_started.set()
            shard = claim_next_pending_shard(
                session,
                case.job_id,
                case.server_ids[1],
            )
            return None if shard is None else shard.status

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            succeeded = executor.submit(primary)
            assert entered.wait(FUTURE_TIMEOUT_SECONDS)
            exhausted = executor.submit(exhaust)
            assert exhaust_started.wait(FUTURE_TIMEOUT_SECONDS)
            _wait_until_blocked(case.engine, exhaust_pid[0])
            release.set()
            assert succeeded.result(timeout=FUTURE_TIMEOUT_SECONDS).status == "succeeded"
            assert exhausted.result(timeout=FUTURE_TIMEOUT_SECONDS) is None
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            shard = session.get(WorkShard, case.shard_ids[0])
            job = session.get(Job, case.job_id)
            assert shard.status == "succeeded"
            assert job.status == "running"
    finally:
        release.set()
        _cleanup_case(case)


def test_postgres_lease_exhaustion_first_fences_late_success():
    case = _seed_case(max_attempts=1)
    entered, release, primary = _run_with_paused_commit(
        case,
        lambda session: claim_next_pending_shard(
            session,
            case.job_id,
            case.server_ids[1],
        ),
    )
    update_pid: list[int] = []
    update_started = threading.Event()

    def late_success():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, backend_pid):
            update_pid.append(backend_pid)
            update_started.set()
            return update_work_shard(
                session,
                case.shard_ids[0],
                WorkShardUpdateRequest(
                    status="succeeded",
                    assigned_server_id=case.server_ids[0],
                    attempt_count=1,
                    processed_files=1,
                ),
            ).status

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            exhausted = executor.submit(primary)
            assert entered.wait(FUTURE_TIMEOUT_SECONDS)
            succeeded = executor.submit(late_success)
            assert update_started.wait(FUTURE_TIMEOUT_SECONDS)
            _wait_until_blocked(case.engine, update_pid[0])
            release.set()
            assert exhausted.result(timeout=FUTURE_TIMEOUT_SECONDS) is None
            assert succeeded.result(timeout=FUTURE_TIMEOUT_SECONDS) == "failed"
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            shard = session.get(WorkShard, case.shard_ids[0])
            job = session.get(Job, case.job_id)
            assert shard.status == "failed"
            assert job.status == "failed"
            assert job.failure_category == "lease_expired"
    finally:
        release.set()
        _cleanup_case(case)


def test_postgres_dual_terminal_shards_use_last_explicit_failure():
    case = _seed_case(max_attempts=1, shard_count=2)
    entered, release, primary = _run_with_paused_commit(
        case,
        lambda session: update_work_shard(
            session,
            case.shard_ids[0],
            WorkShardUpdateRequest(
                status="failed",
                assigned_server_id=case.server_ids[0],
                attempt_count=1,
                failure_category="model_error",
                error_message="first explicit failure",
            ),
        ),
    )
    second_pid: list[int] = []
    second_started = threading.Event()

    def second_failure():
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, backend_pid):
            second_pid.append(backend_pid)
            second_started.set()
            return update_work_shard(
                session,
                case.shard_ids[1],
                WorkShardUpdateRequest(
                    status="failed",
                    assigned_server_id=case.server_ids[1],
                    attempt_count=1,
                    failure_category="output_error",
                    error_message="last explicit failure",
                ),
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(primary)
            assert entered.wait(FUTURE_TIMEOUT_SECONDS)
            second = executor.submit(second_failure)
            assert second_started.wait(FUTURE_TIMEOUT_SECONDS)
            _wait_until_blocked(case.engine, second_pid[0])
            release.set()
            assert first.result(timeout=FUTURE_TIMEOUT_SECONDS).status == "failed"
            assert second.result(timeout=FUTURE_TIMEOUT_SECONDS).status == "failed"
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            job = session.get(Job, case.job_id)
            assert job.status == "failed"
            assert job.failure_category == "output_error"
            assert job.error_message == "last explicit failure"
    finally:
        release.set()
        _cleanup_case(case)


def test_postgres_exhaustion_then_success_uses_lease_failure():
    case = _seed_case(max_attempts=1, shard_count=2)
    try:
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            success_shard = session.get(WorkShard, case.shard_ids[1])
            success_shard.lease_expires_at = utcnow() + timedelta(hours=1)
            session.commit()
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            workers_core.reconcile_expired_shard_leases(
                session,
                job_id=case.job_id,
            )
            session.commit()
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            first = session.get(WorkShard, case.shard_ids[0])
            second = session.get(WorkShard, case.shard_ids[1])
            assert first.status == "failed"
            assert second.status == "running"
            assert session.get(Job, case.job_id).status == "running"
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            result = update_work_shard(
                session,
                case.shard_ids[1],
                WorkShardUpdateRequest(
                    status="succeeded",
                    assigned_server_id=case.server_ids[1],
                    attempt_count=1,
                    processed_files=1,
                ),
            )
            assert result.status == "succeeded"
        with _postgres_session(
            case.session_factory,
            case.engine,
        ) as (session, _):
            job = session.get(Job, case.job_id)
            assert job.status == "failed"
            assert job.failure_category == "lease_expired"
            assert job.error_message == LEASE_ERROR
    finally:
        _cleanup_case(case)
