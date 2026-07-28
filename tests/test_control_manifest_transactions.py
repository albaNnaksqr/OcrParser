from __future__ import annotations

import ast
import importlib
import re
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event as sa_event, inspect, select
from sqlalchemy.orm import sessionmaker

from ocr_platform.control.database import init_db
from ocr_platform.control import scheduling
from ocr_platform.control.domains.manifests import commands, core
from ocr_platform.control.domains.manifests.commands import (
    CLAIM_NEXT_PENDING_SHARD_ACTIVE_TRANSACTION_ERROR,
    CLAIM_NEXT_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR,
    COMPLETE_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR,
    FAIL_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR,
    ManifestCommandTransactionError,
    REGISTER_REMOTE_MANIFEST_ACTIVE_TRANSACTION_ERROR,
    UPDATE_WORK_SHARD_ACTIVE_TRANSACTION_ERROR,
)
from ocr_platform.control.domains.common import (
    POOL_SERVER_ID,
    ShardAttemptConflictError,
)
from ocr_platform.control.models import (
    Job,
    JobLog,
    Manifest,
    ScanUnit,
    Server,
    ShardAttempt,
    WorkShard,
    utcnow,
)
from ocr_platform.control.schemas import (
    RemoteManifestRegisterRequest,
    RemoteManifestShardRequest,
    ScanUnitCompleteRequest,
    ScanUnitFailRequest,
    WorkShardUpdateRequest,
)


ROOT = Path(__file__).resolve().parents[1]


def _database(tmp_path, *, expire_on_commit: bool = False):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'control.db'}",
        future=True,
    )
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=expire_on_commit,
    )
    init_db(engine)
    return session_factory, engine


def _seed_job(session_factory, *, job_id: str = "job-a") -> None:
    with session_factory() as session:
        with session.begin():
            session.add(
                Server(
                    id="server-a",
                    name="Server A",
                    host="localhost",
                )
            )
            session.add(
                Job(
                    id=job_id,
                    input_dir="/shared/input",
                    output_dir="/shared/output",
                    engine="dotsocr",
                    input_mode="remote_folder_snapshot",
                    assigned_server_id="server-a",
                    status="queued",
                )
            )


def _seed_running_scan_unit(
    session_factory,
    *,
    job_id: str = "job-a",
) -> int:
    _seed_job(session_factory, job_id=job_id)
    with session_factory() as session:
        with session.begin():
            session.add(
                Manifest(
                    job_id=job_id,
                    input_mode="distributed_remote_folder_snapshot",
                    input_root="/shared/input",
                    manifest_path=f"/shared/manifests/{job_id}/manifest.jsonl",
                    meta_path=f"/shared/manifests/{job_id}/manifest.meta.json",
                    status="scanning",
                )
            )
            unit = ScanUnit(
                job_id=job_id,
                path="/shared/input",
                status="running",
                assigned_server_id="server-a",
                attempt_count=2,
            )
            session.add(unit)
            session.flush()
            return unit.id


def _seed_pending_shard(
    session_factory,
    *,
    job_id: str = "job-a",
) -> int:
    _seed_job(session_factory, job_id=job_id)
    with session_factory() as session:
        with session.begin():
            manifest = Manifest(
                job_id=job_id,
                input_mode="remote_folder_snapshot",
                input_root="/shared/input",
                manifest_path=f"/shared/manifests/{job_id}/manifest.jsonl",
                status="ready",
            )
            session.add(manifest)
            session.flush()
            shard = WorkShard(
                job_id=job_id,
                manifest_id=manifest.id,
                shard_index=1,
                shard_path=f"/shared/manifests/{job_id}/shard-000001.jsonl",
                status="pending",
                file_count=1,
            )
            session.add(shard)
            session.flush()
            return shard.id


def _seed_scan_claim_case(
    session_factory,
    *,
    units: list[dict] | None = None,
    job_status: str = "queued",
) -> list[int]:
    unit_specs = (
        [{"path": "/shared/input", "status": "pending"}]
        if units is None
        else units
    )
    with session_factory() as session:
        with session.begin():
            session.add(
                Server(
                    id="server-a",
                    name="Server A",
                    host="localhost",
                    status="online",
                    last_heartbeat_at=utcnow(),
                    capabilities_json=(
                        '{"shared_paths":[{"path":"/shared/allowed",'
                        '"exists":true,"is_dir":true,"readable":true,'
                        '"writable":true},{"path":"/shared/input",'
                        '"exists":true,"is_dir":true,"readable":true,'
                        '"writable":true}]}'
                    ),
                )
            )
            session.add(
                Job(
                    id="job-a",
                    input_dir="/shared/input",
                    output_dir="/shared/output",
                    engine="dotsocr",
                    input_mode="distributed_remote_folder_snapshot",
                    assigned_server_id=POOL_SERVER_ID,
                    status=job_status,
                )
            )
            scan_units = []
            for spec in unit_specs:
                unit = ScanUnit(
                    job_id="job-a",
                    path=spec["path"],
                    status=spec["status"],
                    assigned_server_id=spec.get("assigned_server_id"),
                    attempt_count=spec.get("attempt_count", 0),
                    started_at=spec.get("started_at"),
                    lease_expires_at=spec.get("lease_expires_at"),
                )
                session.add(unit)
                scan_units.append(unit)
            session.flush()
            return [unit.id for unit in scan_units]


def _seed_shard_update_case(
    session_factory,
    *,
    shard_status: str = "running",
    job_status: str = "running",
    attempt_count: int = 1,
    max_attempts: int = 3,
) -> int:
    _seed_job(session_factory)
    with session_factory() as session:
        with session.begin():
            job = session.get(Job, "job-a")
            job.status = job_status
            job.max_shard_attempts = max_attempts
            manifest = Manifest(
                job_id=job.id,
                input_mode="remote_folder_snapshot",
                input_root="/shared/input",
                manifest_path="/shared/manifests/job-a/manifest.jsonl",
                status="ready",
            )
            session.add(manifest)
            session.flush()
            shard = WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=1,
                shard_path="/shared/manifests/job-a/shard-000001.jsonl",
                status=shard_status,
                assigned_server_id="server-a",
                attempt_count=attempt_count,
                file_count=1,
                started_at=utcnow(),
                lease_expires_at=(
                    None
                    if shard_status in {"succeeded", "failed", "stopped"}
                    else utcnow() + timedelta(minutes=1)
                ),
                finished_at=(
                    utcnow()
                    if shard_status in {"succeeded", "failed", "stopped"}
                    else None
                ),
            )
            session.add(shard)
            session.flush()
            session.add(
                ShardAttempt(
                    job_id=job.id,
                    shard_id=shard.id,
                    attempt_number=attempt_count,
                    server_id="server-a",
                    status=shard_status,
                    started_at=shard.started_at,
                    finished_at=shard.finished_at,
                )
            )
            return shard.id


def _request(
    *,
    manifest_path: str = "/shared/manifests/job-a/manifest.jsonl",
    shard_prefix: str = "/shared/manifests/job-a/shards",
    shard_count: int = 2,
) -> RemoteManifestRegisterRequest:
    return RemoteManifestRegisterRequest(
        input_mode="remote_folder_snapshot",
        input_root="/shared/input",
        manifest_path=manifest_path,
        meta_path=f"{manifest_path}.meta.json",
        file_count=shard_count,
        total_bytes=12,
        shards=[
            RemoteManifestShardRequest(
                shard_index=index,
                shard_path=f"{shard_prefix}/shard-{index:06d}.jsonl",
                file_count=1,
            )
            for index in range(1, shard_count + 1)
        ],
    )


def _scan_complete_request(
    *,
    child_paths: list[str] | None = None,
) -> ScanUnitCompleteRequest:
    return ScanUnitCompleteRequest(
        assigned_server_id="server-a",
        attempt_count=2,
        manifest_path="/shared/manifests/job-a/scan/manifest.jsonl",
        meta_path="/shared/manifests/job-a/scan/manifest.meta.json",
        file_count=2,
        total_bytes=12,
        child_paths=child_paths or [],
        shards=[
            RemoteManifestShardRequest(
                shard_index=99,
                shard_path="/shared/manifests/job-a/shards/shard-local.jsonl",
                file_count=2,
            )
        ],
    )


def _compatibility_service():
    module_name = ".".join(("ocr_platform", "control", "service"))
    return importlib.import_module(module_name)


def _transaction_observers(session):
    commits: list[int] = []
    rollbacks: list[int] = []
    sa_event.listen(
        session,
        "after_commit",
        lambda current: commits.append(1),
    )
    sa_event.listen(
        session,
        "after_rollback",
        lambda current: rollbacks.append(1),
    )
    return commits, rollbacks


def test_register_manifest_command_commits_exactly_once(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            manifest = commands.register_remote_manifest(
                session,
                "job-a",
                _request(),
            )

            assert manifest.id is not None
            assert commits == [1]
            assert rollbacks == []
            assert session.in_transaction() is False

        with session_factory() as session:
            persisted = session.scalar(
                select(Manifest).where(Manifest.job_id == "job-a")
            )
            shards = list(
                session.scalars(
                    select(WorkShard)
                    .where(WorkShard.job_id == "job-a")
                    .order_by(WorkShard.shard_index)
                )
            )
            assert persisted is not None
            assert persisted.id == manifest.id
            assert [shard.shard_index for shard in shards] == [1, 2]
            assert [shard.file_count for shard in shards] == [1, 1]
    finally:
        engine.dispose()


@pytest.mark.parametrize("expire_on_commit", [False, True])
def test_claim_shard_command_commits_once_and_returns_readable_result(
    tmp_path,
    expire_on_commit,
) -> None:
    session_factory, engine = _database(
        tmp_path,
        expire_on_commit=expire_on_commit,
    )
    shard_id = _seed_pending_shard(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            claimed = commands.claim_next_pending_shard(
                session,
                "job-a",
                "server-a",
            )

            assert claimed is not None
            assert claimed.id == shard_id
            assert claimed.status == "running"
            assert claimed.assigned_server_id == "server-a"
            assert claimed.attempt_count == 1
            assert claimed.lease_expires_at is not None
            assert session.expire_on_commit is expire_on_commit
            assert session.in_transaction() is False
            assert commits == [1]
            assert rollbacks == []
            detached_values = (
                claimed.id,
                claimed.status,
                claimed.assigned_server_id,
                claimed.attempt_count,
            )

        assert inspect(claimed).detached is True
        assert (
            claimed.id,
            claimed.status,
            claimed.assigned_server_id,
            claimed.attempt_count,
        ) == detached_values

        with session_factory() as session:
            attempts = list(
                session.scalars(
                    select(ShardAttempt).where(
                        ShardAttempt.shard_id == shard_id
                    )
                )
            )
            assert len(attempts) == 1
            assert attempts[0].attempt_number == 1
            assert attempts[0].status == "running"
    finally:
        engine.dispose()


@pytest.mark.parametrize("transaction_mode", ["explicit", "autobegin"])
def test_claim_shard_command_rejects_active_transaction_without_pollution(
    tmp_path,
    transaction_mode,
) -> None:
    session_factory, engine = _database(tmp_path)
    shard_id = _seed_pending_shard(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            if transaction_mode == "explicit":
                session.begin()
            else:
                assert session.get(WorkShard, shard_id) is not None
            outer_row = JobLog(
                job_id="job-a",
                server_id="outer",
                stream="stdout",
                line=f"claim-{transaction_mode}",
            )
            session.add(outer_row)

            with pytest.raises(
                ManifestCommandTransactionError,
                match=(
                    "^"
                    + re.escape(
                        CLAIM_NEXT_PENDING_SHARD_ACTIVE_TRANSACTION_ERROR
                    )
                    + "$"
                ),
            ):
                commands.claim_next_pending_shard(
                    session,
                    "job-a",
                    "server-a",
                )

            assert session.in_transaction() is True
            assert outer_row in session.new
            assert commits == []
            assert rollbacks == []
            session.rollback()

        with session_factory() as session:
            shard = session.get(WorkShard, shard_id)
            assert shard is not None
            assert shard.status == "pending"
            assert shard.attempt_count == 0
            assert session.scalar(
                select(JobLog).where(JobLog.server_id == "outer")
            ) is None
    finally:
        engine.dispose()


def test_claim_shard_cas_collision_rolls_back_before_retry(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    shard_id = _seed_pending_shard(session_factory)
    original_claim = scheduling._claim_work_shard
    claim_calls = 0

    class _LostRaceResult:
        rowcount = 0

    def collide_once(*args, **kwargs):
        nonlocal claim_calls
        claim_calls += 1
        result, claimable_parent = original_claim(*args, **kwargs)
        if claim_calls == 1:
            return _LostRaceResult(), claimable_parent
        return result, claimable_parent

    monkeypatch.setattr(
        scheduling,
        "_claim_work_shard",
        collide_once,
    )

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            claimed = commands.claim_next_pending_shard(
                session,
                "job-a",
                "server-a",
            )

            assert claimed is not None
            assert claimed.id == shard_id
            assert claimed.attempt_count == 1
            assert claim_calls == 2
            assert commits == [1]
            assert rollbacks == [1]
            assert session.in_transaction() is False

        with session_factory() as session:
            attempts = list(
                session.scalars(
                    select(ShardAttempt).where(
                        ShardAttempt.shard_id == shard_id
                    )
                )
            )
            assert len(attempts) == 1
            assert attempts[0].attempt_number == 1
    finally:
        engine.dispose()


def test_claim_shard_commit_failure_rolls_back_claim_and_attempt(
    tmp_path,
) -> None:
    session_factory, engine = _database(tmp_path)
    shard_id = _seed_pending_shard(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            def fail_commit(current_session):
                raise RuntimeError("injected claim commit failure")

            sa_event.listen(
                session,
                "before_commit",
                fail_commit,
                once=True,
            )
            with pytest.raises(
                RuntimeError,
                match="injected claim commit failure",
            ):
                commands.claim_next_pending_shard(
                    session,
                    "job-a",
                    "server-a",
                )

            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False

        with session_factory() as session:
            shard = session.get(WorkShard, shard_id)
            assert shard is not None
            assert shard.status == "pending"
            assert shard.assigned_server_id is None
            assert shard.attempt_count == 0
            assert session.scalar(
                select(ShardAttempt).where(
                    ShardAttempt.shard_id == shard_id
                )
            ) is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("expire_on_commit", [False, True])
@pytest.mark.parametrize(
    (
        "initial_status",
        "request_status",
        "expected_status",
        "expected_processed",
    ),
    [
        ("succeeded", "running", "succeeded", 0),
        ("retrying", "running", "retrying", 0),
        ("stale", "running", "stale", 0),
        ("running", "succeeded", "succeeded", 7),
    ],
)
def test_update_shard_success_paths_commit_once_and_return_readable(
    tmp_path,
    expire_on_commit,
    initial_status,
    request_status,
    expected_status,
    expected_processed,
) -> None:
    session_factory, engine = _database(
        tmp_path,
        expire_on_commit=expire_on_commit,
    )
    shard_id = _seed_shard_update_case(
        session_factory,
        shard_status=initial_status,
    )

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            updated = commands.update_work_shard(
                session,
                shard_id,
                WorkShardUpdateRequest(
                    status=request_status,
                    assigned_server_id="server-a",
                    attempt_count=1,
                    processed_files=7,
                ),
            )

            assert updated.id == shard_id
            assert updated.status == expected_status
            assert updated.processed_files == expected_processed
            assert session.expire_on_commit is expire_on_commit
            assert session.in_transaction() is False
            assert commits == [1]
            assert rollbacks == []
            detached_values = (
                updated.id,
                updated.status,
                updated.assigned_server_id,
                updated.attempt_count,
                updated.processed_files,
                updated.finished_at,
            )

        assert inspect(updated).detached is True
        assert (
            updated.id,
            updated.status,
            updated.assigned_server_id,
            updated.attempt_count,
            updated.processed_files,
            updated.finished_at,
        ) == detached_values

        with session_factory() as session:
            shard = session.get(WorkShard, shard_id)
            attempt = session.scalar(
                select(ShardAttempt).where(
                    ShardAttempt.shard_id == shard_id
                )
            )
            assert shard.status == expected_status
            assert shard.processed_files == expected_processed
            assert attempt.status == expected_status
            assert attempt.processed_files == expected_processed
    finally:
        engine.dispose()


@pytest.mark.parametrize("transaction_mode", ["explicit", "autobegin"])
def test_update_shard_rejects_active_transaction_without_pollution(
    tmp_path,
    transaction_mode,
) -> None:
    session_factory, engine = _database(tmp_path)
    shard_id = _seed_shard_update_case(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            if transaction_mode == "explicit":
                session.begin()
            else:
                assert session.get(WorkShard, shard_id) is not None
            outer_row = JobLog(
                job_id="job-a",
                server_id="outer",
                stream="stdout",
                line=f"shard-update-{transaction_mode}",
            )
            session.add(outer_row)

            with pytest.raises(
                ManifestCommandTransactionError,
                match=(
                    "^"
                    + re.escape(
                        UPDATE_WORK_SHARD_ACTIVE_TRANSACTION_ERROR
                    )
                    + "$"
                ),
            ):
                commands.update_work_shard(
                    session,
                    shard_id,
                    WorkShardUpdateRequest(status="succeeded"),
                )

            assert session.in_transaction() is True
            assert outer_row in session.new
            assert commits == []
            assert rollbacks == []
            session.rollback()

        with session_factory() as session:
            shard = session.get(WorkShard, shard_id)
            assert shard.status == "running"
            assert session.scalar(
                select(JobLog).where(JobLog.server_id == "outer")
            ) is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("failure_kind", ["unknown", "conflict"])
def test_update_shard_failure_rolls_back_once_and_leaves_session_clean(
    tmp_path,
    failure_kind,
) -> None:
    session_factory, engine = _database(tmp_path)
    shard_id = _seed_shard_update_case(session_factory)
    requested_id = shard_id if failure_kind == "conflict" else shard_id + 999
    request = WorkShardUpdateRequest(
        status="succeeded",
        assigned_server_id=(
            "wrong-server" if failure_kind == "conflict" else None
        ),
        attempt_count=1 if failure_kind == "conflict" else None,
    )
    expected_error = (
        ShardAttemptConflictError
        if failure_kind == "conflict"
        else ValueError
    )

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            with pytest.raises(expected_error):
                commands.update_work_shard(
                    session,
                    requested_id,
                    request,
                )

            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False
            with session.begin():
                assert session.get(WorkShard, shard_id) is not None
    finally:
        engine.dispose()


@pytest.mark.parametrize("conflict_kind", ["server", "attempt"])
def test_update_shard_fencing_precedes_terminal_replay(
    tmp_path,
    conflict_kind,
) -> None:
    session_factory, engine = _database(tmp_path)
    shard_id = _seed_shard_update_case(
        session_factory,
        shard_status="succeeded",
    )

    try:
        with session_factory() as session:
            expected_message = (
                "different server attempt"
                if conflict_kind == "server"
                else "stale attempt"
            )
            with pytest.raises(
                ShardAttemptConflictError,
                match=expected_message,
            ):
                commands.update_work_shard(
                    session,
                    shard_id,
                    WorkShardUpdateRequest(
                        status="running",
                        assigned_server_id=(
                            "wrong-server"
                            if conflict_kind == "server"
                            else "server-a"
                        ),
                        attempt_count=(
                            0 if conflict_kind == "attempt" else 1
                        ),
                        processed_files=999,
                    ),
                )
            assert session.in_transaction() is False

        with session_factory() as session:
            shard = session.get(WorkShard, shard_id)
            assert shard.status == "succeeded"
            assert shard.processed_files == 0
    finally:
        engine.dispose()


def test_update_terminal_replay_finalizes_job_idempotently(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    shard_id = _seed_shard_update_case(
        session_factory,
        shard_status="failed",
        job_status="running",
        max_attempts=1,
    )
    request = WorkShardUpdateRequest(
        status="running",
        assigned_server_id="server-a",
        attempt_count=1,
        processed_files=999,
    )

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            replayed = commands.update_work_shard(
                session,
                shard_id,
                request,
            )

            assert replayed.status == "failed"
            assert replayed.processed_files == 0
            assert commits == [1]
            assert rollbacks == []

        with session_factory() as session:
            job = session.get(Job, "job-a")
            assert job.status == "failed"
            assert job.failure_category == "shard_failed"
            first_finished_at = job.finished_at

        with session_factory() as session:
            replayed = commands.update_work_shard(
                session,
                shard_id,
                request,
            )
            assert replayed.status == "failed"

        with session_factory() as session:
            job = session.get(Job, "job-a")
            assert job.status == "failed"
            assert job.finished_at == first_finished_at
    finally:
        engine.dispose()


def test_update_shard_commit_failure_rolls_back_shard_attempt_and_job(
    tmp_path,
) -> None:
    session_factory, engine = _database(tmp_path)
    shard_id = _seed_shard_update_case(
        session_factory,
        max_attempts=1,
    )

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            def fail_commit(current_session):
                raise RuntimeError("injected shard update commit failure")

            sa_event.listen(
                session,
                "before_commit",
                fail_commit,
                once=True,
            )
            with pytest.raises(
                RuntimeError,
                match="injected shard update commit failure",
            ):
                commands.update_work_shard(
                    session,
                    shard_id,
                    WorkShardUpdateRequest(
                        status="failed",
                        assigned_server_id="server-a",
                        attempt_count=1,
                        processed_files=1,
                        failure_category="model_error",
                        error_message="permanent failure",
                    ),
                )

            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False

        with session_factory() as session:
            shard = session.get(WorkShard, shard_id)
            attempt = session.scalar(
                select(ShardAttempt).where(
                    ShardAttempt.shard_id == shard_id
                )
            )
            job = session.get(Job, "job-a")
            assert shard.status == "running"
            assert shard.processed_files == 0
            assert shard.failure_category is None
            assert shard.error_message is None
            assert attempt.status == "running"
            assert attempt.processed_files == 0
            assert attempt.failure_category is None
            assert job.status == "running"
            assert job.failure_category is None
            assert job.error_message is None
            assert job.finished_at is None
    finally:
        engine.dispose()


def test_update_shard_attempt_lookup_failure_rolls_back_shard_fields(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    shard_id = _seed_shard_update_case(session_factory)

    def fail_attempt_lookup(session, shard):
        raise RuntimeError("injected attempt lookup failure")

    monkeypatch.setattr(
        scheduling,
        "_latest_current_shard_attempt",
        fail_attempt_lookup,
    )

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            with pytest.raises(
                RuntimeError,
                match="injected attempt lookup failure",
            ):
                commands.update_work_shard(
                    session,
                    shard_id,
                    WorkShardUpdateRequest(
                        status="running",
                        assigned_server_id="server-a",
                        attempt_count=1,
                        processed_files=9,
                        completed_pages=11,
                        execution_paused=True,
                        failure_category="model_error",
                        error_message="must roll back",
                    ),
                )

            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False

        with session_factory() as session:
            shard = session.get(WorkShard, shard_id)
            attempt = session.scalar(
                select(ShardAttempt).where(
                    ShardAttempt.shard_id == shard_id
                )
            )
            job = session.get(Job, "job-a")
            assert shard.status == "running"
            assert shard.processed_files == 0
            assert shard.completed_pages == 0
            assert shard.execution_paused is False
            assert shard.failure_category is None
            assert shard.error_message is None
            assert attempt.status == "running"
            assert attempt.processed_files == 0
            assert job.status == "running"
    finally:
        engine.dispose()


def test_update_shard_finalization_failure_rolls_back_all_models(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    shard_id = _seed_shard_update_case(
        session_factory,
        max_attempts=1,
    )
    original_finalize = scheduling._finalize_job_after_shard_change

    def fail_after_finalization(*args, **kwargs):
        original_finalize(*args, **kwargs)
        raise RuntimeError("injected finalization failure")

    monkeypatch.setattr(
        scheduling,
        "_finalize_job_after_shard_change",
        fail_after_finalization,
    )

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            with pytest.raises(
                RuntimeError,
                match="injected finalization failure",
            ):
                commands.update_work_shard(
                    session,
                    shard_id,
                    WorkShardUpdateRequest(
                        status="failed",
                        assigned_server_id="server-a",
                        attempt_count=1,
                        processed_files=1,
                        failure_category="model_error",
                        error_message="must roll back",
                    ),
                )

            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False

        with session_factory() as session:
            shard = session.get(WorkShard, shard_id)
            attempt = session.scalar(
                select(ShardAttempt).where(
                    ShardAttempt.shard_id == shard_id
                )
            )
            job = session.get(Job, "job-a")
            assert shard.status == "running"
            assert shard.processed_files == 0
            assert shard.failure_category is None
            assert shard.error_message is None
            assert shard.finished_at is None
            assert attempt.status == "running"
            assert attempt.processed_files == 0
            assert attempt.failure_category is None
            assert attempt.error_message is None
            assert attempt.finished_at is None
            assert job.status == "running"
            assert job.failure_category is None
            assert job.error_message is None
            assert job.finished_at is None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("max_attempts", "expected_status", "expected_job_status"),
    [
        (2, "retrying", "running"),
        (1, "failed", "failed"),
    ],
)
def test_update_failed_shard_keeps_attempt_and_job_in_sync(
    tmp_path,
    max_attempts,
    expected_status,
    expected_job_status,
) -> None:
    session_factory, engine = _database(tmp_path)
    shard_id = _seed_shard_update_case(
        session_factory,
        max_attempts=max_attempts,
    )

    try:
        with session_factory() as session:
            updated = commands.update_work_shard(
                session,
                shard_id,
                WorkShardUpdateRequest(
                    status="failed",
                    assigned_server_id="server-a",
                    attempt_count=1,
                    processed_files=1,
                    failure_category="model_error",
                    error_message="OCR failed",
                ),
            )
            assert updated.status == expected_status

        with session_factory() as session:
            shard = session.get(WorkShard, shard_id)
            attempt = session.scalar(
                select(ShardAttempt).where(
                    ShardAttempt.shard_id == shard_id
                )
            )
            job = session.get(Job, "job-a")
            assert shard.status == expected_status
            assert attempt.status == expected_status
            assert attempt.failure_category == "model_error"
            assert attempt.error_message == "OCR failed"
            assert job.status == expected_job_status
            if expected_status == "failed":
                assert job.failure_category == "model_error"
                assert job.error_message == "OCR failed"
    finally:
        engine.dispose()


@pytest.mark.parametrize("expire_on_commit", [False, True])
@pytest.mark.parametrize(
    ("initial_status", "attempt_count", "expected_rollbacks"),
    [
        ("stale", 1, []),
        ("pending", 0, [1]),
    ],
)
def test_claim_scan_unit_commits_once_and_returns_readable_result(
    tmp_path,
    expire_on_commit,
    initial_status,
    attempt_count,
    expected_rollbacks,
) -> None:
    session_factory, engine = _database(
        tmp_path,
        expire_on_commit=expire_on_commit,
    )
    [unit_id] = _seed_scan_claim_case(
        session_factory,
        units=[
            {
                "path": "/shared/input",
                "status": initial_status,
                "attempt_count": attempt_count,
            }
        ],
    )

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            claimed = commands.claim_next_scan_unit(session, "server-a")

            assert claimed is not None
            assert claimed.id == unit_id
            assert claimed.status == "running"
            assert claimed.assigned_server_id == "server-a"
            assert claimed.attempt_count == attempt_count + 1
            assert claimed.started_at is not None
            assert claimed.lease_expires_at is not None
            assert session.expire_on_commit is expire_on_commit
            assert session.in_transaction() is False
            assert commits == [1]
            assert rollbacks == expected_rollbacks
            detached_values = (
                claimed.id,
                claimed.status,
                claimed.assigned_server_id,
                claimed.attempt_count,
                claimed.started_at,
                claimed.lease_expires_at,
            )

        assert inspect(claimed).detached is True
        assert (
            claimed.id,
            claimed.status,
            claimed.assigned_server_id,
            claimed.attempt_count,
            claimed.started_at,
            claimed.lease_expires_at,
        ) == detached_values

        with session_factory() as session:
            job = session.get(Job, "job-a")
            assert job is not None
            assert job.status == "running"
            assert job.started_at == claimed.started_at
    finally:
        engine.dispose()


def test_claim_scan_unit_exhausts_each_phase_without_commit(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_scan_claim_case(session_factory, units=[])

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            assert commands.claim_next_scan_unit(session, "server-a") is None

            assert commits == []
            assert rollbacks == [1, 1]
            assert session.in_transaction() is False

            with session.begin():
                assert session.get(Server, "server-a") is not None
    finally:
        engine.dispose()


def test_claim_scan_unit_invalid_server_rolls_back_without_commit(
    tmp_path,
) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_scan_claim_case(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            assert commands.claim_next_scan_unit(session, "missing") is None

            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False
    finally:
        engine.dispose()


@pytest.mark.parametrize("transaction_mode", ["explicit", "autobegin"])
def test_claim_scan_unit_rejects_active_transaction_without_pollution(
    tmp_path,
    transaction_mode,
) -> None:
    session_factory, engine = _database(tmp_path)
    [unit_id] = _seed_scan_claim_case(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            if transaction_mode == "explicit":
                session.begin()
            else:
                assert session.get(ScanUnit, unit_id) is not None
            outer_row = JobLog(
                job_id="job-a",
                server_id="outer",
                stream="stdout",
                line=f"scan-claim-{transaction_mode}",
            )
            session.add(outer_row)

            with pytest.raises(
                ManifestCommandTransactionError,
                match=(
                    "^"
                    + re.escape(
                        CLAIM_NEXT_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR
                    )
                    + "$"
                ),
            ):
                commands.claim_next_scan_unit(session, "server-a")

            assert session.in_transaction() is True
            assert outer_row in session.new
            assert commits == []
            assert rollbacks == []
            session.rollback()

        with session_factory() as session:
            unit = session.get(ScanUnit, unit_id)
            assert unit is not None
            assert unit.status == "pending"
            assert unit.attempt_count == 0
            assert session.scalar(
                select(JobLog).where(JobLog.server_id == "outer")
            ) is None
    finally:
        engine.dispose()


def test_claim_scan_unit_cas_collision_rolls_back_before_full_retry(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    [unit_id] = _seed_scan_claim_case(session_factory)
    first_now = utcnow()
    second_now = first_now + timedelta(seconds=1)
    now_values = iter((first_now, second_now))
    monkeypatch.setattr(core, "utcnow", lambda: next(now_values))

    class _LostRaceResult:
        rowcount = 0

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            original_execute = session.execute
            claim_updates = 0

            def collide_once(statement, *args, **kwargs):
                nonlocal claim_updates
                result = original_execute(statement, *args, **kwargs)
                table = getattr(statement, "table", None)
                if (
                    getattr(table, "name", None) == ScanUnit.__tablename__
                    and result.rowcount == 1
                ):
                    claim_updates += 1
                    if claim_updates == 1:
                        return _LostRaceResult()
                return result

            monkeypatch.setattr(session, "execute", collide_once)

            claimed = commands.claim_next_scan_unit(session, "server-a")

            assert claimed is not None
            assert claimed.id == unit_id
            assert claimed.attempt_count == 1
            assert claimed.started_at == second_now
            assert claim_updates == 2
            assert commits == [1]
            assert rollbacks == [1, 1, 1]
            assert session.in_transaction() is False

        with session_factory() as session:
            unit = session.get(ScanUnit, unit_id)
            assert unit is not None
            assert unit.status == "running"
            assert unit.attempt_count == 1
            assert unit.started_at == second_now
    finally:
        engine.dispose()


def test_claim_scan_unit_commit_failure_rolls_back_claim_and_job(
    tmp_path,
) -> None:
    session_factory, engine = _database(tmp_path)
    [unit_id] = _seed_scan_claim_case(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

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
                commands.claim_next_scan_unit(session, "server-a")

            assert commits == []
            assert rollbacks == [1, 1]
            assert session.in_transaction() is False

        with session_factory() as session:
            unit = session.get(ScanUnit, unit_id)
            job = session.get(Job, "job-a")
            assert unit is not None
            assert unit.status == "pending"
            assert unit.assigned_server_id is None
            assert unit.attempt_count == 0
            assert job is not None
            assert job.status == "queued"
            assert job.started_at is None
    finally:
        engine.dispose()


def test_claim_scan_unit_prefers_stale_before_pending(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    stale_id, pending_id = _seed_scan_claim_case(
        session_factory,
        units=[
            {
                "path": "/shared/input/stale",
                "status": "stale",
                "attempt_count": 1,
            },
            {
                "path": "/shared/input/pending",
                "status": "pending",
            },
        ],
    )

    try:
        with session_factory() as session:
            claimed = commands.claim_next_scan_unit(session, "server-a")

            assert claimed is not None
            assert claimed.id == stale_id
            assert claimed.attempt_count == 2

        with session_factory() as session:
            pending = session.get(ScanUnit, pending_id)
            assert pending is not None
            assert pending.status == "pending"
    finally:
        engine.dispose()


def test_claim_scan_unit_pending_phase_reuses_now_without_reconciliation(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    fixed_now = utcnow()
    expired_id, pending_id = _seed_scan_claim_case(
        session_factory,
        units=[
            {
                "path": "/shared/blocked",
                "status": "running",
                "assigned_server_id": "other-server",
                "attempt_count": 1,
                "started_at": fixed_now - timedelta(minutes=2),
                "lease_expires_at": fixed_now - timedelta(minutes=1),
            },
            {
                "path": "/shared/allowed/pending",
                "status": "pending",
            },
        ],
    )
    now_calls = 0

    def one_now():
        nonlocal now_calls
        now_calls += 1
        return fixed_now

    monkeypatch.setattr(core, "utcnow", one_now)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            claimed = commands.claim_next_scan_unit(session, "server-a")

            assert claimed is not None
            assert claimed.id == pending_id
            assert claimed.started_at == fixed_now
            assert now_calls == 1
            assert commits == [1]
            assert rollbacks == [1]

        with session_factory() as session:
            expired = session.get(ScanUnit, expired_id)
            assert expired is not None
            assert expired.status == "running"
            assert expired.assigned_server_id == "other-server"
            assert expired.lease_expires_at == fixed_now - timedelta(minutes=1)
    finally:
        engine.dispose()


def test_complete_scan_unit_command_commits_once_and_replay_is_idempotent(
    tmp_path,
) -> None:
    session_factory, engine = _database(
        tmp_path,
        expire_on_commit=True,
    )
    unit_id = _seed_running_scan_unit(session_factory)
    request = _scan_complete_request(
        child_paths=["/shared/input/child", "/shared/input/child"],
    )

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            completed = commands.complete_scan_unit(
                session,
                unit_id,
                request,
            )

            assert completed.status == "succeeded"
            assert completed.manifest_path == request.manifest_path
            assert session.expire_on_commit is True
            assert session.in_transaction() is False
            assert commits == [1]
            assert rollbacks == []

            replayed = commands.complete_scan_unit(
                session,
                unit_id,
                request,
            )

            assert replayed.status == "succeeded"
            assert session.expire_on_commit is True
            assert session.in_transaction() is False
            assert commits == [1, 1]
            assert rollbacks == []

        with session_factory() as session:
            manifest = session.scalar(
                select(Manifest).where(Manifest.job_id == "job-a")
            )
            units = list(
                session.scalars(
                    select(ScanUnit)
                    .where(ScanUnit.job_id == "job-a")
                    .order_by(ScanUnit.id)
                )
            )
            shards = list(
                session.scalars(
                    select(WorkShard)
                    .where(WorkShard.job_id == "job-a")
                    .order_by(WorkShard.shard_index)
                )
            )
            assert manifest is not None
            assert manifest.status == "scanning"
            assert manifest.file_count == 2
            assert manifest.total_bytes == 12
            assert manifest.next_shard_index == 2
            assert [unit.path for unit in units] == [
                "/shared/input",
                "/shared/input/child",
            ]
            assert [unit.status for unit in units] == [
                "succeeded",
                "pending",
            ]
            assert [shard.shard_index for shard in shards] == [1]
            assert [shard.file_count for shard in shards] == [2]
    finally:
        engine.dispose()


def test_complete_scan_unit_failure_rolls_back_all_manifest_side_effects(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    unit_id = _seed_running_scan_unit(session_factory)

    def fail_freeze(*args, **kwargs):
        raise RuntimeError("freeze failed")

    monkeypatch.setattr(core, "freeze_manifest_if_scan_complete", fail_freeze)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            with pytest.raises(RuntimeError, match="freeze failed"):
                commands.complete_scan_unit(
                    session,
                    unit_id,
                    _scan_complete_request(),
                )

            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False

        with session_factory() as session:
            unit = session.get(ScanUnit, unit_id)
            manifest = session.scalar(
                select(Manifest).where(Manifest.job_id == "job-a")
            )
            assert unit is not None
            assert unit.status == "running"
            assert unit.manifest_path is None
            assert unit.file_count == 0
            assert manifest is not None
            assert manifest.status == "scanning"
            assert manifest.file_count == 0
            assert manifest.total_bytes == 0
            assert manifest.next_shard_index == 1
            assert session.scalar(
                select(WorkShard).where(WorkShard.job_id == "job-a")
            ) is None
    finally:
        engine.dispose()


def test_fail_scan_unit_command_commits_once_and_replay_is_idempotent(
    tmp_path,
) -> None:
    session_factory, engine = _database(
        tmp_path,
        expire_on_commit=True,
    )
    unit_id = _seed_running_scan_unit(session_factory)
    request = ScanUnitFailRequest(
        assigned_server_id="server-a",
        attempt_count=2,
        error_message="permission denied",
    )

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            failed = commands.fail_scan_unit(session, unit_id, request)

            assert failed.status == "failed"
            assert failed.failure_category == "input_invalid"
            assert session.expire_on_commit is True
            assert session.in_transaction() is False
            assert commits == [1]
            assert rollbacks == []

            replayed = commands.fail_scan_unit(session, unit_id, request)

            assert replayed.status == "failed"
            assert session.expire_on_commit is True
            assert session.in_transaction() is False
            assert commits == [1, 1]
            assert rollbacks == []

        with session_factory() as session:
            unit = session.get(ScanUnit, unit_id)
            manifest = session.scalar(
                select(Manifest).where(Manifest.job_id == "job-a")
            )
            assert unit is not None
            assert unit.status == "failed"
            assert unit.error_message == "permission denied"
            assert manifest is not None
            assert manifest.status == "failed"
    finally:
        engine.dispose()


@pytest.mark.parametrize("transaction_mode", ["explicit", "autobegin"])
@pytest.mark.parametrize(
    ("command_name", "expected_error"),
    [
        (
            "complete_scan_unit",
            COMPLETE_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR,
        ),
        (
            "fail_scan_unit",
            FAIL_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR,
        ),
    ],
)
def test_scan_unit_commands_reject_active_transaction_without_outer_pollution(
    tmp_path,
    transaction_mode,
    command_name,
    expected_error,
) -> None:
    session_factory, engine = _database(tmp_path)
    unit_id = _seed_running_scan_unit(session_factory)
    request = (
        _scan_complete_request()
        if command_name == "complete_scan_unit"
        else ScanUnitFailRequest(error_message="failed")
    )

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            if transaction_mode == "explicit":
                session.begin()
            else:
                assert session.get(ScanUnit, unit_id) is not None
            outer_row = JobLog(
                job_id="job-a",
                server_id="outer",
                stream="stdout",
                line=f"{command_name}-{transaction_mode}",
            )
            session.add(outer_row)

            with pytest.raises(
                ManifestCommandTransactionError,
                match="^" + re.escape(expected_error) + "$",
            ):
                getattr(commands, command_name)(
                    session,
                    unit_id,
                    request,
                )

            assert session.in_transaction() is True
            assert outer_row in session.new
            assert commits == []
            assert rollbacks == []
            session.rollback()
            assert commits == []
            assert rollbacks == [1]

        with session_factory() as session:
            unit = session.get(ScanUnit, unit_id)
            assert unit is not None
            assert unit.status == "running"
            assert session.scalar(
                select(JobLog).where(JobLog.server_id == "outer")
            ) is None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("command_name", "payload"),
    [
        (
            "complete_scan_unit",
            ScanUnitCompleteRequest(
                assigned_server_id="server-b",
                attempt_count=1,
            ),
        ),
        (
            "fail_scan_unit",
            ScanUnitFailRequest(
                assigned_server_id="server-b",
                attempt_count=1,
                error_message="late failure",
            ),
        ),
    ],
)
def test_scan_unit_attempt_conflict_rolls_back_once(
    tmp_path,
    command_name,
    payload,
) -> None:
    session_factory, engine = _database(tmp_path)
    unit_id = _seed_running_scan_unit(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            with pytest.raises(commands.ScanUnitAttemptConflictError):
                getattr(commands, command_name)(
                    session,
                    unit_id,
                    payload,
                )

            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False

        with session_factory() as session:
            unit = session.get(ScanUnit, unit_id)
            manifest = session.scalar(
                select(Manifest).where(Manifest.job_id == "job-a")
            )
            assert unit is not None
            assert unit.status == "running"
            assert unit.assigned_server_id == "server-a"
            assert unit.attempt_count == 2
            assert manifest is not None
            assert manifest.status == "scanning"
    finally:
        engine.dispose()


@pytest.mark.parametrize("transaction_mode", ["explicit", "autobegin"])
def test_register_manifest_rejects_active_transaction_without_outer_pollution(
    tmp_path,
    transaction_mode,
) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            if transaction_mode == "explicit":
                session.begin()
            else:
                assert session.get(Job, "job-a") is not None
            outer_row = JobLog(
                job_id="job-a",
                server_id="outer",
                stream="stdout",
                line=f"outer-{transaction_mode}",
            )
            session.add(outer_row)

            with pytest.raises(
                ManifestCommandTransactionError,
                match=(
                    "^"
                    + re.escape(
                        REGISTER_REMOTE_MANIFEST_ACTIVE_TRANSACTION_ERROR
                    )
                    + "$"
                ),
            ):
                commands.register_remote_manifest(
                    session,
                    "job-a",
                    _request(),
                )

            assert session.in_transaction() is True
            assert outer_row in session.new
            assert commits == []
            assert rollbacks == []
            session.rollback()
            assert commits == []
            assert rollbacks == [1]

        with session_factory() as session:
            assert session.scalar(
                select(JobLog).where(JobLog.server_id == "outer")
            ) is None
            assert session.scalar(
                select(Manifest).where(Manifest.job_id == "job-a")
            ) is None
            assert session.scalar(
                select(WorkShard).where(WorkShard.job_id == "job-a")
            ) is None
    finally:
        engine.dispose()


def test_register_manifest_second_flush_failure_rolls_back_manifest_and_shards(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            original_flush = session.flush
            flush_count = 0

            def fail_second_flush(*args, **kwargs):
                nonlocal flush_count
                flush_count += 1
                if flush_count == 2:
                    raise RuntimeError("shard flush failed")
                return original_flush(*args, **kwargs)

            monkeypatch.setattr(session, "flush", fail_second_flush)

            with pytest.raises(RuntimeError, match="shard flush failed"):
                commands.register_remote_manifest(
                    session,
                    "job-a",
                    _request(),
                )

            assert flush_count == 2
            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False

        with session_factory() as session:
            assert session.scalar(
                select(Manifest).where(Manifest.job_id == "job-a")
            ) is None
            assert session.scalar(
                select(WorkShard).where(WorkShard.job_id == "job-a")
            ) is None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("shard_count", "expected_error"),
    [
        (2, "job already has registered shards: job-a"),
        (0, "job already has registered manifest: job-a"),
    ],
)
def test_duplicate_registration_keeps_original_rows_and_existing_error(
    tmp_path,
    shard_count: int,
    expected_error: str,
) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)

    try:
        with session_factory() as session:
            original = commands.register_remote_manifest(
                session,
                "job-a",
                _request(shard_count=shard_count),
            )

        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            with pytest.raises(
                ValueError,
                match=expected_error,
            ):
                commands.register_remote_manifest(
                    session,
                    "job-a",
                    _request(
                        manifest_path=(
                            "/shared/manifests/retry/manifest.jsonl"
                        ),
                        shard_prefix="/shared/manifests/retry/shards",
                        shard_count=shard_count,
                    ),
                )

            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False

        with session_factory() as session:
            manifests = list(
                session.scalars(
                    select(Manifest).where(Manifest.job_id == "job-a")
                )
            )
            shards = list(
                session.scalars(
                    select(WorkShard)
                    .where(WorkShard.job_id == "job-a")
                    .order_by(WorkShard.shard_index)
                )
            )
            assert [manifest.id for manifest in manifests] == [original.id]
            assert [shard.shard_index for shard in shards] == list(
                range(1, shard_count + 1)
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("expire_on_commit", [False, True])
def test_register_manifest_result_remains_readable_and_restores_expiry(
    tmp_path,
    expire_on_commit,
) -> None:
    session_factory, engine = _database(
        tmp_path,
        expire_on_commit=expire_on_commit,
    )
    _seed_job(session_factory)

    try:
        with session_factory() as session:
            assert session.expire_on_commit is expire_on_commit
            manifest = commands.register_remote_manifest(
                session,
                "job-a",
                _request(),
            )
            assert session.expire_on_commit is expire_on_commit
            assert session.in_transaction() is False
            values = (
                manifest.id,
                manifest.job_id,
                manifest.input_mode,
                manifest.input_root,
                manifest.manifest_path,
                manifest.meta_path,
                manifest.file_count,
                manifest.total_bytes,
                manifest.status,
            )

        assert inspect(manifest).detached is True
        assert (
            manifest.id,
            manifest.job_id,
            manifest.input_mode,
            manifest.input_root,
            manifest.manifest_path,
            manifest.meta_path,
            manifest.file_count,
            manifest.total_bytes,
            manifest.status,
        ) == values
    finally:
        engine.dispose()


def test_service_patch_restore_preserves_manifest_wrapper_and_core_leaf(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)
    service = _compatibility_service()
    command_name = "register_" + "remote_manifest"
    wrapper = getattr(commands, command_name)
    original_leaf = getattr(core, command_name)
    calls: list[bool] = []

    def fake_leaf(session, job_id, request):
        calls.append(session.in_transaction())
        return Manifest(
            id=999,
            job_id=job_id,
            input_mode=request.input_mode,
            input_root=request.input_root,
            manifest_path=request.manifest_path,
            meta_path=request.meta_path,
            file_count=request.file_count,
            total_bytes=request.total_bytes,
            status="ready",
        )

    assert getattr(service, command_name) is wrapper
    assert getattr(core, command_name) is original_leaf

    try:
        with monkeypatch.context() as patch:
            patch.setattr(service, command_name, fake_leaf)
            assert getattr(service, command_name) is fake_leaf
            assert getattr(core, command_name) is fake_leaf

            with session_factory() as session:
                fake_manifest = wrapper(
                    session,
                    "job-a",
                    _request(),
                )
                assert fake_manifest.id == 999
                assert session.in_transaction() is False

            with session_factory() as session:
                direct_fake = getattr(service, command_name)(
                    session,
                    "job-a",
                    _request(),
                )
                assert direct_fake.id == 999

        assert getattr(service, command_name) is wrapper
        assert getattr(core, command_name) is original_leaf
        assert calls == [True, False]

        with session_factory() as session:
            manifest = getattr(service, command_name)(
                session,
                "job-a",
                _request(),
            )
            assert manifest.id is not None
            assert session.in_transaction() is False

        with session_factory() as session:
            assert session.scalar(
                select(Manifest).where(Manifest.job_id == "job-a")
            ) is not None
            assert len(
                list(
                    session.scalars(
                        select(WorkShard).where(
                            WorkShard.job_id == "job-a"
                        )
                    )
                )
            ) == 2
    finally:
        setattr(core, command_name, original_leaf)
        engine.dispose()


def test_manifest_registration_session_call_scope_is_exact() -> None:
    core_path = (
        ROOT
        / "ocr_platform"
        / "control"
        / "domains"
        / "manifests"
        / "core.py"
    )
    commands_path = core_path.with_name("commands.py")
    ports_path = core_path.with_name("ports.py")
    scheduling_path = (
        ROOT / "ocr_platform" / "control" / "scheduling.py"
    )

    def session_calls(path: Path, function_name: str) -> Counter[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == function_name
        )
        return Counter(
            node.func.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "session"
            and node.func.attr
            in {
                "begin",
                "commit",
                "execute",
                "flush",
                "refresh",
                "rollback",
            }
        )

    assert session_calls(
        commands_path,
        "register_remote_manifest",
    ) == {"begin": 1}
    assert session_calls(
        core_path,
        "register_remote_manifest",
    ) == {
        "execute": 2,
        "flush": 1,
    }
    assert session_calls(
        core_path,
        "_create_static_shards_for_job",
    ) == {"flush": 2}
    assert session_calls(
        commands_path,
        "claim_next_pending_shard",
    ) == {
        "begin": 1,
        "execute": 1,
    }
    assert session_calls(core_path, "claim_next_pending_shard") == {}
    assert session_calls(core_path, "_claim_next_pending_shard") == {
        "execute": 1,
        "refresh": 1,
    }
    assert session_calls(scheduling_path, "_claim_work_shard") == {
        "execute": 1,
    }
    assert session_calls(
        commands_path,
        "claim_next_scan_unit",
    ) == {
        "begin": 1,
    }
    assert session_calls(core_path, "claim_next_scan_unit") == {}
    assert session_calls(core_path, "_claim_next_scan_unit_phase") == {
        "execute": 2,
        "refresh": 1,
    }
    assert session_calls(commands_path, "complete_scan_unit") == {
        "begin": 1,
    }
    assert session_calls(core_path, "complete_scan_unit") == {}
    assert session_calls(core_path, "_complete_scan_unit") == {
        "flush": 1,
    }
    assert session_calls(
        scheduling_path,
        "_lock_scan_unit_for_transition",
    ) == {"execute": 1}
    assert session_calls(
        ports_path,
        "lock_manifest_for_scan_unit_completion",
    ) == {"execute": 1}
    assert session_calls(
        ports_path,
        "materialize_scan_unit_completion",
    ) == {}
    assert session_calls(
        ports_path,
        "existing_scan_unit_paths",
    ) == {"execute": 1}
    assert session_calls(
        ports_path,
        "next_manifest_shard_index",
    ) == {"execute": 2}
    assert session_calls(
        ports_path,
        "freeze_manifest_if_scan_complete",
    ) == {"execute": 2}
    assert session_calls(commands_path, "fail_scan_unit") == {
        "begin": 1,
    }
    assert session_calls(core_path, "fail_scan_unit") == {}
    assert session_calls(core_path, "_fail_scan_unit") == {
        "flush": 1,
    }
    assert session_calls(
        ports_path,
        "fail_manifest_if_scan_complete",
    ) == {"execute": 2}
    assert session_calls(
        core_path,
        "claim_worker_manifest_integrity_check",
    ) == {
        "execute": 2,
        "commit": 1,
    }
    assert session_calls(
        core_path,
        "request_worker_manifest_integrity_check",
    ) == {
        "execute": 1,
        "commit": 1,
    }
    assert session_calls(
        core_path,
        "complete_worker_manifest_integrity_check",
    ) == {"commit": 1}
    assert session_calls(commands_path, "update_work_shard") == {
        "begin": 1,
    }
    assert session_calls(core_path, "update_work_shard") == {}
    assert session_calls(
        scheduling_path,
        "get_work_shard_update_snapshot",
    ) == {
        "execute": 1,
    }
    assert session_calls(
        scheduling_path,
        "_lock_job_for_shard_change",
    ) == {
        "execute": 1,
    }
    assert session_calls(
        scheduling_path,
        "lock_work_shard_for_update",
    ) == {
        "execute": 1,
    }
    assert session_calls(
        scheduling_path,
        "_latest_current_shard_attempt",
    ) == {
        "execute": 1,
    }
    core_source = core_path.read_text(encoding="utf-8")
    core_tree = ast.parse(core_source)
    assert not any(
        node
        for node in core_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_update_work_shard"
    )

    commands_source = commands_path.read_text(encoding="utf-8")
    commands_tree = ast.parse(commands_source)
    update_command = next(
        node
        for node in commands_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "update_work_shard"
    )
    update_command_source = ast.get_source_segment(
        commands_source,
        update_command,
    )
    assert update_command_source.index(
        "_scheduling.get_work_shard_update_snapshot("
    ) < update_command_source.index(
        "_scheduling._lock_job_for_shard_change("
    )
    assert update_command_source.index(
        "_scheduling._lock_job_for_shard_change("
    ) < update_command_source.index(
        "_scheduling.lock_work_shard_for_update("
    )
    assert update_command_source.index(
        "_scheduling.lock_work_shard_for_update("
    ) < update_command_source.index(
        "_scheduling.apply_work_shard_update("
    )
    assert "shard.status =" not in update_command_source
    assert "attempt.status =" not in update_command_source

    scheduling_source = scheduling_path.read_text(encoding="utf-8")
    scheduling_tree = ast.parse(scheduling_source)
    update_policy = next(
        node
        for node in scheduling_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "apply_work_shard_update"
    )
    update_policy_source = ast.get_source_segment(
        scheduling_source,
        update_policy,
    )
    terminal_guard = (
        "if shard.status in TERMINAL_SHARD_STATUSES:"
    )
    retrying_guard = 'shard.status in {"retrying", "stale"}'
    assert update_policy_source.index(
        "request.assigned_server_id"
    ) < update_policy_source.index(terminal_guard)
    assert update_policy_source.index(
        "request.attempt_count"
    ) < update_policy_source.index(terminal_guard)
    assert update_policy_source.index(terminal_guard) < (
        update_policy_source.index(retrying_guard)
    )
    assert "shard.status = _remaining_retry_status(job, shard)" in (
        update_policy_source
    )
    assert "attempt.status = shard.status" in update_policy_source
    assert "_finalize_job_after_shard_change(" in update_policy_source
