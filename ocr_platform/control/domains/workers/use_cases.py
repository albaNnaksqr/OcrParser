"""Cross-domain worker application use cases."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ...models import Server
from ..common import (
    POOL_SERVER_ID,
    ServerArchiveError,
    UnknownServerError,
    utcnow,
)
from ..jobs.lifecycle import stop_assigned_queued_jobs_for_server
from . import policy
from .identity import effective_server_status
from .projection import (
    count_open_jobs_for_server,
    count_running_shards_for_server,
)


def archive_server(session: Session, server_id: str) -> None:
    if server_id == POOL_SERVER_ID:
        raise ServerArchiveError(
            "The internal server pool cannot be archived."
        )
    server = session.get(Server, server_id)
    if server is None:
        raise UnknownServerError(f"Unknown server: {server_id}")
    if server.archived_at is not None:
        return
    if effective_server_status(server) != "offline":
        raise ServerArchiveError(
            "Only offline or stale servers can be archived."
        )
    stop_assigned_queued_jobs_for_server(session, server_id)
    if (
        count_open_jobs_for_server(session, server_id) > 0
        or count_running_shards_for_server(session, server_id) > 0
    ):
        raise ServerArchiveError("Server still has active work.")

    policy.archive(server, now=utcnow())
    session.flush()


__all__ = ["archive_server"]
