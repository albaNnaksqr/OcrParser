from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event as sa_event, select
from sqlalchemy.orm import sessionmaker

from ocr_platform.control.database import init_db
from ocr_platform.control.domains.workers import commands, policy
from ocr_platform.control.domains.workers.commands import (
    ACTIVE_TRANSACTION_ERROR,
    WorkerCommandTransactionError,
)
from ocr_platform.control.models import Job, Server, utcnow
from ocr_platform.control.schemas import (
    ServerHeartbeatRequest,
    ServerRegisterRequest,
)


def _database(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'control.db'}",
        future=True,
    )
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    init_db(engine)
    return session_factory, engine


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


def _register_request() -> ServerRegisterRequest:
    return ServerRegisterRequest(
        id="server-a",
        name="Server A",
        host="localhost",
        capacity_slots=2,
        capabilities={"generation": "test"},
    )


def test_register_command_commits_exactly_once(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            server = commands.register_server(
                session,
                _register_request(),
            )
            assert server.id == "server-a"
            assert server.status == "online"
            assert commits == [1]
            assert rollbacks == []
            assert session.in_transaction() is False
        with session_factory() as session:
            assert session.get(Server, "server-a") is not None
    finally:
        engine.dispose()


def test_heartbeat_command_commits_exactly_once(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    try:
        with session_factory() as session:
            commands.register_server(session, _register_request())
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            server = commands.heartbeat_server(
                session,
                "server-a",
                ServerHeartbeatRequest(
                    status="idle",
                    capabilities={"heartbeat": True},
                ),
            )
            assert server.status == "idle"
            assert commits == [1]
            assert rollbacks == []
            assert session.in_transaction() is False
    finally:
        engine.dispose()


def test_claim_command_commits_exactly_once(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    try:
        with session_factory() as session:
            with session.begin():
                session.add(
                    Server(
                        id="server-a",
                        name="Server A",
                        host="localhost",
                        status="online",
                    )
                )
                session.add(
                    Job(
                        id="job-a",
                        input_dir="/shared/input",
                        output_dir="/shared/output",
                        engine="dotsocr",
                        assigned_server_id="server-a",
                        status="queued",
                    )
                )
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            job = commands.claim_next_job(session, "server-a")
            assert job is not None
            assert job.status == "running"
            assert commits == [1]
            assert rollbacks == []
            assert session.in_transaction() is False
        with session_factory() as session:
            assert session.get(Job, "job-a").status == "running"
    finally:
        engine.dispose()


def test_archive_command_commits_exactly_once(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    try:
        with session_factory() as session:
            with session.begin():
                session.add(
                    Server(
                        id="server-a",
                        name="Server A",
                        host="localhost",
                        status="idle",
                        last_heartbeat_at=(
                            utcnow() - timedelta(seconds=300)
                        ),
                    )
                )
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            commands.archive_server(session, "server-a")
            assert commits == [1]
            assert rollbacks == []
            assert session.in_transaction() is False
        with session_factory() as session:
            assert session.get(Server, "server-a").archived_at is not None
    finally:
        engine.dispose()


def test_registration_policy_failure_rolls_back_entire_command(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _database(tmp_path)
    original = policy.apply_registration

    def fail_after_policy(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected worker policy failure")

    monkeypatch.setattr(
        policy,
        "apply_registration",
        fail_after_policy,
    )
    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            with pytest.raises(
                RuntimeError,
                match="injected worker policy failure",
            ):
                commands.register_server(
                    session,
                    _register_request(),
                )
            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False
        with session_factory() as session:
            assert session.scalar(
                select(Server).where(Server.id == "server-a")
            ) is None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "command",
    [
        lambda session: commands.register_server(
            session,
            _register_request(),
        ),
        lambda session: commands.heartbeat_server(
            session,
            "server-a",
            ServerHeartbeatRequest(status="idle"),
        ),
        lambda session: commands.claim_next_job(
            session,
            "server-a",
        ),
        lambda session: commands.archive_server(
            session,
            "server-a",
        ),
    ],
)
def test_worker_commands_reject_active_transaction(
    tmp_path,
    command,
) -> None:
    session_factory, engine = _database(tmp_path)
    try:
        with session_factory() as session:
            commits, rollbacks = _transaction_observers(session)
            with session.begin():
                session.add(
                    Server(
                        id="outer",
                        name="Outer",
                        host="localhost",
                    )
                )
                with pytest.raises(
                    WorkerCommandTransactionError,
                    match=ACTIVE_TRANSACTION_ERROR,
                ):
                    command(session)
                assert session.in_transaction() is True
                assert commits == []
                assert rollbacks == []
            assert commits == [1]
            assert rollbacks == []
    finally:
        engine.dispose()
