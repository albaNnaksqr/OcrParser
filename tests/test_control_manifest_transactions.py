from __future__ import annotations


import ast


import json


import re


from collections import Counter


from datetime import timedelta


from pathlib import Path


import pytest


from sqlalchemy import create_engine, event as sa_event, inspect, select


from sqlalchemy.orm import sessionmaker


from ocr_platform.control.database import init_db


from ocr_platform.control import scheduling


from ocr_platform.control.domains.manifests import (
    commands,
    integrity,
    use_cases,
)


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
    ManifestIntegrityResponse,
    ManifestIntegrityWorkerCompleteRequest,
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


def _seed_worker_integrity_manifest(session_factory) -> int:
    _seed_job(session_factory)
    with session_factory.begin() as session:
        server = session.get(Server, "server-a")
        server.status = "online"
        server.capabilities_json = json.dumps(
            {
                "shared_paths": [
                    {
                        "path": "/shared",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                    }
                ]
            }
        )
        manifest = Manifest(
            job_id="job-a",
            input_mode="remote_folder_snapshot",
            input_root="/shared/input",
            manifest_path="/shared/manifest.jsonl",
            file_count=0,
            total_bytes=0,
            status="ready",
        )
        session.add(manifest)
        session.flush()
        return manifest.id


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

    def collide_once(*args, **kwargs):
        nonlocal claim_calls
        claim_calls += 1
        if claim_calls == 1:
            claimable_parent = (
                select(Job.id)
                .where(Job.id == kwargs["job_id"])
                .where(Job.stop_requested.is_(False))
                .where(
                    Job.status.not_in(
                        {"stopping", "succeeded", "failed", "stopped"}
                    )
                )
                .exists()
            )
            raise scheduling._WorkShardClaimCollision(
                claimable_parent
            )
        return original_claim(*args, **kwargs)

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
