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

from ... import database
from ...limits import ControlLimits as __ControlLimits
from ...limits import legacy_control_limits as __legacy_control_limits
from ...models import Job, JobCounter, JobEvent, JobFile, JobLog, Manifest, ModelProfile, ScanUnit, Server, ShardAttempt, WorkShard
from ...schemas import (
    JobCreateRequest, JobEventRequest, JobLogListResponse, JobLogRequest, JobLogResponse,
    ManifestFreezeReportResponse, ManifestIntegrityResponse, ManifestIntegrityWorkerCompleteRequest,
    ManifestIntegrityWorkerRequestResponse, ManifestIntegrityWorkerTask, ManifestIntegrityWorkerShardTask,
    ManifestIntegrityScanUnitIssue, ManifestIntegrityShardIssue, JobPreflightIssue, JobPreflightResponse,
    JobRecentErrorListResponse, JobRecentErrorResponse, JobSummaryListResponse, JobShardProgressSummary,
    JobSummaryResponse, JobWorkerShardSummary, ModelProfileRequest, ModelProfileResponse,
    ScanUnitCompleteRequest, ScanUnitFailRequest, ServerHeartbeatRequest, ServerRegisterRequest,
    ShardAttemptListResponse, WorkShardUpdateRequest, RemoteManifestRegisterRequest, ShardAttemptResponse,
)
from ..common import *
from .integrity import (
    DuplicateManifestRelativePathError,
    InvalidManifestRelativePathError,
    InvalidManifestRowError,
)

def _latest_manifest_scan_progress(*args, **kwargs):
    from .projection import latest_manifest_scan_progress as target

    return target(*args, **kwargs)

def _manifest_scan_error_samples(*args, **kwargs):
    from .projection import manifest_scan_error_samples as target

    return target(*args, **kwargs)

def _manifest_scan_metadata(*args, **kwargs):
    from .projection import manifest_scan_metadata as target

    return target(*args, **kwargs)

def _normal_posix_path(*args, **kwargs):
    from .paths import normal_posix_path as target

    return target(*args, **kwargs)

def _path_is_under(*args, **kwargs):
    from .paths import path_is_under as target

    return target(*args, **kwargs)

def _recent_manifest_scan_error_samples(*args, **kwargs):
    from .projection import recent_manifest_scan_error_samples as target

    return target(*args, **kwargs)

def _remaining_retry_status(*args, **kwargs):
    from ...scheduling import _remaining_retry_status as target

    return target(*args, **kwargs)

def _scan_unit_problem_samples(*args, **kwargs):
    from .projection import scan_unit_problem_samples as target

    return target(*args, **kwargs)

def evaluate_server_path_access(*args, **kwargs):
    from .paths import evaluate_server_path_access as target

    return target(*args, **kwargs)

def get_job_or_raise(*args, **kwargs):
    from .projection import get_job_or_raise as target

    return target(*args, **kwargs)

def list_servers(*args, **kwargs):
    from .paths import list_servers as target

    return target(*args, **kwargs)

def reconcile_expired_scan_unit_leases(*args, **kwargs):
    from ...scheduling import reconcile_expired_scan_unit_leases as target

    return target(*args, **kwargs)

def reconcile_expired_shard_leases(*args, **kwargs):
    from ...scheduling import reconcile_expired_shard_leases as target

    return target(*args, **kwargs)

def _reconcile_expired_shard_leases(*args, **kwargs):
    from ...scheduling import _reconcile_expired_shard_leases as target

    return target(*args, **kwargs)

def _lock_job_for_shard_change(*args, **kwargs):
    from ...scheduling import _lock_job_for_shard_change as target

    return target(*args, **kwargs)

def _finalize_job_after_shard_change(*args, **kwargs):
    from ...scheduling import _finalize_job_after_shard_change as target

    return target(*args, **kwargs)

def scan_unit_lease_deadline(*args, **kwargs):
    from ...scheduling import scan_unit_lease_deadline as target

    return target(*args, **kwargs)

def server_is_allowed_for_job(*args, **kwargs):
    from .paths import server_is_allowed_for_job as target

    return target(*args, **kwargs)

def shard_lease_deadline(*args, **kwargs):
    from ...scheduling import shard_lease_deadline as target

    return target(*args, **kwargs)

def default_manifest_root_for_shared_path(*args, **kwargs):
    from .paths import default_manifest_root_for_shared_path as target

    return target(*args, **kwargs)

def infer_default_manifest_root(*args, **kwargs):
    from .paths import infer_default_manifest_root as target

    return target(*args, **kwargs)

def server_can_access_input_dir(*args, **kwargs):
    from .paths import server_can_access_input_dir as target

    return target(*args, **kwargs)

def _manifest_output_dir(*args, **kwargs):
    from .paths import manifest_output_dir as target

    return target(*args, **kwargs)

def _manifest_output_dir_for_job(*args, **kwargs):
    from .paths import manifest_output_dir_for_job as target

    return target(*args, **kwargs)

def _read_manifest_items(*args, **kwargs):
    from .construction import read_manifest_items as target

    return target(*args, **kwargs)

def _create_static_shards_for_job(*args, **kwargs):
    from .construction import create_static_shards_for_job as target

    return target(*args, **kwargs)

def _create_distributed_scan_for_job(*args, **kwargs):
    from .construction import create_distributed_scan_for_job as target

    return target(*args, **kwargs)

def register_remote_manifest(*args, **kwargs):
    from .commands import register_remote_manifest as target

    return target(*args, **kwargs)

def claim_next_scan_unit(*args, **kwargs):
    from .commands import claim_next_scan_unit as target

    return target(*args, **kwargs)

def _claim_next_scan_unit_phase(*args, **kwargs):
    from .use_cases import claim_next_scan_unit_phase as target

    return target(*args, **kwargs)

def _next_manifest_shard_index(*args, **kwargs):
    from .construction import next_manifest_shard_index as target

    return target(*args, **kwargs)

def _manifest_for_scan_unit_completion_select(*args, **kwargs):
    from .construction import manifest_for_scan_unit_completion_select as target

    return target(*args, **kwargs)

def _existing_scan_unit_paths(*args, **kwargs):
    from .construction import existing_scan_unit_paths as target

    return target(*args, **kwargs)

def complete_scan_unit(*args, **kwargs):
    from .commands import complete_scan_unit as target

    return target(*args, **kwargs)

def _complete_scan_unit(*args, **kwargs):
    from .use_cases import complete_scan_unit as target

    return target(*args, **kwargs)

def fail_scan_unit(*args, **kwargs):
    from .commands import fail_scan_unit as target

    return target(*args, **kwargs)

def _fail_scan_unit(*args, **kwargs):
    from .use_cases import fail_scan_unit as target

    return target(*args, **kwargs)

def _normalized_shard_status_filter(*args, **kwargs):
    from .projection import normalize_shard_status_filter as target

    return target(*args, **kwargs)

def _validate_manifest_relative_path_shape(*args, **kwargs):
    from .integrity import _validate_manifest_relative_path_shape as target

    return target(*args, **kwargs)

def _count_jsonl_rows_with_relative_paths(*args, **kwargs):
    from .integrity import _count_jsonl_rows_with_relative_paths as target

    return target(*args, **kwargs)

def _count_jsonl_rows(*args, **kwargs):
    from .integrity import _count_jsonl_rows as target

    return target(*args, **kwargs)

def _validate_json_file(*args, **kwargs):
    from .integrity import _validate_json_file as target

    return target(*args, **kwargs)

def _append_manifest_integrity_issue_sample(*args, **kwargs):
    from .integrity import _append_manifest_integrity_issue_sample as target

    return target(*args, **kwargs)

def _scan_unit_status_counts(*args, **kwargs):
    from .freeze import _scan_unit_status_counts as target

    return target(*args, **kwargs)

def _shard_status_counts(*args, **kwargs):
    from .freeze import _shard_status_counts as target

    return target(*args, **kwargs)

def _manifest_integrity_issue_samples(*args, **kwargs):
    from .integrity import _manifest_integrity_issue_samples as target

    return target(*args, **kwargs)

def _manifest_integrity_freeze_summary(*args, **kwargs):
    from .integrity import manifest_integrity_freeze_summary as target

    return target(*args, **kwargs)

def _build_manifest_freeze_report(*args, **kwargs):
    from .freeze import build_manifest_freeze_report as target

    return target(*args, **kwargs)

def freeze_manifest_if_scan_complete(*args, **kwargs):
    from .freeze import freeze_manifest_if_scan_complete as target

    return target(*args, **kwargs)

def get_manifest_freeze_report(*args, **kwargs):
    from .freeze import get_manifest_freeze_report as target

    return target(*args, **kwargs)

def path_is_under_worker_shared_root(*args, **kwargs):
    from .integrity import path_is_under_worker_shared_root as target

    return target(*args, **kwargs)

def _server_can_read_path(*args, **kwargs):
    from .integrity import _server_can_read_path as target

    return target(*args, **kwargs)

def __bounded_manifest_integrity_issues(*args, **kwargs):
    from .integrity import bounded_manifest_integrity_issues as target

    return target(*args, **kwargs)

def _manifest_worker_report_payload(*args, **kwargs):
    from .integrity import _manifest_worker_report_payload as target

    return target(*args, **kwargs)

def _load_worker_integrity_report(*args, **kwargs):
    from .integrity import load_worker_integrity_report as target

    return target(*args, **kwargs)

def request_worker_manifest_integrity_check(*args, **kwargs):
    from .commands import request_worker_manifest_integrity_check as target

    return target(*args, **kwargs)

def claim_worker_manifest_integrity_check(*args, **kwargs):
    from .commands import claim_worker_manifest_integrity_check as target

    return target(*args, **kwargs)

def complete_worker_manifest_integrity_check(*args, **kwargs):
    from .commands import complete_worker_manifest_integrity_check as target

    return target(*args, **kwargs)

def get_manifest_integrity_report(*args, **kwargs):
    from .integrity import get_manifest_integrity_report as target

    return target(*args, **kwargs)

def list_work_shards(*args, **kwargs):
    from .projection import list_work_shards as target

    return target(*args, **kwargs)

def has_static_shards(*args, **kwargs):
    from .projection import has_static_shards as target

    return target(*args, **kwargs)

def stop_reclaimable_work_for_job(*args, **kwargs):
    from ...scheduling import stop_reclaimable_work_for_job as target

    return target(*args, **kwargs)

def finalize_stopped_job_if_idle(*args, **kwargs):
    from .use_cases import finalize_stopped_job_if_idle as target

    return target(*args, **kwargs)

def _claimable_shard_id_select(*args, **kwargs):
    from ...scheduling import _claimable_shard_id_select as target

    return target(*args, **kwargs)

def _latest_shard_attempt(*args, **kwargs):
    from ...scheduling import _latest_current_shard_attempt as target

    return target(*args, **kwargs)

def claim_next_pending_shard(*args, **kwargs):
    from .commands import claim_next_pending_shard as target

    return target(*args, **kwargs)

def _claim_next_pending_shard(*args, **kwargs):
    from .use_cases import claim_next_pending_shard as target

    return target(*args, **kwargs)

def update_work_shard(*args, **kwargs):
    from .commands import update_work_shard as target

    return target(*args, **kwargs)

def list_shard_attempts(*args, **kwargs):
    from .projection import list_shard_attempts as target

    return target(*args, **kwargs)

def list_shard_attempts_page(*args, **kwargs):
    from .projection import list_shard_attempts_page as target

    return target(*args, **kwargs)

def shard_attempt_to_response(*args, **kwargs):
    from .projection import shard_attempt_to_response as target

    return target(*args, **kwargs)


def _claimable_scan_unit_id_select(
    *,
    limit: int = SCAN_UNIT_CLAIM_BATCH_SIZE,
    after_id: int | None = None,
    statuses: set[str] | None = None,
):
    from ...scheduling import _claimable_scan_unit_id_select as target

    return target(
        limit=limit,
        after_id=after_id,
        statuses=statuses or RECLAIMABLE_SCAN_UNIT_STATUSES,
    )


__all__ = [
    name
    for name in globals()
    if not name.startswith("__")
    and name not in {
        "_claim_next_scan_unit_phase",
        "_claim_next_pending_shard",
        "_complete_scan_unit",
        "_fail_scan_unit",
        "_finalize_job_after_shard_change",
        "_lock_job_for_shard_change",
        "_reconcile_expired_shard_leases",
    }
]
