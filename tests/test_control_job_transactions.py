from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine, event as sa_event, inspect, select
from sqlalchemy.orm import sessionmaker

from ocr_platform.control.database import init_db
from ocr_platform.control.domains.jobs import (
    commands,
    counters,
    events,
    lifecycle,
    logs,
    queries,
)
from ocr_platform.control.domains.jobs.commands import (
    JobCommandTransactionError,
    RECORD_EVENT_ACTIVE_TRANSACTION_ERROR,
    RECORD_LOG_ACTIVE_TRANSACTION_ERROR,
)
from ocr_platform.control.limits import ControlLimits
from ocr_platform.control.models import (
    Job,
    JobCounter,
    JobEvent,
    JobFile,
    JobLog,
    Server,
)
from ocr_platform.control.schemas import JobEventRequest, JobLogRequest


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
            if session.get(Server, "server-a") is None:
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
                    assigned_server_id="server-a",
                    status="queued",
                )
            )


def _event_request() -> JobEventRequest:
    return JobEventRequest(
        type="file_started",
        payload={
            "file_path": "/shared/input/a.pdf",
            "filename": "a.pdf",
            "total_pages": 2,
        },
    )


def _log_request(*, line: str = "started") -> JobLogRequest:
    return JobLogRequest(
        server_id="server-a",
        stream="stdout",
        line=line,
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


def test_record_event_command_commits_exactly_once(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            job = commands.record_event(
                session,
                "job-a",
                _event_request(),
            )

            assert job.id == "job-a"
            assert commits == [1]
            assert rollbacks == []
            assert session.in_transaction() is False

        with session_factory() as session:
            assert session.scalar(
                select(JobCounter).where(JobCounter.job_id == "job-a")
            ) is not None
            assert session.scalar(
                select(JobEvent).where(JobEvent.job_id == "job-a")
            ) is not None
            assert session.scalar(
                select(JobFile).where(JobFile.job_id == "job-a")
            ) is not None
    finally:
        engine.dispose()

def test_record_log_command_commits_exactly_once(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            row = commands.record_log(
                session,
                "job-a",
                _log_request(),
            )

            assert row.id is not None
            assert commits == [1]
            assert rollbacks == []
            assert session.in_transaction() is False

        with session_factory() as session:
            persisted = session.scalar(
                select(JobLog).where(JobLog.job_id == "job-a")
            )
            assert persisted is not None
            assert persisted.line == "started"
    finally:
        engine.dispose()


def test_record_log_limit_zero_commits_without_persisting(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            row = commands.record_log(
                session,
                "job-a",
                _log_request(line="not retained"),
                limits=ControlLimits(job_log_detail_limit=0),
            )

            assert row.id is None
            assert row.line == "not retained"
            assert commits == [1]
            assert rollbacks == []
            assert session.in_transaction() is False

        with session_factory() as session:
            assert session.scalar(
                select(JobLog).where(JobLog.job_id == "job-a")
            ) is None
    finally:
        engine.dispose()


def test_request_stop_command_commits_exactly_once(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)
    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            job = commands.request_stop(session, "job-a")
            assert job.status == "stopped"
            assert job.stop_requested is True
            assert commits == [1]
            assert rollbacks == []
            assert session.in_transaction() is False
    finally:
        engine.dispose()


def test_request_stop_rolls_back_job_policy_and_coordinated_work(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)

    def fail_after_job_policy(session, job):
        raise RuntimeError("injected coordinated stop failure")

    monkeypatch.setattr(
        lifecycle,
        "stop_reclaimable_work_for_job",
        fail_after_job_policy,
    )
    try:
        with session_factory() as session:
            with pytest.raises(
                RuntimeError,
                match="injected coordinated stop failure",
            ):
                commands.request_stop(session, "job-a")
        with session_factory() as session:
            job = session.get(Job, "job-a")
            assert job.status == "queued"
            assert job.stop_requested is False
    finally:
        engine.dispose()


def test_job_summary_query_is_read_only_after_explicit_refresh(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)
    try:
        with session_factory() as session:
            commands.refresh_job_summary(session, "job-a")
            commits, rollbacks = _transaction_observers(session)
            summary = queries.get_job_summary(session, "job-a")
            assert summary.id == "job-a"
            assert commits == []
            assert rollbacks == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("command_name", ["record_event", "record_log"])
@pytest.mark.parametrize("transaction_mode", ["explicit", "autobegin"])
def test_job_commands_reject_active_transaction_without_polluting_outer_work(
    tmp_path,
    command_name,
    transaction_mode,
) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)
    expected_error = (
        RECORD_EVENT_ACTIVE_TRANSACTION_ERROR
        if command_name == "record_event"
        else RECORD_LOG_ACTIVE_TRANSACTION_ERROR
    )

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
                JobCommandTransactionError,
                match=f"^{re.escape(expected_error)}$",
            ):
                if command_name == "record_event":
                    commands.record_event(
                        session,
                        "job-a",
                        _event_request(),
                    )
                else:
                    commands.record_log(
                        session,
                        "job-a",
                        _log_request(),
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
                select(JobEvent).where(JobEvent.job_id == "job-a")
            ) is None
    finally:
        engine.dispose()


def _assert_event_work_rolled_back(session_factory) -> None:
    with session_factory() as session:
        job = session.get(Job, "job-a")
        assert job.status == "queued"
        assert session.get(JobCounter, "job-a") is None
        assert session.scalar(
            select(JobEvent).where(JobEvent.job_id == "job-a")
        ) is None
        assert session.scalar(
            select(JobFile).where(JobFile.job_id == "job-a")
        ) is None


def test_record_event_second_direct_flush_failure_rolls_back_all_work(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            original_flush = session.flush
            original_upsert = counters.upsert_job_file_from_event
            flush_count = 0

            def mark_job_and_upsert(current, job, request):
                job.status = "running"
                return original_upsert(current, job, request)

            def fail_third_total_flush(*args, **kwargs):
                nonlocal flush_count
                flush_count += 1
                if flush_count == 3:
                    raise RuntimeError("second direct event flush failed")
                return original_flush(*args, **kwargs)

            monkeypatch.setattr(
                events,
                "upsert_job_file_from_event",
                mark_job_and_upsert,
            )
            monkeypatch.setattr(session, "flush", fail_third_total_flush)

            with pytest.raises(
                RuntimeError,
                match="second direct event flush failed",
            ):
                commands.record_event(
                    session,
                    "job-a",
                    _event_request(),
                )

            assert flush_count == 3
            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False

        _assert_event_work_rolled_back(session_factory)
    finally:
        engine.dispose()


def test_record_event_prune_failure_rolls_back_all_work(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)

    def fail_prune(session, job_id, *, limits=None):
        session.get(Job, job_id).status = "running"
        raise RuntimeError("event prune failed")

    monkeypatch.setattr(events, "prune_job_detail_rows", fail_prune)
    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            with pytest.raises(RuntimeError, match="event prune failed"):
                commands.record_event(
                    session,
                    "job-a",
                    _event_request(),
                )

            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False

        _assert_event_work_rolled_back(session_factory)
    finally:
        engine.dispose()


def test_record_log_failure_after_flush_rolls_back_row(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)

    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)

            def fail_retention_query(*args, **kwargs):
                raise RuntimeError("log retention query failed")

            monkeypatch.setattr(session, "execute", fail_retention_query)

            with pytest.raises(
                RuntimeError,
                match="log retention query failed",
            ):
                commands.record_log(
                    session,
                    "job-a",
                    _log_request(),
                )

            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False

        with session_factory() as session:
            assert session.scalar(
                select(JobLog).where(JobLog.job_id == "job-a")
            ) is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("expire_on_commit", [False, True])
def test_job_command_results_remain_readable_and_restore_expiry(
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
            job = commands.record_event(
                session,
                "job-a",
                _event_request(),
            )
            assert session.expire_on_commit is expire_on_commit
            assert session.in_transaction() is False
            job_values = (job.id, job.status, job.created_at)

            row = commands.record_log(
                session,
                "job-a",
                _log_request(),
            )
            assert session.expire_on_commit is expire_on_commit
            assert session.in_transaction() is False
            row_values = (
                row.id,
                row.job_id,
                row.server_id,
                row.stream,
                row.line,
                row.created_at,
            )

        assert inspect(job).detached is True
        assert inspect(row).detached is True
        assert (job.id, job.status, job.created_at) == job_values
        assert (
            row.id,
            row.job_id,
            row.server_id,
            row.stream,
            row.line,
            row.created_at,
        ) == row_values
    finally:
        engine.dispose()


@pytest.mark.parametrize("command_name", ["record_event", "record_log"])
def test_job_command_resolves_legacy_limits_once(
    tmp_path,
    monkeypatch,
    command_name,
) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)
    resolved = ControlLimits()
    calls = 0

    def resolve_limits():
        nonlocal calls
        calls += 1
        return resolved

    monkeypatch.setattr(commands, "legacy_control_limits", resolve_limits)
    try:
        with session_factory() as session:
            if command_name == "record_event":
                commands.record_event(
                    session,
                    "job-a",
                    _event_request(),
                )
            else:
                commands.record_log(
                    session,
                    "job-a",
                    _log_request(),
                )

        assert calls == 1
    finally:
        engine.dispose()


def test_job_command_dynamically_delegates_to_owned_event_leaf(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    _seed_job(session_factory)

    try:
        delegated_limits: list[ControlLimits] = []

        def patched_event_leaf(
            session,
            job_id,
            request,
            *,
            limits=None,
        ):
            delegated_limits.append(limits)
            return session.get(Job, job_id)

        monkeypatch.setattr(events, "record_event", patched_event_leaf)
        with session_factory() as session:
            delegated = commands.record_event(
                session,
                "job-a",
                _event_request(),
            )
            assert delegated.id == "job-a"
            assert session.in_transaction() is False
        assert len(delegated_limits) == 1
        assert isinstance(delegated_limits[0], ControlLimits)
    finally:
        engine.dispose()
