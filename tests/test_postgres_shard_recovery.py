from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import delete, select, text

from ocr_platform.control import scheduling as scheduling_core
from ocr_platform.control.database import create_session_factory
from ocr_platform.control.domains.common import POOL_SERVER_ID
from ocr_platform.control.domains.jobs.commands import request_stop
from ocr_platform.control.domains.manifests.commands import (
    claim_next_pending_shard,
    update_work_shard,
)
from ocr_platform.control.domains.workers.commands import (
    heartbeat_server,
    register_server,
)
from ocr_platform.control.domains.workers import core as workers_core
from ocr_platform.control.models import (
    Job,
    Manifest,
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


def _set_timeouts(session) -> int:
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    session.execute(text("SET LOCAL statement_timeout = '12s'"))
    return int(session.execute(text("SELECT pg_backend_pid()")).scalar_one())


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
    assigned_server_id: str | None = None,
    shard_status: str = "running",
) -> PostgresShardCase:
    session_factory, engine = create_session_factory(POSTGRES_URL)
    suffix = uuid.uuid4().hex
    server_ids = tuple(f"pg-recovery-{suffix}-{index}" for index in range(3))
    job_id = str(uuid.uuid4())
    with session_factory() as session:
        _set_timeouts(session)
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
        with case.session_factory() as session:
            _set_timeouts(session)
            session.execute(delete(Job).where(Job.id == case.job_id))
            session.execute(
                delete(Server).where(Server.id.in_(case.server_ids))
            )
            session.commit()
    finally:
        case.engine.dispose()


def _claim(case: PostgresShardCase, server_id: str):
    with case.session_factory() as session:
        _set_timeouts(session)
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
        with case.session_factory() as session:
            _set_timeouts(session)
            original_commit = session.commit

            def paused_commit():
                entered_commit.set()
                if not release_commit.wait(FUTURE_TIMEOUT_SECONDS):
                    raise TimeoutError("timed out waiting to release commit")
                original_commit()

            session.commit = paused_commit
            return operation(session)

    return entered_commit, release_commit, primary


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
        with case.session_factory() as session:
            _set_timeouts(session)
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
        with case.session_factory() as session:
            _set_timeouts(session)
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
        with case.session_factory() as session:
            _set_timeouts(session)
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
        with case.session_factory() as session:
            _set_timeouts(session)
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
        with case.session_factory() as session:
            _set_timeouts(session)
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
        with case.session_factory() as session:
            _set_timeouts(session)
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
            with case.session_factory() as session:
                _set_timeouts(session)
                workers_core.ensure_pool_server(session)
                session.commit()
            with case.session_factory() as session:
                _set_timeouts(session)
                assert workers_core.claim_next_pool_job(
                    session,
                    case.server_ids[1],
                ) is None
        else:
            assert _claim(case, case.server_ids[1]) is None
        with case.session_factory() as session:
            _set_timeouts(session)
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
        with case.session_factory() as session:
            second_pid.append(_set_timeouts(session))
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
        with case.session_factory() as session:
            _set_timeouts(session)
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
        with case.session_factory() as session:
            reregister_pid.append(_set_timeouts(session))
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
        with case.session_factory() as session:
            _set_timeouts(session)
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
        with case.session_factory() as session:
            _set_timeouts(session)
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
    with case.session_factory() as session:
        _set_timeouts(session)
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
        with case.session_factory() as session:
            _set_timeouts(session)
            return register_server(
                session,
                ServerRegisterRequest(
                    id=case.server_ids[0],
                    name=case.server_ids[0],
                    host="localhost",
                ),
            ).id

    def heartbeat():
        with case.session_factory() as session:
            heartbeat_pid.append(_set_timeouts(session))
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
        with case.session_factory() as session:
            exhaust_pid.append(_set_timeouts(session))
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
        with case.session_factory() as session:
            _set_timeouts(session)
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
        with case.session_factory() as session:
            update_pid.append(_set_timeouts(session))
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
        with case.session_factory() as session:
            _set_timeouts(session)
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
        with case.session_factory() as session:
            second_pid.append(_set_timeouts(session))
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
        with case.session_factory() as session:
            _set_timeouts(session)
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
        with case.session_factory() as session:
            _set_timeouts(session)
            success_shard = session.get(WorkShard, case.shard_ids[1])
            success_shard.lease_expires_at = utcnow() + timedelta(hours=1)
            session.commit()
        with case.session_factory() as session:
            _set_timeouts(session)
            workers_core.reconcile_expired_shard_leases(
                session,
                job_id=case.job_id,
            )
            session.commit()
        with case.session_factory() as session:
            _set_timeouts(session)
            first = session.get(WorkShard, case.shard_ids[0])
            second = session.get(WorkShard, case.shard_ids[1])
            assert first.status == "failed"
            assert second.status == "running"
            assert session.get(Job, case.job_id).status == "running"
        with case.session_factory() as session:
            _set_timeouts(session)
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
        with case.session_factory() as session:
            _set_timeouts(session)
            job = session.get(Job, case.job_id)
            assert job.status == "failed"
            assert job.failure_category == "lease_expired"
            assert job.error_message == LEASE_ERROR
    finally:
        _cleanup_case(case)
