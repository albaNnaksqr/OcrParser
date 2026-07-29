from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event as sa_event, select
from sqlalchemy.orm import sessionmaker

from ocr_platform.control import scheduling
from ocr_platform.control.database import init_db
from ocr_platform.control.domains.workers import commands as worker_commands
from ocr_platform.control.models import (
    Job,
    Manifest,
    ScanUnit,
    Server,
    ShardAttempt,
    WorkShard,
)
from ocr_platform.control.schemas import ServerRegisterRequest


ROOT = Path(__file__).resolve().parents[1]
SHARD_RESTART_ERROR = (
    "worker process re-registered before shard completion"
)
SCAN_RESTART_ERROR = (
    "worker process re-registered before scan completion"
)


def _session_factory():
    engine = create_engine("sqlite://", future=True)
    init_db(engine)
    return engine, sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )


def _seed_parent(session) -> tuple[Job, Manifest]:
    session.add_all(
        [
            Server(
                id="server-a",
                name="server-a-old",
                host="old-a",
                status="busy",
                capacity_slots=2,
                capabilities_json='{"generation":"old"}',
            ),
            Server(
                id="server-b",
                name="server-b",
                host="host-b",
                status="busy",
            ),
        ]
    )
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
    return job, manifest


def _snapshot(session) -> dict[str, object]:
    job = session.get(Job, "job-a")
    servers = list(
        session.execute(select(Server).order_by(Server.id)).scalars()
    )
    shards = list(
        session.execute(
            select(WorkShard).order_by(WorkShard.shard_index)
        ).scalars()
    )
    attempts = list(
        session.execute(
            select(ShardAttempt).order_by(
                ShardAttempt.shard_id,
                ShardAttempt.attempt_number,
            )
        ).scalars()
    )
    scan_units = list(
        session.execute(select(ScanUnit).order_by(ScanUnit.id)).scalars()
    )
    return {
        "job": (
            job.status,
            job.assigned_server_id,
            job.failure_category,
            job.error_message,
            job.stop_requested,
            job.started_at,
            job.finished_at,
        ),
        "servers": [
            (
                server.id,
                server.name,
                server.host,
                server.status,
                server.capacity_slots,
                server.capabilities_json,
                server.last_heartbeat_at,
                server.archived_at,
            )
            for server in servers
        ],
        "shards": [
            (
                shard.shard_index,
                shard.status,
                shard.assigned_server_id,
                shard.attempt_count,
                shard.processed_files,
                shard.failed_files,
                shard.skipped_files,
                shard.completed_pages,
                shard.failure_category,
                shard.error_message,
                shard.started_at,
                shard.finished_at,
                shard.lease_expires_at,
            )
            for shard in shards
        ],
        "attempts": [
            (
                attempt.shard_id,
                attempt.attempt_number,
                attempt.server_id,
                attempt.status,
                attempt.processed_files,
                attempt.failed_files,
                attempt.skipped_files,
                attempt.completed_pages,
                attempt.failure_category,
                attempt.error_message,
                attempt.started_at,
                attempt.finished_at,
            )
            for attempt in attempts
        ],
        "scan_units": [
            (
                unit.path,
                unit.status,
                unit.assigned_server_id,
                unit.attempt_count,
                unit.file_count,
                unit.total_bytes,
                unit.failure_category,
                unit.error_message,
                unit.started_at,
                unit.finished_at,
                unit.lease_expires_at,
            )
            for unit in scan_units
        ],
    }


def test_restart_fencing_updates_only_target_running_work_and_is_idempotent(
) -> None:
    engine, session_factory = _session_factory()
    fixed_now = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)
    started_at = fixed_now - timedelta(minutes=10)
    old_finished_at = fixed_now - timedelta(minutes=5)
    active_lease = fixed_now + timedelta(minutes=5)
    shard_statuses = [
        "running",
        "running",
        "pending",
        "retrying",
        "stale",
        "succeeded",
        "failed",
        "stopped",
    ]
    scan_statuses = [
        "running",
        "pending",
        "stale",
        "succeeded",
        "failed",
        "stopped",
    ]

    try:
        with session_factory.begin() as session:
            job, manifest = _seed_parent(session)
            job.started_at = started_at
            shards = [
                WorkShard(
                    job_id=job.id,
                    manifest_id=manifest.id,
                    shard_index=index,
                    shard_path=f"/shared/shard-{index}.jsonl",
                    status=status,
                    assigned_server_id="server-a",
                    attempt_count=2 if index == 1 else 1,
                    file_count=10,
                    processed_files=index,
                    failed_files=index + 1,
                    skipped_files=index + 2,
                    completed_pages=index + 3,
                    failure_category="original_category",
                    error_message="original shard error",
                    started_at=started_at,
                    finished_at=old_finished_at,
                    lease_expires_at=active_lease,
                )
                for index, status in enumerate(shard_statuses, start=1)
            ]
            other_server_shard = WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=20,
                shard_path="/shared/shard-20.jsonl",
                status="running",
                assigned_server_id="server-b",
                attempt_count=1,
                file_count=10,
                processed_files=20,
                failure_category="other_server",
                error_message="other server shard error",
                started_at=started_at,
                finished_at=old_finished_at,
                lease_expires_at=active_lease,
            )
            session.add_all([*shards, other_server_shard])
            session.flush()
            session.add_all(
                [
                    ShardAttempt(
                        job_id=job.id,
                        shard_id=shards[0].id,
                        attempt_number=1,
                        server_id="server-a",
                        status="running",
                        processed_files=11,
                        failure_category="old_attempt",
                        error_message="old running attempt",
                        started_at=started_at,
                        finished_at=old_finished_at,
                    ),
                    ShardAttempt(
                        job_id=job.id,
                        shard_id=shards[0].id,
                        attempt_number=2,
                        server_id="server-a",
                        status="running",
                        processed_files=12,
                        failed_files=1,
                        skipped_files=2,
                        completed_pages=3,
                        failure_category="current_attempt",
                        error_message="current running attempt",
                        started_at=started_at,
                    ),
                    ShardAttempt(
                        job_id=job.id,
                        shard_id=shards[1].id,
                        attempt_number=1,
                        server_id="server-a",
                        status="succeeded",
                        processed_files=13,
                        failure_category=None,
                        error_message=None,
                        started_at=started_at,
                        finished_at=old_finished_at,
                    ),
                    ShardAttempt(
                        job_id=job.id,
                        shard_id=other_server_shard.id,
                        attempt_number=1,
                        server_id="server-b",
                        status="running",
                        processed_files=14,
                        failure_category="other_server",
                        error_message="other server attempt",
                        started_at=started_at,
                    ),
                ]
            )
            session.add_all(
                [
                    ScanUnit(
                        job_id=job.id,
                        path=f"/shared/scan-{index}",
                        status=status,
                        assigned_server_id="server-a",
                        attempt_count=index,
                        file_count=index,
                        total_bytes=index * 10,
                        failure_category="original_category",
                        error_message="original scan error",
                        started_at=started_at,
                        finished_at=old_finished_at,
                        lease_expires_at=active_lease,
                    )
                    for index, status in enumerate(scan_statuses, start=1)
                ]
            )
            session.add(
                ScanUnit(
                    job_id=job.id,
                    path="/shared/scan-other-server",
                    status="running",
                    assigned_server_id="server-b",
                    attempt_count=1,
                    file_count=20,
                    total_bytes=200,
                    failure_category="other_server",
                    error_message="other server scan error",
                    started_at=started_at,
                    finished_at=old_finished_at,
                    lease_expires_at=active_lease,
                )
            )

        with session_factory.begin() as session:
            scheduling._fence_running_work_for_restarted_server(
                session,
                "server-a",
                now=fixed_now,
            )

        with session_factory() as session:
            first_snapshot = _snapshot(session)

        shard_rows = {
            row[0]: row for row in first_snapshot["shards"]
        }
        for shard_index in (1, 2):
            row = shard_rows[shard_index]
            assert row[1] == "stale"
            assert row[2] is None
            assert row[8] == "process_killed"
            assert row[9] == SHARD_RESTART_ERROR
            assert row[11] is None
            assert row[12] is None
        assert shard_rows[1][3:8] == (2, 1, 2, 3, 4)
        assert shard_rows[2][3:8] == (1, 2, 3, 4, 5)
        for shard_index in (3, 4, 5, 6, 7, 8, 20):
            row = shard_rows[shard_index]
            assert row[2] == (
                "server-b" if shard_index == 20 else "server-a"
            )
            assert row[8] == (
                "other_server"
                if shard_index == 20
                else "original_category"
            )
            assert row[9] == (
                "other server shard error"
                if shard_index == 20
                else "original shard error"
            )
            assert row[11] == old_finished_at
            assert row[12] == active_lease

        attempt_rows = first_snapshot["attempts"]
        current_attempt = next(
            row
            for row in attempt_rows
            if row[0] == shards[0].id and row[1] == 2
        )
        assert current_attempt[3:] == (
            "stale",
            12,
            1,
            2,
            3,
            "process_killed",
            SHARD_RESTART_ERROR,
            started_at,
            fixed_now,
        )
        old_attempt = next(
            row
            for row in attempt_rows
            if row[0] == shards[0].id and row[1] == 1
        )
        assert old_attempt[3] == "running"
        assert old_attempt[8:12] == (
            "old_attempt",
            "old running attempt",
            started_at,
            old_finished_at,
        )
        terminal_attempt = next(
            row
            for row in attempt_rows
            if row[0] == shards[1].id and row[1] == 1
        )
        assert terminal_attempt[3] == "succeeded"
        assert terminal_attempt[11] == old_finished_at
        other_attempt = next(
            row for row in attempt_rows if row[2] == "server-b"
        )
        assert other_attempt[3] == "running"
        assert len(attempt_rows) == 4

        scan_rows = {
            row[0]: row for row in first_snapshot["scan_units"]
        }
        fenced_scan = scan_rows["/shared/scan-1"]
        assert fenced_scan[1] == "stale"
        assert fenced_scan[2] is None
        assert fenced_scan[3:6] == (1, 1, 10)
        assert fenced_scan[6] == "process_killed"
        assert fenced_scan[7] == SCAN_RESTART_ERROR
        assert fenced_scan[8] == started_at
        assert fenced_scan[9] is None
        assert fenced_scan[10] is None
        for path in [
            "/shared/scan-2",
            "/shared/scan-3",
            "/shared/scan-4",
            "/shared/scan-5",
            "/shared/scan-6",
            "/shared/scan-other-server",
        ]:
            row = scan_rows[path]
            assert row[2] == (
                "server-b"
                if path == "/shared/scan-other-server"
                else "server-a"
            )
            assert row[9] == old_finished_at
            assert row[10] == active_lease
        assert first_snapshot["job"] == (
            "running",
            "server-a",
            None,
            None,
            False,
            started_at,
            None,
        )
        assert [row[0] for row in first_snapshot["servers"]] == [
            "server-a",
            "server-b",
        ]

        with session_factory.begin() as session:
            scheduling._fence_running_work_for_restarted_server(
                session,
                "server-a",
                now=fixed_now + timedelta(hours=1),
            )
        with session_factory() as session:
            assert _snapshot(session) == first_snapshot
    finally:
        engine.dispose()


def test_register_restart_fencing_rolls_back_all_phases_on_scan_failure(
) -> None:
    engine, session_factory = _session_factory()
    fixed_now = datetime(2026, 7, 28, 17, 0, tzinfo=timezone.utc)
    active_lease = fixed_now + timedelta(minutes=5)

    try:
        with session_factory.begin() as session:
            job, manifest = _seed_parent(session)
            shard = WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=1,
                shard_path="/shared/shard-1.jsonl",
                status="running",
                assigned_server_id="server-a",
                attempt_count=1,
                file_count=1,
                failure_category="original",
                error_message="original shard error",
                lease_expires_at=active_lease,
            )
            session.add(shard)
            session.flush()
            session.add(
                ShardAttempt(
                    job_id=job.id,
                    shard_id=shard.id,
                    attempt_number=1,
                    server_id="server-a",
                    status="running",
                    failure_category="original",
                    error_message="original attempt error",
                    started_at=fixed_now,
                )
            )
            session.add(
                ScanUnit(
                    job_id=job.id,
                    path="/shared/scan-1",
                    status="running",
                    assigned_server_id="server-a",
                    attempt_count=1,
                    failure_category="original",
                    error_message="original scan error",
                    lease_expires_at=active_lease,
                )
            )

        with session_factory() as session:
            before = _snapshot(session)

        def fail_scan_update(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ):
            if statement.lstrip().upper().startswith("UPDATE SCAN_UNITS"):
                raise RuntimeError("injected scan fencing failure")

        sa_event.listen(engine, "before_cursor_execute", fail_scan_update)
        try:
            with pytest.raises(
                RuntimeError,
                match="injected scan fencing failure",
            ):
                with session_factory() as session:
                    worker_commands.register_server(
                        session,
                        ServerRegisterRequest(
                            id="server-a",
                            name="server-a-new",
                            host="new-a",
                            capacity_slots=9,
                            capabilities={"generation": "new"},
                        ),
                    )
        finally:
            sa_event.remove(
                engine,
                "before_cursor_execute",
                fail_scan_update,
            )

        with session_factory() as session:
            assert _snapshot(session) == before
    finally:
        engine.dispose()


def test_restart_fencing_policy_ownership_lock_and_call_order_are_static(
) -> None:
    scheduling_path = ROOT / "ocr_platform" / "control" / "scheduling.py"
    registration_path = (
        ROOT
        / "ocr_platform"
        / "control"
        / "domains"
        / "workers"
        / "registration.py"
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
        "_fence_running_work_for_restarted_server",
    )
    assert policy_source.count("session.execute(") == 4
    assert policy_source.index(
        "select(WorkShard.id)"
    ) < policy_source.index(".order_by(WorkShard.id.asc())")
    assert policy_source.index(
        ".order_by(WorkShard.id.asc())"
    ) < policy_source.index(".with_for_update()")
    assert policy_source.index(
        ".with_for_update()"
    ) < policy_source.index("update(WorkShard)")
    assert policy_source.index(
        "update(WorkShard)"
    ) < policy_source.index("update(ShardAttempt)")
    assert policy_source.index(
        "update(ShardAttempt)"
    ) < policy_source.index("update(ScanUnit)")
    assert "ShardAttempt.attempt_number" in policy_source
    assert "WorkShard.attempt_count" in policy_source
    assert 'ShardAttempt.status == "running"' in policy_source
    assert policy_source.count('status="stale"') == 3
    assert policy_source.count('failure_category="process_killed"') == 3
    assert policy_source.count("finished_at=None") == 2
    assert "finished_at=now" in policy_source
    assert "attempt_count=" not in policy_source
    assert "session.commit(" not in policy_source
    assert "session.rollback(" not in policy_source
    assert "session.flush(" not in policy_source
    assert "finalize" not in policy_source
    assert "select(Server" not in policy_source
    assert "select(Job" not in policy_source

    register_source = function_source(registration_path, "register")
    assert register_source.index(
        "engine_provenance.sanitize_capabilities"
    ) < register_source.index("select(Server)")
    assert register_source.index(
        "select(Server)"
    ) < register_source.index(".with_for_update()")
    assert register_source.index(
        ".with_for_update()"
    ) < register_source.index("if server is None:")
    assert register_source.index(
        "else:"
    ) < register_source.index(
        "scheduling._fence_running_work_for_restarted_server("
    )
    assert register_source.index(
        "scheduling._fence_running_work_for_restarted_server("
    ) < register_source.index("policy.apply_registration(")
    assert register_source.index(
        "policy.apply_registration("
    ) < register_source.index("session.flush()")
    assert register_source.index(
        "session.flush()"
    ) < register_source.index("session.refresh(server)")
    assert register_source.count(
        "_fence_running_work_for_restarted_server("
    ) == 1
    assert "session.commit(" not in register_source
    assert "session.rollback(" not in register_source
