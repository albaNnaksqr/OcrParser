from __future__ import annotations

import ast
import importlib
import re
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event as sa_event, inspect, select
from sqlalchemy.orm import sessionmaker

from ocr_platform.control.database import init_db
from ocr_platform.control.domains.manifests import commands, core
from ocr_platform.control.domains.manifests.commands import (
    COMPLETE_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR,
    FAIL_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR,
    ManifestCommandTransactionError,
    REGISTER_REMOTE_MANIFEST_ACTIVE_TRANSACTION_ERROR,
)
from ocr_platform.control.models import (
    Job,
    JobLog,
    Manifest,
    ScanUnit,
    Server,
    WorkShard,
)
from ocr_platform.control.schemas import (
    RemoteManifestRegisterRequest,
    RemoteManifestShardRequest,
    ScanUnitCompleteRequest,
    ScanUnitFailRequest,
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
    assert session_calls(core_path, "claim_next_pending_shard") == {
        "execute": 2,
        "commit": 1,
        "rollback": 1,
        "refresh": 1,
    }
    assert session_calls(scheduling_path, "_claim_work_shard") == {
        "execute": 1,
    }
    assert session_calls(core_path, "claim_next_scan_unit") == {
        "execute": 2,
        "commit": 1,
        "rollback": 2,
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
    assert session_calls(core_path, "update_work_shard") == {
        "execute": 2,
        "commit": 3,
        "refresh": 3,
    }
