from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ServerRegisterRequest(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    host: str = Field(min_length=1)
    capacity_slots: int = 1
    capabilities: dict[str, Any] = Field(default_factory=dict)


class ServerHeartbeatRequest(BaseModel):
    status: str = Field(default="idle", min_length=1)
    current_job_id: Optional[str] = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


class JobCreateRequest(BaseModel):
    input_dir: str
    output_dir: str
    engine: str
    model_profile_id: Optional[str] = None
    assigned_server_id: Optional[str] = None
    allowed_server_ids: list[str] = Field(default_factory=list)
    input_mode: str = "directory"
    manifest_path: Optional[str] = None
    manifest_root: Optional[str] = None
    target_files_per_shard: int = 1000
    max_shard_attempts: int = Field(default=3, ge=1)
    engine_config: Optional[str] = None
    ip: Optional[str] = None
    port: Optional[int] = None
    model_name: Optional[str] = None
    page_concurrency: Optional[int] = None
    force_reprocess: bool = False
    extra_args: dict[str, Any] = Field(default_factory=dict)


class ModelProfileRequest(BaseModel):
    label: str = Field(min_length=1)
    engine: str = Field(min_length=1)
    ip: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    model_name: Optional[str] = None
    page_concurrency: Optional[int] = Field(default=None, ge=1)
    extra_args: dict[str, Any] = Field(default_factory=dict)
    requires_api_key: bool = False
    is_default: bool = False
    api_key: Optional[str] = None
    api_key_env_var: Optional[str] = None
    clear_api_key: bool = False


class ModelProfileResponse(BaseModel):
    id: str
    label: str
    engine: str
    ip: Optional[str]
    port: Optional[int]
    model_name: Optional[str]
    page_concurrency: Optional[int]
    extra_args: dict[str, Any] = Field(default_factory=dict)
    requires_api_key: bool
    has_api_key: bool
    api_key_env_var: Optional[str] = None
    is_default: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class JobEventRequest(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class JobLogRequest(BaseModel):
    server_id: str
    stream: str
    line: str


class JobLogResponse(BaseModel):
    id: int
    job_id: str
    server_id: str
    stream: str
    line: str
    created_at: datetime


class JobLogListResponse(BaseModel):
    job_id: str
    total: int
    limit: int
    offset: int
    has_more: bool
    items: list[JobLogResponse] = Field(default_factory=list)


class JobRecentErrorResponse(BaseModel):
    source: str
    event_type: Optional[str] = None
    file_path: Optional[str] = None
    filename: Optional[str] = None
    failure_category: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class JobRecentErrorListResponse(BaseModel):
    job_id: str
    total: int
    limit: int
    offset: int
    has_more: bool
    items: list[JobRecentErrorResponse] = Field(default_factory=list)


class WorkShardUpdateRequest(BaseModel):
    status: Literal["pending", "running", "retrying", "stale", "succeeded", "failed", "stopped"]
    assigned_server_id: Optional[str] = None
    attempt_count: Optional[int] = Field(default=None, ge=0)
    processed_files: int = Field(default=0, ge=0)
    failed_files: int = Field(default=0, ge=0)
    skipped_files: int = Field(default=0, ge=0)
    completed_pages: int = Field(default=0, ge=0)
    api_inflight: Optional[int] = Field(default=None, ge=0)
    api_inflight_peak: Optional[int] = Field(default=None, ge=0)
    api_waiting: Optional[int] = Field(default=None, ge=0)
    oldest_api_inflight: Optional[float] = Field(default=None, ge=0)
    execution_paused: Optional[bool] = None
    api_concurrency_limit: Optional[int] = Field(default=None, ge=1)
    execution_control_reason: Optional[str] = None
    failure_category: Optional[str] = None
    error_message: Optional[str] = None


class ServerResponse(BaseModel):
    id: str
    name: str
    host: str
    status: str
    capacity_slots: int
    capabilities: dict[str, Any]
    last_heartbeat_at: Optional[datetime] = None
    is_stale: bool = False
    active_jobs: int = 0
    running_shards: int = 0


class RemoteWorkerBaseRequest(BaseModel):
    host: str = Field(min_length=1)
    ssh_user: Optional[str] = None
    connect_timeout_seconds: int = Field(default=6, ge=1, le=60)
    timeout_seconds: int = Field(default=60, ge=1, le=600)

    def ssh_target(self) -> str:
        user = (self.ssh_user or "").strip()
        return f"{user}@{self.host}" if user else self.host


class RemoteWorkerPreflightRequest(RemoteWorkerBaseRequest):
    service_user: str = "ocr-agent"
    service_group: str = "ocr-agent"
    repo_dir: str = "/opt/ocr-platform/ocrparser"
    shared_roots: list[str] = Field(default_factory=lambda: ["/shared/ocr-data"])


class RemoteWorkerInstallDryRunRequest(RemoteWorkerPreflightRequest):
    server_id: str = Field(min_length=1)
    control_url: str = Field(min_length=1)
    runner: str = "tmux"


class RemoteWorkerScaleRequest(RemoteWorkerPreflightRequest):
    target_count: int = Field(default=1, ge=1, le=16)
    seed_server_id: Optional[str] = None
    server_id_prefix: str = Field(default="ocr-worker", min_length=1, max_length=64)


class RemoteWorkerServiceRequest(RemoteWorkerBaseRequest):
    action: Literal["start", "stop", "restart", "disable"]
    service_name: str = "ocr-agent-worker.service"


class RemoteWorkerOperationResponse(BaseModel):
    ok: bool
    action: str
    host: str
    command: list[str]
    return_code: int
    stdout: str
    stderr: str


class RemoteWorkerScalePlanItem(BaseModel):
    action: Literal[
        "create_env",
        "update_env",
        "start_service",
        "stop_service",
        "disable_service",
        "wait_heartbeat",
        "skip",
    ]
    status: Literal["pending", "ok", "warning", "failed", "skipped"]
    instance: Optional[str] = None
    server_id: Optional[str] = None
    message: str = ""


class RemoteWorkerScaleResponse(RemoteWorkerOperationResponse):
    plan_items: list[RemoteWorkerScalePlanItem] = Field(default_factory=list)


class RemoteWorkerTargetResponse(BaseModel):
    id: str
    host: str
    hostname: str
    ssh_user: str
    server_id: str
    service_user: str
    service_group: str
    repo_dir: str
    control_url: str = ""
    shared_roots: list[str] = Field(default_factory=list)


class RemoteWorkerTargetListResponse(BaseModel):
    targets: list[RemoteWorkerTargetResponse] = Field(default_factory=list)


class ServerEligibilityItem(BaseModel):
    server_id: str
    name: str
    host: str
    status: str
    is_stale: bool
    can_access: bool
    matched_path: Optional[str] = None
    reason: str


class ServerEligibilityResponse(BaseModel):
    input_dir: str
    total_servers: int
    eligible_servers: int
    servers: list[ServerEligibilityItem] = Field(default_factory=list)


class JobFileResponse(BaseModel):
    file_path: str
    filename: str
    status: str
    total_pages: Optional[int]
    done_pages: int
    output_path: Optional[str]
    error: Optional[str]
    failure_category: Optional[str] = None


class JobShardProgressSummary(BaseModel):
    id: int
    shard_index: int
    status: str
    assigned_server_id: Optional[str]
    started_at: Optional[datetime] = None
    running_seconds: Optional[float] = None
    file_count: int
    processed_files: int
    failed_files: int
    skipped_files: int
    completed_pages: int
    api_inflight: int = 0
    api_inflight_peak: int = 0
    api_waiting: int = 0
    oldest_api_inflight: float = 0.0
    execution_paused: bool = False
    api_concurrency_limit: Optional[int] = None
    execution_control_reason: Optional[str] = None
    pages_per_second: Optional[float]
    files_per_minute: Optional[float]
    attempt_count: int = 0
    max_attempts: int = 0
    lease_expires_at: Optional[datetime] = None
    lease_seconds_remaining: Optional[int] = None
    lease_status: str = "none"
    failure_category: Optional[str] = None
    error_message: Optional[str] = None


class JobWorkerShardSummary(BaseModel):
    server_id: Optional[str]
    total_shards: int = 0
    pending_shards: int = 0
    running_shards: int = 0
    retrying_shards: int = 0
    stale_shards: int = 0
    succeeded_shards: int = 0
    failed_shards: int = 0
    stopped_shards: int = 0
    current_shards: list[JobShardProgressSummary] = Field(default_factory=list)
    api_inflight: int = 0
    api_inflight_peak: int = 0
    api_waiting: int = 0
    oldest_api_inflight: float = 0.0
    execution_paused: bool = False


class JobSummaryResponse(BaseModel):
    id: str
    input_dir: str
    output_dir: str
    engine: str
    assigned_server_id: Optional[str]
    allowed_server_ids: list[str] = Field(default_factory=list)
    status: str
    lifecycle_stage: str = "queued"
    failure_category: Optional[str]
    error_message: Optional[str]
    stop_requested: bool
    force_reprocess: bool
    archived_at: Optional[datetime] = None
    total_files: int
    scanned_files: int = 0
    completed_files: int
    failed_files: int
    failure_category_counts: dict[str, int] = Field(default_factory=dict)
    skipped_files: int
    total_pages: Optional[int]
    completed_pages: int
    progress_percent: Optional[float]
    pages_per_second: Optional[float]
    files_per_minute: Optional[float]
    eta_seconds: Optional[int]
    last_event_at: Optional[datetime]
    last_heartbeat_at: Optional[datetime]
    is_stale: bool
    degraded_pages: int
    manifest_status: Optional[str] = None
    manifest_snapshot_status: str = "missing"
    manifest_frozen_at: Optional[datetime] = None
    manifest_integrity_status: Optional[str] = None
    manifest_integrity_ok: Optional[bool] = None
    manifest_integrity_issue_count: int = 0
    scan_status: str = "not_started"
    scan_progress_files: int = 0
    scan_discovered_pdf_count: int = 0
    scan_estimated_total_files: Optional[int] = None
    scan_estimated_total_pdf_count: Optional[int] = None
    scan_remaining_files: Optional[int] = None
    scan_remaining_pdf_count: Optional[int] = None
    scan_progress_percent: Optional[float] = None
    scan_progress_dirs: int = 0
    scan_progress_bytes: int = 0
    scan_current_path: Optional[str] = None
    scan_error_count: int = 0
    scan_error_samples: list[dict[str, Any]] = Field(default_factory=list)
    scan_eta_seconds: Optional[int] = None
    scan_started_at: Optional[datetime] = None
    scan_finished_at: Optional[datetime] = None
    total_shards: int = 0
    shards_created: int = 0
    executable_shards: int = 0
    pending_shards: int = 0
    running_shards: int = 0
    retrying_shards: int = 0
    stale_shards: int = 0
    succeeded_shards: int = 0
    failed_shards: int = 0
    stopped_shards: int = 0
    shard_failure_category_counts: dict[str, int] = Field(default_factory=dict)
    total_scan_units: int = 0
    pending_scan_units: int = 0
    running_scan_units: int = 0
    stale_scan_units: int = 0
    succeeded_scan_units: int = 0
    failed_scan_units: int = 0
    scan_unit_failure_category_counts: dict[str, int] = Field(default_factory=dict)
    recovery_status: str = "healthy"
    worker_version_status: str = "unknown"
    worker_version_warning: Optional[str] = None
    worker_version_refs: dict[str, list[str]] = Field(default_factory=dict)
    worker_shards: list[JobWorkerShardSummary] = Field(default_factory=list)
    attention_shards: list[JobShardProgressSummary] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)


class JobSummaryListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    has_more: bool
    items: list[JobSummaryResponse] = Field(default_factory=list)


class JobPreflightIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class JobPreflightResponse(BaseModel):
    ok: bool
    database_dialect: str
    total_workers: int = 0
    eligible_workers: int = 0
    ready_workers: int = 0
    issues: list[JobPreflightIssue] = Field(default_factory=list)


class SchemaMigrationResponse(BaseModel):
    version: str
    applied_at: Optional[str] = None


class DatabaseStatusResponse(BaseModel):
    dialect: str
    schema_migrations_table_exists: bool
    known_migrations: list[str] = Field(default_factory=list)
    applied_migrations: list[SchemaMigrationResponse] = Field(default_factory=list)
    latest_applied_migration: Optional[str] = None
    missing_migrations: list[str] = Field(default_factory=list)
    is_current: bool = False


class JobResponse(BaseModel):
    id: str
    input_dir: str
    output_dir: str
    engine: str
    model_profile_id: Optional[str] = None
    input_mode: str
    manifest_root: Optional[str]
    target_files_per_shard: int
    max_shard_attempts: int
    assigned_server_id: Optional[str]
    allowed_server_ids: list[str] = Field(default_factory=list)
    status: str
    failure_category: Optional[str]
    error_message: Optional[str]
    stop_requested: bool
    force_reprocess: bool
    archived_at: Optional[datetime] = None
    engine_config: Optional[str]
    ip: Optional[str]
    port: Optional[int]
    model_name: Optional[str]
    page_concurrency: Optional[int]
    has_static_shards: bool = False
    extra_args: dict[str, Any]
    command: list[str]
    files: list[JobFileResponse] = Field(default_factory=list)


class JobListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    has_more: bool
    items: list[JobResponse] = Field(default_factory=list)


class ManifestResponse(BaseModel):
    id: int
    job_id: str
    input_mode: str
    input_root: Optional[str]
    manifest_path: str
    meta_path: Optional[str]
    file_count: int
    total_bytes: int
    status: str


class ManifestIntegrityShardIssue(BaseModel):
    shard_id: int
    shard_index: int
    shard_path: str
    expected_file_count: int
    actual_file_count: Optional[int] = None
    reason: str


class ManifestIntegrityScanUnitIssue(BaseModel):
    scan_unit_id: int
    path: str
    manifest_path: Optional[str] = None
    expected_file_count: int
    actual_file_count: Optional[int] = None
    reason: str


class ManifestIntegrityResponse(BaseModel):
    job_id: str
    manifest_id: Optional[int] = None
    source: str = "control"
    checked_by_server_id: Optional[str] = None
    checked_at: Optional[datetime] = None
    worker_integrity_status: Optional[str] = None
    ok: bool
    status: str
    manifest_path: Optional[str] = None
    manifest_file_exists: bool = False
    manifest_expected_file_count: int = 0
    manifest_actual_file_count: Optional[int] = None
    manifest_file_count_matches: bool = False
    manifest_expected_total_bytes: int = 0
    manifest_actual_total_bytes: Optional[int] = None
    manifest_total_bytes_matches: bool = False
    manifest_error: Optional[str] = None
    meta_path: Optional[str] = None
    meta_file_exists: Optional[bool] = None
    meta_error: Optional[str] = None
    meta_expected_file_count: int = 0
    meta_actual_file_count: Optional[int] = None
    meta_file_count_matches: bool = False
    meta_expected_total_bytes: int = 0
    meta_actual_total_bytes: Optional[int] = None
    meta_total_bytes_matches: bool = False
    scan_unit_count: int = 0
    scan_unit_manifest_expected_file_count: int = 0
    scan_unit_manifest_actual_file_count: Optional[int] = None
    scan_unit_manifest_count_matches: bool = False
    scan_unit_manifest_expected_total_bytes: int = 0
    scan_unit_manifest_actual_total_bytes: Optional[int] = None
    scan_unit_manifest_total_bytes_matches: bool = False
    bad_scan_unit_count: int = 0
    bad_scan_units: list[ManifestIntegrityScanUnitIssue] = Field(default_factory=list)
    shard_count: int = 0
    shard_expected_file_count: int = 0
    shard_reference_file_count: int = 0
    shard_file_count_matches_manifest: bool = False
    bad_shard_count: int = 0
    bad_shards: list[ManifestIntegrityShardIssue] = Field(default_factory=list)


class ManifestIntegrityWorkerRequestResponse(BaseModel):
    job_id: str
    manifest_id: Optional[int] = None
    worker_integrity_status: str
    requested_at: Optional[datetime] = None


class ManifestIntegrityWorkerShardTask(BaseModel):
    shard_id: int
    shard_index: int
    shard_path: str
    expected_file_count: int


class ManifestIntegrityWorkerTask(BaseModel):
    job_id: str
    manifest_id: int
    manifest_path: str
    meta_path: Optional[str] = None
    manifest_expected_file_count: int
    manifest_expected_total_bytes: int
    shards: list[ManifestIntegrityWorkerShardTask] = Field(default_factory=list)


class ManifestIntegrityWorkerCompleteRequest(BaseModel):
    report: ManifestIntegrityResponse


class ManifestFreezeReportResponse(BaseModel):
    job_id: str
    manifest_id: Optional[int] = None
    status: str
    frozen_at: Optional[datetime] = None
    report: dict[str, Any] = Field(default_factory=dict)


class WorkShardResponse(BaseModel):
    id: int
    job_id: str
    manifest_id: int
    shard_index: int
    shard_path: str
    status: str
    assigned_server_id: Optional[str]
    file_count: int
    processed_files: int
    failed_files: int
    skipped_files: int
    completed_pages: int
    api_inflight: int = 0
    api_inflight_peak: int = 0
    api_waiting: int = 0
    oldest_api_inflight: float = 0.0
    execution_paused: bool = False
    api_concurrency_limit: Optional[int] = None
    execution_control_reason: Optional[str] = None
    failure_category: Optional[str]
    error_message: Optional[str]
    attempt_count: int = 0
    lease_expires_at: Optional[datetime] = None


class ShardAttemptResponse(BaseModel):
    id: int
    job_id: str
    shard_id: int
    attempt_number: int
    server_id: str
    status: str
    processed_files: int = 0
    failed_files: int = 0
    skipped_files: int = 0
    completed_pages: int = 0
    execution_paused: bool = False
    api_concurrency_limit: Optional[int] = None
    execution_control_reason: Optional[str] = None
    failure_category: Optional[str] = None
    error_message: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None


class ShardAttemptListResponse(BaseModel):
    job_id: str
    shard_id: int
    total: int
    limit: int
    offset: int
    has_more: bool
    items: list[ShardAttemptResponse] = Field(default_factory=list)


class WorkShardListResponse(BaseModel):
    job_id: str
    total: int
    limit: int
    offset: int
    has_more: bool
    items: list[WorkShardResponse] = Field(default_factory=list)


class RemoteManifestShardRequest(BaseModel):
    shard_index: int
    shard_path: str
    file_count: int


class RemoteManifestRegisterRequest(BaseModel):
    input_mode: str = "remote_folder_snapshot"
    input_root: str
    manifest_path: str
    meta_path: Optional[str] = None
    file_count: int = 0
    total_bytes: int = 0
    shards: list[RemoteManifestShardRequest] = Field(default_factory=list)


class ScanUnitResponse(BaseModel):
    id: int
    job_id: str
    path: str
    status: str
    assigned_server_id: Optional[str] = None
    attempt_count: int = 0
    lease_expires_at: Optional[datetime] = None
    file_count: int = 0
    total_bytes: int = 0
    failure_category: Optional[str] = None
    error_message: Optional[str] = None


class ScanUnitCompleteRequest(BaseModel):
    assigned_server_id: Optional[str] = None
    attempt_count: Optional[int] = Field(default=None, ge=0)
    manifest_path: Optional[str] = None
    meta_path: Optional[str] = None
    file_count: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)
    child_paths: list[str] = Field(default_factory=list)
    shards: list[RemoteManifestShardRequest] = Field(default_factory=list)


class ScanUnitFailRequest(BaseModel):
    assigned_server_id: Optional[str] = None
    attempt_count: Optional[int] = Field(default=None, ge=0)
    failure_category: Optional[str] = None
    error_message: str = Field(min_length=1)
