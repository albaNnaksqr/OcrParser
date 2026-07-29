"""Pure worker query surface."""

from .eligibility import (
    candidate_workers_for_job,
    evaluate_server_path_access,
    list_server_eligibility,
    server_can_access_input_dir,
)
from .identity import (
    allowed_server_ids_for_job,
    effective_server_status,
    is_server_stale,
    public_assigned_server_id,
    server_is_allowed_for_job,
)
from .projection import (
    count_active_jobs_for_server,
    count_open_jobs_for_server,
    count_running_shards_for_server,
    job_worker_server_ids,
    job_worker_version_summary,
    list_servers,
    resource_constrained_workers,
    server_versions,
    workers_with_event_spool_backlog,
    workers_with_pending_shard_update_backlog,
)

__all__ = [
    "allowed_server_ids_for_job",
    "candidate_workers_for_job",
    "count_active_jobs_for_server",
    "count_open_jobs_for_server",
    "count_running_shards_for_server",
    "effective_server_status",
    "evaluate_server_path_access",
    "is_server_stale",
    "job_worker_server_ids",
    "job_worker_version_summary",
    "list_server_eligibility",
    "list_servers",
    "public_assigned_server_id",
    "resource_constrained_workers",
    "server_can_access_input_dir",
    "server_is_allowed_for_job",
    "server_versions",
    "workers_with_event_spool_backlog",
    "workers_with_pending_shard_update_backlog",
]
