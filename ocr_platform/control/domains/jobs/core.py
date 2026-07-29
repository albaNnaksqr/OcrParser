from __future__ import annotations

import json
import math
import os
import posixpath
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ocr_parser.infra.failure_category import infer_failure_category
from ocr_parser.config import ParserConfig
from ocr_platform.manifest.models import ManifestItem
from ocr_platform.manifest.scanner import scan_folder_snapshot
from ocr_platform.manifest.sharder import write_manifest_snapshot
from sqlalchemy import Integer, case, delete, distinct, func, select, update
from sqlalchemy.orm import Session

from ... import database
from ... import certification_gate as __certification_gate
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
from ... import settings as __control_settings
from ...limits import ControlLimits as __ControlLimits
from ...limits import legacy_control_limits as __legacy_control_limits
from ..common import *

def _create_distributed_scan_for_job(*args, **kwargs):
    from .lifecycle import _create_distributed_scan_for_job as target
    return target(*args, **kwargs)

def _create_static_shards_for_job(*args, **kwargs):
    from .lifecycle import _create_static_shards_for_job as target
    return target(*args, **kwargs)

def _database_migration_preflight_issue(*args, **kwargs):
    from .lifecycle import _database_migration_preflight_issue as target
    return target(*args, **kwargs)

def _effective_job_model_config(*args, **kwargs):
    from .lifecycle import _effective_job_model_config as target
    return target(*args, **kwargs)

def _job_worker_version_summary(*args, **kwargs):
    from .projection import _job_worker_version_summary as target
    return target(*args, **kwargs)

def _load_worker_integrity_report(*args, **kwargs):
    from .projection import _load_worker_integrity_report as target
    return target(*args, **kwargs)

def _manifest_integrity_freeze_summary(*args, **kwargs):
    from .projection import _manifest_integrity_freeze_summary as target
    return target(*args, **kwargs)

def allowed_server_ids_for_job(*args, **kwargs):
    from .projection import allowed_server_ids_for_job as target
    return target(*args, **kwargs)

def __candidate_workers_for_job(*args, **kwargs):
    from .lifecycle import candidate_workers_for_job as target
    return target(*args, **kwargs)

def ensure_pool_server(*args, **kwargs):
    from .lifecycle import ensure_pool_server as target
    return target(*args, **kwargs)

def _lock_job_for_shard_change(*args, **kwargs):
    from ...scheduling import _lock_job_for_shard_change as target
    return target(*args, **kwargs)

def finalize_stopped_job_if_idle(*args, **kwargs):
    from .lifecycle import finalize_stopped_job_if_idle as target
    return target(*args, **kwargs)

def has_static_shards(*args, **kwargs):
    from .events import has_static_shards as target
    return target(*args, **kwargs)

def infer_default_manifest_root(*args, **kwargs):
    from .lifecycle import infer_default_manifest_root as target
    return target(*args, **kwargs)

def public_assigned_server_id(*args, **kwargs):
    from .projection import public_assigned_server_id as target
    return target(*args, **kwargs)

def reconcile_expired_scan_unit_leases(*args, **kwargs):
    from ...scheduling import reconcile_expired_scan_unit_leases as target
    return target(*args, **kwargs)

def reconcile_expired_shard_leases(*args, **kwargs):
    from ...scheduling import reconcile_expired_shard_leases as target
    return target(*args, **kwargs)

def stop_reclaimable_work_for_job(*args, **kwargs):
    from ...scheduling import stop_reclaimable_work_for_job as target
    return target(*args, **kwargs)

def create_job(*args, **kwargs):
    from .lifecycle import create as target
    return target(*args, **kwargs)

def _normalized_status_filter(*args, **kwargs):
    from .projection import normalize_status_filter as target
    return target(*args, **kwargs)

def list_job_summaries(*args, **kwargs):
    from .projection import list_job_summaries as target
    return target(*args, **kwargs)

def list_job_summaries_page(*args, **kwargs):
    from .projection import list_job_summaries_page as target
    return target(*args, **kwargs)

def _static_input_file_count(*args, **kwargs):
    from .projection import _static_input_file_count as target
    return target(*args, **kwargs)

def _latest_manifest_scan_progress(*args, **kwargs):
    from .projection import _latest_manifest_scan_progress as target
    return target(*args, **kwargs)

def _manifest_scan_metadata(*args, **kwargs):
    from .projection import _manifest_scan_metadata as target
    return target(*args, **kwargs)

def _manifest_scan_error_samples(*args, **kwargs):
    from .projection import _manifest_scan_error_samples as target
    return target(*args, **kwargs)

def _recent_manifest_scan_error_samples(*args, **kwargs):
    from .projection import _recent_manifest_scan_error_samples as target
    return target(*args, **kwargs)

def _scan_unit_problem_samples(*args, **kwargs):
    from .projection import _scan_unit_problem_samples as target
    return target(*args, **kwargs)

def _manifest_scan_started_at(*args, **kwargs):
    from .projection import _manifest_scan_started_at as target
    return target(*args, **kwargs)

def _parse_datetime(*args, **kwargs):
    from .projection import _parse_datetime as target
    return target(*args, **kwargs)

def _scan_eta_seconds(*args, **kwargs):
    from .projection import _scan_eta_seconds as target
    return target(*args, **kwargs)

def _scan_eta_seconds_from_rate(*args, **kwargs):
    from .projection import _scan_eta_seconds_from_rate as target
    return target(*args, **kwargs)

def _scan_unit_eta_seconds(*args, **kwargs):
    from .projection import _scan_unit_eta_seconds as target
    return target(*args, **kwargs)

def _shard_lease_status(*args, **kwargs):
    from .projection import _shard_lease_status as target
    return target(*args, **kwargs)

def _shard_progress_summary(*args, **kwargs):
    from .projection import _shard_progress_summary as target
    return target(*args, **kwargs)

def _job_lifecycle_stage(*args, **kwargs):
    from .projection import _job_lifecycle_stage as target
    return target(*args, **kwargs)

def _manifest_snapshot_status(*args, **kwargs):
    from .projection import _manifest_snapshot_status as target
    return target(*args, **kwargs)

def _manifest_freeze_integrity_summary(*args, **kwargs):
    from .projection import _manifest_freeze_integrity_summary as target
    return target(*args, **kwargs)

def get_job_summary(*args, **kwargs):
    from .projection import get_job_summary as target
    return target(*args, **kwargs)

def list_recent_job_files(*args, **kwargs):
    from .projection import list_recent_job_files as target
    return target(*args, **kwargs)

def _recent_error_from_event(*args, **kwargs):
    from .projection import _recent_error_from_event as target
    return target(*args, **kwargs)

def _recent_error_from_failed_file_sample(*args, **kwargs):
    from .projection import _recent_error_from_failed_file_sample as target
    return target(*args, **kwargs)

def _recent_error_from_event_sample(*args, **kwargs):
    from .projection import _recent_error_from_event_sample as target
    return target(*args, **kwargs)

def list_recent_job_errors_page(*args, **kwargs):
    from .projection import list_recent_job_errors_page as target
    return target(*args, **kwargs)

def request_stop(*args, **kwargs):
    from .lifecycle import request_stop as target
    return target(*args, **kwargs)

def delete_job(*args, **kwargs):
    from .lifecycle import delete as target
    return target(*args, **kwargs)

def archive_job(*args, **kwargs):
    from .lifecycle import archive as target
    return target(*args, **kwargs)

def get_job_or_raise(*args, **kwargs):
    from .lifecycle import get_or_raise as target
    return target(*args, **kwargs)

def parse_page_no(*args, **kwargs):
    from .counters import parse_page_no as target
    return target(*args, **kwargs)

def upsert_job_file_from_event(*args, **kwargs):
    from .counters import upsert_job_file_from_event as target
    return target(*args, **kwargs)

def _optional_int(*args, **kwargs):
    from .counters import _optional_int as target
    return target(*args, **kwargs)

def get_or_create_job_counter(*args, **kwargs):
    from .counters import get_or_create_job_counter as target
    return target(*args, **kwargs)

def _load_recent_failed_file_samples(*args, **kwargs):
    from .counters import _load_recent_failed_file_samples as target
    return target(*args, **kwargs)

def _load_recent_error_samples(*args, **kwargs):
    from .counters import _load_recent_error_samples as target
    return target(*args, **kwargs)

def _load_failure_category_counts(*args, **kwargs):
    from .counters import _load_failure_category_counts as target
    return target(*args, **kwargs)

def _increment_failure_category_count(*args, **kwargs):
    from .counters import _increment_failure_category_count as target
    return target(*args, **kwargs)

def _failed_file_sample_from_payload(*args, **kwargs):
    from .counters import _failed_file_sample_from_payload as target
    return target(*args, **kwargs)

def _store_recent_failed_file_sample(*args, **kwargs):
    from .counters import _store_recent_failed_file_sample as target
    return target(*args, **kwargs)

def _failure_event_sample_from_event(*args, **kwargs):
    from .counters import _failure_event_sample_from_event as target
    return target(*args, **kwargs)

def _store_recent_error_sample(*args, **kwargs):
    from .counters import _store_recent_error_sample as target
    return target(*args, **kwargs)

def job_counter_event_already_seen(*args, **kwargs):
    from .counters import job_counter_event_already_seen as target
    return target(*args, **kwargs)

def update_job_counter_from_event(*args, **kwargs):
    from .counters import update_job_counter_from_event as target
    return target(*args, **kwargs)

def _job_counter_total_files(*args, **kwargs):
    from .counters import _job_counter_total_files as target
    return target(*args, **kwargs)

def prune_job_detail_rows(*args, **kwargs):
    from .counters import prune_job_detail_rows as target
    return target(*args, **kwargs)

def record_event(*args, **kwargs):
    from .events import record_event as target
    return target(*args, **kwargs)

def record_log(*args, **kwargs):
    from .logs import record as target
    return target(*args, **kwargs)

def job_log_to_response(*args, **kwargs):
    from .logs import to_response as target
    return target(*args, **kwargs)

def list_job_logs_page(*args, **kwargs):
    from .logs import list_page as target
    return target(*args, **kwargs)

__all__ = [
    name
    for name in globals()
    if not name.startswith("__")
    and name != "_lock_job_for_shard_change"
]
