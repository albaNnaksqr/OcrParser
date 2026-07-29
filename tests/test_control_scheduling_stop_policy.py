import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event as sa_event, select
from sqlalchemy.orm import sessionmaker

from ocr_platform.control import scheduling
from ocr_platform.control.database import init_db
from ocr_platform.control.domains.jobs import commands as jobs_commands
from ocr_platform.control.domains.jobs import core as jobs_core
from ocr_platform.control.domains.jobs import lifecycle as jobs_lifecycle
from ocr_platform.control.models import (
    Job,
    Manifest,
    ScanUnit,
    ShardAttempt,
    WorkShard,
)


ROOT = Path(__file__).resolve().parents[1]


def test_stop_reclaimable_policy_updates_only_owned_states_with_one_time(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite://", future=True)
    init_db(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    fixed_time = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    calls = []
    monkeypatch.setattr(
        scheduling,
        "utcnow",
        lambda: calls.append(fixed_time) or fixed_time,
    )
    shard_statuses = [
        "pending",
        "retrying",
        "stale",
        "running",
        "succeeded",
        "failed",
        "stopped",
    ]
    scan_statuses = [
        "pending",
        "stale",
        "running",
        "succeeded",
        "failed",
        "stopped",
    ]
    active_lease = fixed_time + timedelta(minutes=5)

    try:
        with session_factory.begin() as session:
            job = Job(
                id="job-a",
                input_dir="/shared/input",
                output_dir="/shared/output",
                engine="dotsocr",
                assigned_server_id="server-a",
                status="running",
            )
            manifest = Manifest(
                job_id=job.id,
                input_mode="remote_folder_snapshot",
                input_root="/shared/input",
                manifest_path="/shared/manifest.jsonl",
                status="ready",
            )
            session.add_all([job, manifest])
            session.flush()
            shards = [
                WorkShard(
                    job_id=job.id,
                    manifest_id=manifest.id,
                    shard_index=index,
                    shard_path=f"/shared/shard-{index}.jsonl",
                    status=status,
                    file_count=1,
                    failure_category=(
                        "lease_expired" if status == "stale" else None
                    ),
                    lease_expires_at=active_lease,
                )
                for index, status in enumerate(shard_statuses, start=1)
            ]
            session.add_all(shards)
            session.flush()
            session.add(
                ShardAttempt(
                    job_id=job.id,
                    shard_id=shards[1].id,
                    attempt_number=1,
                    server_id="server-a",
                    status="retrying",
                    processed_files=3,
                    failure_category="model_error",
                    error_message="preserve attempt",
                    started_at=fixed_time,
                    finished_at=fixed_time,
                )
            )
            session.add_all(
                [
                    ScanUnit(
                        job_id=job.id,
                        path=f"/shared/scan-{index}",
                        status=status,
                        failure_category=(
                            "lease_expired" if status == "stale" else None
                        ),
                        lease_expires_at=active_lease,
                    )
                    for index, status in enumerate(scan_statuses, start=1)
                ]
            )

        with session_factory.begin() as session:
            job = session.get(Job, "job-a")
            scheduling.stop_reclaimable_work_for_job(session, job)

        with session_factory() as session:
            shards = list(
                session.execute(
                    select(WorkShard).order_by(WorkShard.shard_index)
                ).scalars()
            )
            scan_units = list(
                session.execute(
                    select(ScanUnit).order_by(ScanUnit.id)
                ).scalars()
            )
            attempt = session.execute(select(ShardAttempt)).scalar_one()

        assert calls == [fixed_time]
        assert [shard.status for shard in shards] == [
            "stopped",
            "stopped",
            "stopped",
            "running",
            "succeeded",
            "failed",
            "stopped",
        ]
        assert [unit.status for unit in scan_units] == [
            "stopped",
            "stopped",
            "running",
            "succeeded",
            "failed",
            "stopped",
        ]
        stopped_shards = shards[:3]
        stopped_scan_units = scan_units[:2]
        assert {
            item.failure_category
            for item in [*stopped_shards, *stopped_scan_units]
        } == {"operator_stopped"}
        assert all(
            item.lease_expires_at is None
            for item in [*stopped_shards, *stopped_scan_units]
        )
        assert len(
            {
                item.finished_at
                for item in [*stopped_shards, *stopped_scan_units]
            }
        ) == 1
        assert shards[3].lease_expires_at == active_lease
        assert scan_units[2].lease_expires_at == active_lease
        assert (
            attempt.status,
            attempt.processed_files,
            attempt.failure_category,
            attempt.error_message,
            attempt.started_at,
            attempt.finished_at,
        ) == (
            "retrying",
            3,
            "model_error",
            "preserve attempt",
            fixed_time,
            fixed_time,
        )
    finally:
        engine.dispose()


def test_stop_reclaimable_policy_is_atomic_under_outer_transaction() -> None:
    engine = create_engine("sqlite://", future=True)
    init_db(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )

    try:
        with session_factory.begin() as session:
            job = Job(
                id="job-a",
                input_dir="/shared/input",
                output_dir="/shared/output",
                engine="dotsocr",
                assigned_server_id="server-a",
                status="running",
            )
            manifest = Manifest(
                job_id=job.id,
                input_mode="remote_folder_snapshot",
                input_root="/shared/input",
                manifest_path="/shared/manifest.jsonl",
                status="ready",
            )
            session.add_all([job, manifest])
            session.flush()
            session.add(
                WorkShard(
                    job_id=job.id,
                    manifest_id=manifest.id,
                    shard_index=1,
                    shard_path="/shared/shard-1.jsonl",
                    status="pending",
                    file_count=1,
                )
            )
            session.add(
                ScanUnit(
                    job_id=job.id,
                    path="/shared/scan-1",
                    status="pending",
                )
            )

        def fail_second_bulk(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ):
            if statement.lstrip().upper().startswith("UPDATE SCAN_UNITS"):
                raise RuntimeError("injected scan stop failure")

        sa_event.listen(engine, "before_cursor_execute", fail_second_bulk)
        try:
            with pytest.raises(
                RuntimeError,
                match="injected scan stop failure",
            ):
                with session_factory() as session:
                    jobs_commands.request_stop(session, "job-a")
        finally:
            sa_event.remove(
                engine,
                "before_cursor_execute",
                fail_second_bulk,
            )

        with session_factory() as session:
            job = session.get(Job, "job-a")
            shard = session.execute(select(WorkShard)).scalar_one()
            scan_unit = session.execute(select(ScanUnit)).scalar_one()
            assert job.status == "running"
            assert job.stop_requested is False
            assert shard.status == "pending"
            assert shard.failure_category is None
            assert shard.finished_at is None
            assert scan_unit.status == "pending"
            assert scan_unit.failure_category is None
            assert scan_unit.finished_at is None
    finally:
        engine.dispose()


def test_stop_reclaimable_policy_ownership_and_call_order_are_static() -> None:
    scheduling_path = ROOT / "ocr_platform" / "control" / "scheduling.py"
    manifests_path = (
        ROOT
        / "ocr_platform"
        / "control"
        / "domains"
        / "manifests"
        / "core.py"
    )
    jobs_compat_path = (
        ROOT / "ocr_platform" / "control" / "domains" / "jobs" / "core.py"
    )
    jobs_lifecycle_path = (
        ROOT / "ocr_platform" / "control" / "domains" / "jobs" / "lifecycle.py"
    )
    workers_path = (
        ROOT
        / "ocr_platform"
        / "control"
        / "domains"
        / "workers"
        / "core.py"
    )

    def function_source(path: Path, name: str) -> str:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        return ast.get_source_segment(source, function)

    policy_source = function_source(
        scheduling_path,
        "stop_reclaimable_work_for_job",
    )
    assert policy_source.count("current_time = utcnow()") == 1
    assert policy_source.count("session.execute(") == 2
    assert "session.commit(" not in policy_source
    assert "session.rollback(" not in policy_source
    assert "session.flush(" not in policy_source
    assert "ShardAttempt" not in policy_source
    assert "_finalize_job_after_shard_change" not in policy_source
    assert "update(WorkShard)" in policy_source
    assert "update(ScanUnit)" in policy_source
    assert policy_source.count('status="stopped"') == 2
    assert policy_source.count(
        'failure_category="operator_stopped"'
    ) == 2
    assert policy_source.count("lease_expires_at=None") == 2
    assert policy_source.count("finished_at=current_time") == 2

    for path in (manifests_path, jobs_compat_path, workers_path):
        wrapper_source = function_source(
            path,
            "stop_reclaimable_work_for_job",
        )
        assert (
            "from ...scheduling import "
            "stop_reclaimable_work_for_job as target"
        ) in wrapper_source
        assert "session.execute(" not in wrapper_source
        assert 'status="stopped"' not in wrapper_source

    request_stop_source = function_source(jobs_lifecycle_path, "request_stop")
    assert request_stop_source.index(
        "job = _lock_job_for_shard_change("
    ) < request_stop_source.index("policy.request_stop(job)")
    assert request_stop_source.index(
        "policy.request_stop(job)"
    ) < request_stop_source.index(
        "stop_reclaimable_work_for_job(session, job)"
    )
    assert request_stop_source.index(
        "stop_reclaimable_work_for_job(session, job)"
    ) < request_stop_source.index(
        "finalize_stopped_job_if_idle(session, job)"
    )
    assert "session.commit()" not in request_stop_source
    assert "session.rollback()" not in request_stop_source

    worker_stop_source = function_source(
        workers_path,
        "stop_assigned_queued_jobs_for_server",
    )
    assert worker_stop_source.index(
        'job.status = "stopped"'
    ) < worker_stop_source.index(
        "stop_reclaimable_work_for_job(session, job)"
    )
    assert worker_stop_source.index(
        "stop_reclaimable_work_for_job(session, job)"
    ) < worker_stop_source.index("session.flush()")
