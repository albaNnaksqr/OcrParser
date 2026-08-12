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


def test_worker_integrity_commands_each_commit_exactly_once(
    tmp_path,
) -> None:
    session_factory, engine = _database(tmp_path)
    manifest_id = _seed_worker_integrity_manifest(session_factory)
    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            requested = commands.request_worker_manifest_integrity_check(
                session,
                "job-a",
            )
            assert requested.worker_integrity_status == "pending"
            assert commits == [1]
            assert rollbacks == []
            assert session.in_transaction() is False

        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            task = commands.claim_worker_manifest_integrity_check(
                session,
                "server-a",
            )
            assert task is not None
            assert task.manifest_id == manifest_id
            assert commits == [1]
            assert rollbacks == []
            assert session.in_transaction() is False

        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            completed = (
                commands.complete_worker_manifest_integrity_check(
                    session,
                    manifest_id,
                    "server-a",
                    ManifestIntegrityWorkerCompleteRequest(
                        report=ManifestIntegrityResponse(
                            job_id="job-a",
                            manifest_id=manifest_id,
                            ok=True,
                            status="ok",
                        )
                    ),
                )
            )
            assert completed.worker_integrity_status == "ok"
            assert commits == [1]
            assert rollbacks == []
            assert session.in_transaction() is False
    finally:
        engine.dispose()


def test_worker_integrity_request_rolls_back_policy_failure(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    manifest_id = _seed_worker_integrity_manifest(session_factory)
    original = integrity.policy.request_worker_integrity

    def fail_after_policy(manifest, *, requested_at):
        original(manifest, requested_at=requested_at)
        raise RuntimeError("injected worker integrity failure")

    monkeypatch.setattr(
        integrity.policy,
        "request_worker_integrity",
        fail_after_policy,
    )
    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            with pytest.raises(
                RuntimeError,
                match="injected worker integrity failure",
            ):
                commands.request_worker_manifest_integrity_check(
                    session,
                    "job-a",
                )
            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False

        with session_factory() as session:
            manifest = session.get(Manifest, manifest_id)
            assert manifest.worker_integrity_status is None
            assert manifest.worker_integrity_requested_at is None
    finally:
        engine.dispose()


def test_manifest_registration_session_call_scope_is_exact() -> None:
    manifests_path = (
        ROOT
        / "ocr_platform"
        / "control"
        / "domains"
        / "manifests"
    )
    commands_path = manifests_path / "commands.py"
    construction_path = manifests_path / "construction.py"
    freeze_path = manifests_path / "freeze.py"
    integrity_path = manifests_path / "integrity.py"
    use_cases_path = manifests_path / "use_cases.py"
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
        construction_path,
        "register_remote_manifest",
    ) == {
        "execute": 2,
        "flush": 1,
    }
    assert session_calls(
        construction_path,
        "_create_static_shards_for_job",
    ) == {"flush": 2}
    assert session_calls(
        commands_path,
        "claim_next_pending_shard",
    ) == {
        "begin": 1,
        "execute": 1,
    }
    assert session_calls(use_cases_path, "_claim_next_pending_shard") == {
        "execute": 1,
    }
    assert session_calls(
        scheduling_path,
        "_lock_claim_parent_job",
    ) == {"execute": 1}
    assert session_calls(scheduling_path, "_claim_work_shard") == {
        "execute": 1,
        "refresh": 1,
    }
    assert session_calls(
        commands_path,
        "claim_next_scan_unit",
    ) == {
        "begin": 1,
    }
    assert session_calls(use_cases_path, "_claim_next_scan_unit_phase") == {
        "execute": 1,
        "refresh": 1,
    }
    assert session_calls(
        scheduling_path,
        "_claim_scan_unit_candidate",
    ) == {"execute": 1}
    assert session_calls(commands_path, "complete_scan_unit") == {
        "begin": 1,
    }
    assert session_calls(use_cases_path, "_complete_scan_unit") == {
        "flush": 1,
    }
    assert session_calls(
        scheduling_path,
        "_lock_scan_unit_for_transition",
    ) == {"execute": 1}
    assert session_calls(
        construction_path,
        "lock_manifest_for_scan_unit_completion",
    ) == {"execute": 1}
    assert session_calls(
        construction_path,
        "materialize_scan_unit_completion",
    ) == {}
    assert session_calls(
        construction_path,
        "existing_scan_unit_paths",
    ) == {"execute": 1}
    assert session_calls(
        construction_path,
        "next_manifest_shard_index",
    ) == {"execute": 2}
    assert session_calls(
        freeze_path,
        "freeze_manifest_if_scan_complete",
    ) == {"execute": 2}
    assert session_calls(commands_path, "fail_scan_unit") == {
        "begin": 1,
    }
    assert session_calls(use_cases_path, "_fail_scan_unit") == {
        "flush": 1,
    }
    assert session_calls(
        freeze_path,
        "fail_manifest_if_scan_complete",
    ) == {"execute": 2}
    assert session_calls(
        integrity_path,
        "claim_worker_manifest_integrity_check",
    ) == {
        "execute": 2,
        "flush": 1,
    }
    assert session_calls(
        integrity_path,
        "request_worker_manifest_integrity_check",
    ) == {
        "execute": 1,
        "flush": 1,
    }
    assert session_calls(
        integrity_path,
        "complete_worker_manifest_integrity_check",
    ) == {"flush": 1}
    assert session_calls(
        commands_path,
        "claim_worker_manifest_integrity_check",
    ) == {"begin": 1}
    assert session_calls(
        commands_path,
        "request_worker_manifest_integrity_check",
    ) == {"begin": 1}
    assert session_calls(
        commands_path,
        "complete_worker_manifest_integrity_check",
    ) == {"begin": 1}
    assert session_calls(commands_path, "update_work_shard") == {
        "begin": 1,
    }
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
