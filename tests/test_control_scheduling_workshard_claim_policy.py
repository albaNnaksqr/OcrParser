from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from ocr_platform.control import scheduling
from ocr_platform.control.database import init_db
from ocr_platform.control.domains.common import (
    RECLAIMABLE_SHARD_STATUSES,
    SHARD_LEASE_SECONDS,
    TERMINAL_JOB_STATUSES,
)
from ocr_platform.control.models import (
    Job,
    Manifest,
    Server,
    ShardAttempt,
    WorkShard,
)


ROOT = Path(__file__).resolve().parents[1]
NON_CLAIMABLE_JOB_STATUSES = {"stopping", *TERMINAL_JOB_STATUSES}


def _database():
    engine = create_engine("sqlite://", future=True)
    init_db(engine)
    return engine, sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )


def _seed_job_and_shard(
    session,
    *,
    suffix: str,
    shard_status: str = "pending",
    shard_index: int = 1,
    attempt_count: int = 0,
    max_attempts: int = 3,
    job_status: str = "running",
) -> tuple[Job, WorkShard]:
    server_id = f"server-{suffix}"
    session.add(
        Server(
            id=server_id,
            name=server_id,
            host="localhost",
        )
    )
    job = Job(
        id=f"job-{suffix}",
        input_dir=f"/shared/input-{suffix}",
        output_dir=f"/shared/output-{suffix}",
        engine="dotsocr",
        input_mode="remote_folder_snapshot",
        assigned_server_id=server_id,
        status=job_status,
        max_shard_attempts=max_attempts,
    )
    manifest = Manifest(
        job_id=job.id,
        input_mode="remote_folder_snapshot",
        input_root=job.input_dir,
        manifest_path=f"/shared/{suffix}/manifest.jsonl",
        status="ready",
    )
    session.add_all([job, manifest])
    session.flush()
    shard = WorkShard(
        job_id=job.id,
        manifest_id=manifest.id,
        shard_index=shard_index,
        shard_path=f"/shared/{suffix}/shard-{shard_index}.jsonl",
        status=shard_status,
        attempt_count=attempt_count,
        file_count=7,
        processed_files=2,
        failed_files=1,
        skipped_files=1,
        completed_pages=9,
        execution_paused=True,
        api_concurrency_limit=3,
        execution_control_reason="pressure",
        failure_category="old_failure",
        error_message="old error",
    )
    session.add(shard)
    session.flush()
    return job, shard


def _claim(
    session,
    shard: WorkShard,
    *,
    server_id: str,
    now: datetime,
) -> WorkShard:
    return scheduling._claim_work_shard(
        session,
        shard_id=shard.id,
        job_id=shard.job_id,
        server_id=server_id,
        started_at=now,
        lease_expires_at=(
            now + timedelta(seconds=SHARD_LEASE_SECONDS)
        ),
        reclaimable_statuses=RECLAIMABLE_SHARD_STATUSES,
        non_claimable_job_statuses=NON_CLAIMABLE_JOB_STATUSES,
    )


def test_work_shard_selector_and_parent_lock_sql_are_owned_by_scheduling(
) -> None:
    selector = scheduling._claimable_shard_id_select("job-a")
    selector_sql = str(
        selector.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "JOIN jobs ON jobs.id = work_shards.job_id" in selector_sql
    assert "work_shards.job_id = 'job-a'" in selector_sql
    assert (
        "work_shards.attempt_count < jobs.max_shard_attempts"
        in selector_sql
    )
    assert any(
        (
            f"CASE WHEN (work_shards.status IN ({statuses})) "
            "THEN 0 ELSE 1 END"
        )
        in selector_sql
        for statuses in (
            "'retrying', 'stale'",
            "'stale', 'retrying'",
        )
    )
    assert "work_shards.shard_index ASC" in selector_sql
    assert "LIMIT 1" in selector_sql
    assert "FOR UPDATE OF work_shards SKIP LOCKED" in selector_sql

    parent_lock = scheduling._claim_parent_job_for_key_share_select(
        "job-a"
    )
    parent_sql = str(
        parent_lock.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "WHERE jobs.id = 'job-a' FOR KEY SHARE" in parent_sql


def test_work_shard_selector_keeps_recovery_then_shard_index_order(
) -> None:
    engine, session_factory = _database()
    try:
        with session_factory.begin() as session:
            job, _ = _seed_job_and_shard(
                session,
                suffix="ordering",
                shard_status="pending",
                shard_index=1,
            )
            manifest_id = session.scalar(
                select(Manifest.id).where(Manifest.job_id == job.id)
            )
            session.add_all(
                [
                    WorkShard(
                        job_id=job.id,
                        manifest_id=manifest_id,
                        shard_index=3,
                        shard_path="/shared/ordering/shard-3.jsonl",
                        status="stale",
                        attempt_count=1,
                        file_count=1,
                    ),
                    WorkShard(
                        job_id=job.id,
                        manifest_id=manifest_id,
                        shard_index=2,
                        shard_path="/shared/ordering/shard-2.jsonl",
                        status="retrying",
                        attempt_count=1,
                        file_count=1,
                    ),
                ]
            )
            session.flush()

        with session_factory.begin() as session:
            shard_id = session.execute(
                scheduling._claimable_shard_id_select(job.id)
            ).scalar_one()
            selected = session.get(WorkShard, shard_id)
            assert (selected.status, selected.shard_index) == (
                "retrying",
                2,
            )
    finally:
        engine.dispose()


def test_work_shard_claim_cas_creates_exact_attempt_snapshot() -> None:
    engine, session_factory = _database()
    now = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
    try:
        with session_factory.begin() as session:
            job, shard = _seed_job_and_shard(
                session,
                suffix="success",
                shard_status="stale",
                attempt_count=2,
            )
            shard_id = shard.id
            server_id = job.assigned_server_id

        with session_factory.begin() as session:
            shard = session.get(WorkShard, shard_id)
            claimed = _claim(
                session,
                shard,
                server_id=server_id,
                now=now,
            )
            assert claimed.id == shard_id
            assert claimed.status == "running"
            assert claimed.assigned_server_id == server_id
            assert claimed.attempt_count == 3
            assert claimed.started_at == now
            assert claimed.finished_at is None
            assert claimed.lease_expires_at == (
                now + timedelta(seconds=SHARD_LEASE_SECONDS)
            )
            assert claimed.failure_category is None
            assert claimed.error_message is None

        with session_factory() as session:
            attempt = session.scalar(
                select(ShardAttempt).where(
                    ShardAttempt.shard_id == shard_id
                )
            )
            assert (
                attempt.job_id,
                attempt.shard_id,
                attempt.attempt_number,
                attempt.server_id,
                attempt.status,
            ) == (job.id, shard_id, 3, server_id, "running")
            assert (
                attempt.processed_files,
                attempt.failed_files,
                attempt.skipped_files,
                attempt.completed_pages,
            ) == (2, 1, 1, 9)
            assert attempt.execution_paused is True
            assert attempt.api_concurrency_limit == 3
            assert attempt.execution_control_reason == "pressure"
            assert attempt.failure_category is None
            assert attempt.error_message is None
            assert attempt.started_at == now
            assert attempt.finished_at is None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("job_status", "attempt_count", "max_attempts"),
    [
        ("stopping", 0, 3),
        ("running", 3, 3),
    ],
)
def test_work_shard_cas_loser_never_creates_attempt(
    job_status: str,
    attempt_count: int,
    max_attempts: int,
) -> None:
    engine, session_factory = _database()
    now = datetime(2026, 7, 28, 20, 5, tzinfo=timezone.utc)
    try:
        with session_factory.begin() as session:
            job, shard = _seed_job_and_shard(
                session,
                suffix=f"loser-{job_status}-{attempt_count}",
                attempt_count=attempt_count,
                max_attempts=max_attempts,
                job_status=job_status,
            )
            job_id = job.id
            shard_id = shard.id
            server_id = job.assigned_server_id

        with pytest.raises(
            scheduling._WorkShardClaimCollision,
            match="compare-and-set race",
        ):
            with session_factory.begin() as session:
                shard = session.get(WorkShard, shard_id)
                _claim(
                    session,
                    shard,
                    server_id=server_id,
                    now=now,
                )

        with session_factory() as session:
            shard = session.get(WorkShard, shard_id)
            assert shard.status == "pending"
            assert shard.assigned_server_id is None
            assert shard.attempt_count == attempt_count
            assert session.scalar(
                select(ShardAttempt).where(
                    ShardAttempt.job_id == job_id
                )
            ) is None
    finally:
        engine.dispose()


def test_work_shard_claim_preserves_get_none_without_attempt() -> None:
    class _Updated:
        rowcount = 1

    class _MissingShardSession:
        def __init__(self) -> None:
            self.refreshed = False
            self.added = False

        def execute(self, statement):
            return _Updated()

        def get(self, model, object_id):
            assert model is WorkShard
            assert object_id == 17
            return None

        def refresh(self, value):
            self.refreshed = True

        def add(self, value):
            self.added = True

    session = _MissingShardSession()
    now = datetime(2026, 7, 28, 20, 7, tzinfo=timezone.utc)
    claimed = scheduling._claim_work_shard(
        session,
        shard_id=17,
        job_id="job-missing",
        server_id="server-missing",
        started_at=now,
        lease_expires_at=now + timedelta(minutes=1),
        reclaimable_statuses=RECLAIMABLE_SHARD_STATUSES,
        non_claimable_job_statuses=NON_CLAIMABLE_JOB_STATUSES,
    )

    assert claimed is None
    assert session.refreshed is False
    assert session.added is False


def test_duplicate_attempt_rolls_back_claim_update() -> None:
    engine, session_factory = _database()
    now = datetime(2026, 7, 28, 20, 10, tzinfo=timezone.utc)
    try:
        with session_factory.begin() as session:
            job, shard = _seed_job_and_shard(
                session,
                suffix="duplicate",
            )
            job_id = job.id
            shard_id = shard.id
            server_id = job.assigned_server_id
            session.add(
                ShardAttempt(
                    job_id=job_id,
                    shard_id=shard_id,
                    attempt_number=1,
                    server_id=server_id,
                    status="failed",
                    started_at=now - timedelta(minutes=1),
                    finished_at=now,
                )
            )

        with pytest.raises(IntegrityError):
            with session_factory.begin() as session:
                shard = session.get(WorkShard, shard_id)
                _claim(
                    session,
                    shard,
                    server_id=server_id,
                    now=now,
                )

        with session_factory() as session:
            shard = session.get(WorkShard, shard_id)
            assert shard.status == "pending"
            assert shard.assigned_server_id is None
            assert shard.attempt_count == 0
            attempts = list(
                session.scalars(
                    select(ShardAttempt).where(
                        ShardAttempt.shard_id == shard_id
                    )
                )
            )
            assert len(attempts) == 1
            assert attempts[0].status == "failed"
    finally:
        engine.dispose()


def test_work_shard_claim_ownership_and_application_boundary_are_static(
) -> None:
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
        "_claimable_shard_id_select",
    )
    assert "select(WorkShard.id)" in selector
    assert "WorkShard.status.in_(RECLAIMABLE_SHARD_STATUSES)" in selector
    assert "WorkShard.attempt_count < Job.max_shard_attempts" in selector
    assert ".order_by(recovery_priority, WorkShard.shard_index.asc())" in selector
    assert ".with_for_update(skip_locked=True, of=WorkShard)" in selector

    parent = function_source(
        scheduling_path,
        "_claim_parent_job_for_key_share_select",
    )
    assert ".with_for_update(read=True, key_share=True)" in parent
    parent_lock = function_source(
        scheduling_path,
        "_lock_claim_parent_job",
    )
    assert "session.execute(" in parent_lock
    assert "populate_existing=True" in parent_lock

    claim = function_source(scheduling_path, "_claim_work_shard")
    assert "update(WorkShard)" in claim
    assert "WorkShard.status.in_(reclaimable_statuses)" in claim
    assert "WorkShard.attempt_count + 1" in claim
    assert "claimable_parent" in claim
    assert "Job.stop_requested.is_(False)" in claim
    assert "Job.status.not_in(non_claimable_job_statuses)" in claim
    assert "result.rowcount != 1" in claim
    assert "_WorkShardClaimCollision(claimable_parent)" in claim
    assert "session.get(WorkShard, shard_id)" in claim
    assert "session.refresh(shard)" in claim
    assert "_add_running_shard_attempt_snapshot(" in claim
    for source in (selector, parent, parent_lock, claim):
        assert "session.commit(" not in source
        assert "session.rollback(" not in source
        assert "session.flush(" not in source

    attempt = function_source(
        scheduling_path,
        "_add_running_shard_attempt_snapshot",
    )
    assert "ShardAttempt(" in attempt
    assert "attempt_number=shard.attempt_count" in attempt
    assert 'status="running"' in attempt

    wrapper = function_source(core_path, "_claimable_shard_id_select")
    assert (
        "from ...scheduling import "
        "_claimable_shard_id_select as target"
    ) in wrapper
    assert "select(WorkShard.id)" not in wrapper

    application = function_source(
        use_cases_path,
        "_claim_next_pending_shard",
    )
    reconcile_position = application.index(
        "_reconcile_expired_shard_leases("
    )
    parent_position = application.index(
        "scheduling_policy._lock_claim_parent_job("
    )
    selector_position = application.index(
        "scheduling_policy._claimable_shard_id_select("
    )
    claim_position = application.index(
        "scheduling_policy._claim_work_shard("
    )
    assert reconcile_position < parent_position < selector_position < claim_position
    assert application.count("server_can_access_input_dir(") == 2
    assert application.count("server_is_allowed_for_job(") == 2
    assert "update(WorkShard)" not in application
    assert "ShardAttempt(" not in application

    command = function_source(commands_path, "claim_next_pending_shard")
    assert "with session.begin():" in command
    assert "session.execute(" in command
    assert "except _scheduling._WorkShardClaimCollision as exc:" in command
    assert "collision_parent = exc.claimable_parent" in command

    production_attempt_constructors = []
    for path in (ROOT / "ocr_platform").rglob("*.py"):
        if path.name == "models.py":
            continue
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ShardAttempt"
            ):
                production_attempt_constructors.append(path)
    assert production_attempt_constructors == [scheduling_path]

    core_source = core_path.read_text(encoding="utf-8")
    assert "def _create_shard_attempt(" not in core_source
    assert "class _WorkShardClaimCollision" not in core_source
