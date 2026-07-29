"""Compatibility façade for the pre-PR7c worker core.

Actual worker behavior lives in explicit owned modules.  These redirects
remain until the v0.4 PR8 façade removal.
"""

from __future__ import annotations

import json
import math
import os
import posixpath
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ocr_parser.config import ParserConfig
from ocr_parser.infra.failure_category import infer_failure_category
from ocr_platform.manifest.models import ManifestItem
from ocr_platform.manifest.scanner import scan_folder_snapshot
from ocr_platform.manifest.sharder import write_manifest_snapshot
from sqlalchemy import Integer, case, delete, distinct, func, select, update
from sqlalchemy.orm import Session

import ocr_platform.engine_provenance as __engine_provenance

from ... import certification_gate as __certification_gate
from ... import database
from ... import settings as __control_settings
from ...limits import ControlLimits as __ControlLimits
from ...limits import legacy_control_limits as __legacy_control_limits
from ...models import (
    Job,
    JobCounter,
    JobEvent,
    JobFile,
    JobLog,
    Manifest,
    ModelProfile,
    ScanUnit,
    Server,
    WorkShard,
)
from ...schemas import (
    JobCreateRequest,
    JobEventRequest,
    JobLogListResponse,
    JobLogRequest,
    JobLogResponse,
    JobPreflightIssue,
    JobPreflightResponse,
    JobRecentErrorListResponse,
    JobRecentErrorResponse,
    JobShardProgressSummary,
    JobSummaryListResponse,
    JobSummaryResponse,
    JobWorkerShardSummary,
    ManifestFreezeReportResponse,
    ManifestIntegrityResponse,
    ManifestIntegrityScanUnitIssue,
    ManifestIntegrityShardIssue,
    ManifestIntegrityWorkerCompleteRequest,
    ManifestIntegrityWorkerRequestResponse,
    ManifestIntegrityWorkerShardTask,
    ManifestIntegrityWorkerTask,
    ModelProfileRequest,
    ModelProfileResponse,
    RemoteManifestRegisterRequest,
    ScanUnitCompleteRequest,
    ScanUnitFailRequest,
    ServerHeartbeatRequest,
    ServerRegisterRequest,
    ShardAttemptListResponse,
    ShardAttemptResponse,
    WorkShardUpdateRequest,
)
from ..common import *
from . import assignment as __assignment
from . import commands as __commands
from . import eligibility as __eligibility
from . import identity as __identity
from . import preflight as __preflight
from . import projection as __projection
from . import registration as __registration


def _resolve_model_profile_api_key(*args, **kwargs):
    from ..model_profiles.queries import (
        resolve_model_profile_api_key as target,
    )

    return target(*args, **kwargs)


def ensure_default_model_profiles(*args, **kwargs):
    from ...bootstrap import seed_default_model_profiles as target

    return target(*args, **kwargs)


def infer_default_manifest_root(*args, **kwargs):
    from ..manifests.paths import infer_default_manifest_root as target

    return target(*args, **kwargs)


def stop_reclaimable_work_for_job(*args, **kwargs):
    from ...scheduling import stop_reclaimable_work_for_job as target

    return target(*args, **kwargs)


allowed_server_ids_for_job = __identity.allowed_server_ids_for_job
server_is_allowed_for_job = __identity.server_is_allowed_for_job
public_assigned_server_id = __identity.public_assigned_server_id
is_server_stale = __identity.is_server_stale
effective_server_status = __identity.effective_server_status

register_server = __commands.register_server
heartbeat_server = __commands.heartbeat_server
claim_next_job = __commands.claim_next_job
claim_next_pool_job = __commands.claim_next_pool_job
archive_server = __commands.archive_server
ensure_pool_server = __registration.ensure_pool_server

count_active_jobs_for_server = (
    __projection.count_active_jobs_for_server
)
count_open_jobs_for_server = __projection.count_open_jobs_for_server
count_running_shards_for_server = (
    __projection.count_running_shards_for_server
)
list_servers = __projection.list_servers
_server_versions = __projection.server_versions
_job_worker_server_ids = __projection.job_worker_server_ids
_job_worker_version_summary = (
    __projection.job_worker_version_summary
)
_resource_constrained_workers = (
    __projection.resource_constrained_workers
)
_nonnegative_int = __projection._nonnegative_int
_workers_with_event_spool_backlog = (
    __projection.workers_with_event_spool_backlog
)
_workers_with_pending_shard_update_backlog = (
    __projection.workers_with_pending_shard_update_backlog
)

_normal_posix_path = __eligibility._normal_posix_path
_path_is_under = __eligibility._path_is_under
evaluate_server_path_access = (
    __eligibility.evaluate_server_path_access
)
server_can_access_input_dir = (
    __eligibility.server_can_access_input_dir
)
__candidate_workers_for_job = (
    __eligibility.candidate_workers_for_job
)
list_server_eligibility = __eligibility.list_server_eligibility

_preflight_issue = __preflight.preflight_issue
_database_migration_preflight_issue = (
    __preflight.database_migration_preflight_issue
)
_control_api_auth_preflight_issue = (
    __preflight.control_api_auth_preflight_issue
)


def preflight_job(
    session: Session,
    request: JobCreateRequest,
    *,
    settings: __control_settings.ControlSettings | None = None,
    limits: __ControlLimits | None = None,
) -> JobPreflightResponse:
    return __preflight.preflight_job(
        session,
        request,
        settings=settings,
        limits=(
            limits
            if limits is not None
            else __legacy_control_limits()
        ),
    )


def _fence_running_work_for_restarted_server(*args, **kwargs):
    from ...scheduling import (
        _fence_running_work_for_restarted_server as target,
    )

    return target(*args, **kwargs)


def shard_lease_deadline(now: datetime | None = None) -> datetime:
    from ...scheduling import shard_lease_deadline as target

    return target(now)


def scan_unit_lease_deadline(
    now: datetime | None = None,
) -> datetime:
    from ...scheduling import scan_unit_lease_deadline as target

    return target(now)


def _expired_running_shard_filter(*args, **kwargs):
    from ...scheduling import _expired_running_shard_filter as target

    return target(*args, **kwargs)


def _lock_job_for_shard_change(*args, **kwargs):
    from ...scheduling import _lock_job_for_shard_change as target

    return target(*args, **kwargs)


def _finalize_job_after_shard_change(*args, **kwargs):
    from ...scheduling import _finalize_job_after_shard_change as target

    return target(*args, **kwargs)


def _reconcile_expired_shard_leases(*args, **kwargs):
    from ...scheduling import _reconcile_expired_shard_leases as target

    return target(*args, **kwargs)


def reconcile_expired_shard_leases(
    session: Session,
    *,
    now: datetime | None = None,
    job_id: str | None = None,
) -> None:
    from ...scheduling import reconcile_expired_shard_leases as target

    return target(session, now=now, job_id=job_id)


def reconcile_expired_scan_unit_leases(
    session: Session,
    *,
    now: datetime | None = None,
    job_id: str | None = None,
) -> None:
    from ...scheduling import (
        reconcile_expired_scan_unit_leases as target,
    )

    return target(session, now=now, job_id=job_id)


def _remaining_retry_status(job: Job, shard: WorkShard) -> str:
    from ...scheduling import _remaining_retry_status as target

    return target(job, shard)


def renew_running_shard_leases(
    session: Session,
    server_id: str,
    *,
    job_id: str,
    now: datetime,
) -> None:
    from ...scheduling import renew_running_shard_leases as target

    return target(
        session,
        server_id,
        job_id=job_id,
        now=now,
    )


def renew_running_scan_unit_leases(
    session: Session,
    server_id: str,
    *,
    job_id: str,
    now: datetime,
) -> None:
    from ...scheduling import (
        renew_running_scan_unit_leases as target,
    )

    return target(
        session,
        server_id,
        job_id=job_id,
        now=now,
    )


def stop_assigned_queued_jobs_for_server(*args, **kwargs):
    from ..jobs.lifecycle import (
        stop_assigned_queued_jobs_for_server as target,
    )

    return target(*args, **kwargs)


def _pool_job_has_claimable_shards(*args, **kwargs):
    return __assignment.pool_job_has_claimable_shards(
        *args,
        **kwargs,
    )


__all__ = [
    name
    for name in globals()
    if not name.startswith("__")
    and name
    not in {
        "_SHARD_LEASE_ATTEMPTS_EXHAUSTED_ERROR",
        "_ShardLeaseReconcileResult",
        "_deterministic_failed_shard",
        "_finalize_job_after_shard_change",
        "_latest_current_shard_attempt",
        "_lock_job_for_shard_change",
        "_reconcile_expired_shard_leases",
        "dataclass",
    }
]
