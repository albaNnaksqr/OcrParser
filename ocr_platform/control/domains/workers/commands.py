"""Transactional worker application commands."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.orm import Session

from ...models import Job, Server
from ...schemas import ServerHeartbeatRequest, ServerRegisterRequest
from ..common import ServerArchiveError, UnknownServerError
from . import assignment, registration, use_cases


class WorkerCommandTransactionError(RuntimeError):
    """Raised when a worker command cannot own its transaction."""


ACTIVE_TRANSACTION_ERROR = (
    "worker command requires a session without an active transaction"
)
CommandResult = TypeVar("CommandResult")


def _run_worker_command(
    session: Session,
    operation: Callable[[], CommandResult],
) -> CommandResult:
    if session.in_transaction():
        raise WorkerCommandTransactionError(
            ACTIVE_TRANSACTION_ERROR
        )
    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        with session.begin():
            result = operation()
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return result


def register_server(
    session: Session,
    request: ServerRegisterRequest,
) -> Server:
    return _run_worker_command(
        session,
        lambda: registration.register(session, request),
    )


def heartbeat_server(
    session: Session,
    server_id: str,
    request: ServerHeartbeatRequest,
) -> Server:
    return _run_worker_command(
        session,
        lambda: registration.heartbeat(
            session,
            server_id,
            request,
        ),
    )


def archive_server(session: Session, server_id: str) -> None:
    _run_worker_command(
        session,
        lambda: use_cases.archive_server(session, server_id),
    )


def claim_next_job(
    session: Session,
    server_id: str,
) -> Job | None:
    while True:
        try:
            return _run_worker_command(
                session,
                lambda: assignment.claim_next_job(
                    session,
                    server_id,
                ),
            )
        except assignment.JobClaimCollision:
            continue


def claim_next_pool_job(
    session: Session,
    server_id: str,
) -> Job | None:
    while True:
        try:
            return _run_worker_command(
                session,
                lambda: assignment.claim_next_pool_job(
                    session,
                    server_id,
                ),
            )
        except assignment.JobClaimCollision:
            continue


__all__ = [
    "ACTIVE_TRANSACTION_ERROR",
    "ServerArchiveError",
    "UnknownServerError",
    "WorkerCommandTransactionError",
    "archive_server",
    "claim_next_job",
    "claim_next_pool_job",
    "heartbeat_server",
    "register_server",
]
