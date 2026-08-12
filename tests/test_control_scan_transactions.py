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
    monkeypatch.setattr(use_cases, "utcnow", lambda: next(now_values))

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

    monkeypatch.setattr(use_cases, "utcnow", one_now)

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

    monkeypatch.setattr(
        use_cases,
        "freeze_manifest_if_scan_complete",
        fail_freeze,
    )

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
