"""Pure worker and assignment projections."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import Job, Server, WorkShard
from ..common import (
    POOL_SERVER_ID,
    TERMINAL_JOB_STATUSES,
    json_loads_object,
)
from .identity import allowed_server_ids_for_job


def count_active_jobs_for_server(
    session: Session,
    server_id: str,
) -> int:
    return int(
        session.execute(
            select(func.count(Job.id))
            .where(Job.assigned_server_id == server_id)
            .where(Job.status.in_({"running", "stopping"}))
        ).scalar_one()
        or 0
    )


def count_open_jobs_for_server(
    session: Session,
    server_id: str,
) -> int:
    return int(
        session.execute(
            select(func.count(Job.id))
            .where(Job.assigned_server_id == server_id)
            .where(Job.status.not_in(TERMINAL_JOB_STATUSES))
        ).scalar_one()
        or 0
    )


def count_running_shards_for_server(
    session: Session,
    server_id: str,
) -> int:
    return int(
        session.execute(
            select(func.count(WorkShard.id))
            .where(WorkShard.assigned_server_id == server_id)
            .where(WorkShard.status == "running")
        ).scalar_one()
        or 0
    )


def list_servers(
    session: Session,
    *,
    include_archived: bool = False,
) -> list[Server]:
    stmt = select(Server).order_by(Server.id.asc())
    if not include_archived:
        stmt = stmt.where(Server.archived_at.is_(None))
    return list(session.execute(stmt).scalars().all())


def server_versions(
    session: Session,
    server_ids: set[str],
) -> dict[str, list[str]]:
    versions: dict[str, list[str]] = {}
    if not server_ids:
        return versions
    servers = session.execute(
        select(Server)
        .where(Server.id.in_(server_ids))
        .where(Server.archived_at.is_(None))
    ).scalars().all()
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        key = " / ".join(
            [
                str(capabilities.get("git_ref") or "unknown git"),
                str(
                    capabilities.get("script_version")
                    or "unknown script"
                ),
            ]
        )
        versions.setdefault(key, []).append(server.id)
    return versions


def job_worker_server_ids(session: Session, job: Job) -> set[str]:
    server_ids = {
        str(server_id)
        for server_id in allowed_server_ids_for_job(job)
        if server_id and server_id != POOL_SERVER_ID
    }
    if (
        job.assigned_server_id
        and job.assigned_server_id != POOL_SERVER_ID
    ):
        server_ids.add(job.assigned_server_id)
    assigned_shard_servers = session.execute(
        select(WorkShard.assigned_server_id)
        .where(WorkShard.job_id == job.id)
        .where(WorkShard.assigned_server_id.is_not(None))
    ).scalars().all()
    server_ids.update(
        str(server_id)
        for server_id in assigned_shard_servers
        if server_id
    )
    return server_ids


def job_worker_version_summary(
    session: Session,
    job: Job,
) -> dict[str, Any]:
    versions = server_versions(session, job_worker_server_ids(session, job))
    if not versions:
        return {
            "worker_version_status": "unknown",
            "worker_version_warning": None,
            "worker_version_refs": {},
        }
    if len(versions) == 1:
        return {
            "worker_version_status": "consistent",
            "worker_version_warning": None,
            "worker_version_refs": versions,
        }
    return {
        "worker_version_status": "mixed",
        "worker_version_warning": (
            "assigned workers report different git_ref or "
            "script_version values"
        ),
        "worker_version_refs": versions,
    }


def resource_constrained_workers(
    session: Session,
    server_ids: set[str],
) -> list[dict[str, Any]]:
    if not server_ids:
        return []
    servers = session.execute(
        select(Server)
        .where(Server.id.in_(server_ids))
        .where(Server.archived_at.is_(None))
        .order_by(Server.id.asc())
    ).scalars().all()
    constrained: list[dict[str, Any]] = []
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        pressure = capabilities.get("resource_pressure")
        if (
            not isinstance(pressure, dict)
            or not pressure.get("constrained")
        ):
            continue
        reasons = pressure.get("reasons")
        constrained.append(
            {
                "server_id": server.id,
                "level": str(
                    pressure.get("level") or "constrained"
                ),
                "reasons": (
                    [str(item) for item in reasons]
                    if isinstance(reasons, list)
                    else []
                ),
            }
        )
    return constrained


def _nonnegative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


def workers_with_event_spool_backlog(
    session: Session,
    server_ids: set[str],
) -> list[dict[str, Any]]:
    if not server_ids:
        return []
    servers = session.execute(
        select(Server)
        .where(Server.id.in_(server_ids))
        .where(Server.archived_at.is_(None))
        .order_by(Server.id.asc())
    ).scalars().all()
    workers: list[dict[str, Any]] = []
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        spool = capabilities.get("event_spool")
        if not isinstance(spool, dict):
            continue
        pending_events = _nonnegative_int(spool.get("pending_events"))
        pending_logs = _nonnegative_int(spool.get("pending_logs"))
        failed_events = _nonnegative_int(spool.get("failed_events"))
        failed_logs = _nonnegative_int(spool.get("failed_logs"))
        dropped_events = _nonnegative_int(spool.get("dropped_events"))
        dropped_logs = _nonnegative_int(spool.get("dropped_logs"))
        total_backlog = (
            pending_events
            + pending_logs
            + failed_events
            + failed_logs
            + dropped_events
            + dropped_logs
        )
        if total_backlog <= 0:
            continue
        workers.append(
            {
                "server_id": server.id,
                "dir": str(spool.get("dir") or ""),
                "pending_events": pending_events,
                "pending_logs": pending_logs,
                "failed_events": failed_events,
                "failed_logs": failed_logs,
                "dropped_events": dropped_events,
                "dropped_logs": dropped_logs,
                "total_backlog": total_backlog,
            }
        )
    return workers


def workers_with_pending_shard_update_backlog(
    session: Session,
    server_ids: set[str],
) -> list[dict[str, Any]]:
    if not server_ids:
        return []
    servers = session.execute(
        select(Server)
        .where(Server.id.in_(server_ids))
        .where(Server.archived_at.is_(None))
        .order_by(Server.id.asc())
    ).scalars().all()
    workers: list[dict[str, Any]] = []
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        pending_updates = capabilities.get("pending_shard_updates")
        if not isinstance(pending_updates, dict):
            continue
        pending = _nonnegative_int(pending_updates.get("pending"))
        failed = _nonnegative_int(pending_updates.get("failed"))
        total_backlog = pending + failed
        if total_backlog <= 0:
            continue
        workers.append(
            {
                "server_id": server.id,
                "pending": pending,
                "failed": failed,
                "total_backlog": total_backlog,
            }
        )
    return workers


__all__ = [
    "count_active_jobs_for_server",
    "count_open_jobs_for_server",
    "count_running_shards_for_server",
    "job_worker_server_ids",
    "job_worker_version_summary",
    "list_servers",
    "resource_constrained_workers",
    "server_versions",
    "workers_with_event_spool_backlog",
    "workers_with_pending_shard_update_backlog",
]
