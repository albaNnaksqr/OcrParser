from __future__ import annotations

from ...models import ScanUnit, WorkShard
from ...schemas import ManifestResponse, ScanUnitResponse, WorkShardResponse


def work_shard_to_response(shard: WorkShard) -> WorkShardResponse:
    return WorkShardResponse(
        id=shard.id,
        job_id=shard.job_id,
        manifest_id=shard.manifest_id,
        shard_index=shard.shard_index,
        shard_path=shard.shard_path,
        status=shard.status,
        assigned_server_id=shard.assigned_server_id,
        file_count=shard.file_count,
        processed_files=shard.processed_files,
        failed_files=shard.failed_files,
        skipped_files=shard.skipped_files,
        completed_pages=shard.completed_pages,
        api_inflight=shard.api_inflight,
        api_inflight_peak=shard.api_inflight_peak,
        api_waiting=shard.api_waiting,
        oldest_api_inflight=shard.oldest_api_inflight,
        execution_paused=shard.execution_paused,
        api_concurrency_limit=shard.api_concurrency_limit,
        execution_control_reason=shard.execution_control_reason,
        failure_category=shard.failure_category,
        error_message=shard.error_message,
        attempt_count=shard.attempt_count,
        lease_expires_at=shard.lease_expires_at,
    )


def manifest_to_response(manifest) -> ManifestResponse:
    return ManifestResponse(
        id=manifest.id,
        job_id=manifest.job_id,
        input_mode=manifest.input_mode,
        input_root=manifest.input_root,
        manifest_path=manifest.manifest_path,
        meta_path=manifest.meta_path,
        file_count=manifest.file_count,
        total_bytes=manifest.total_bytes,
        status=manifest.status,
    )


def scan_unit_to_response(unit: ScanUnit) -> ScanUnitResponse:
    return ScanUnitResponse(
        id=unit.id,
        job_id=unit.job_id,
        path=unit.path,
        status=unit.status,
        assigned_server_id=unit.assigned_server_id,
        attempt_count=unit.attempt_count,
        lease_expires_at=unit.lease_expires_at,
        file_count=unit.file_count,
        total_bytes=unit.total_bytes,
        failure_category=unit.failure_category,
        error_message=unit.error_message,
    )
