from __future__ import annotations

from typing import Callable, Generator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...schemas import (
    ManifestFreezeReportResponse,
    ManifestIntegrityResponse,
    ManifestIntegrityWorkerCompleteRequest,
    ManifestIntegrityWorkerRequestResponse,
    ManifestIntegrityWorkerTask,
    ManifestResponse,
    RemoteManifestRegisterRequest,
    ScanUnitCompleteRequest,
    ScanUnitFailRequest,
    ScanUnitResponse,
    ShardAttemptListResponse,
    ShardAttemptResponse,
    WorkShardListResponse,
    WorkShardResponse,
    WorkShardUpdateRequest,
)
from . import commands, queries
from .schemas import manifest_to_response, scan_unit_to_response, work_shard_to_response


GetDb = Callable[[], Generator[Session, None, None]]


def create_router(get_db: GetDb) -> APIRouter:
    router = APIRouter()

    @router.get("/api/jobs/{job_id}/manifest/integrity", response_model=ManifestIntegrityResponse)
    def api_get_manifest_integrity(job_id: str, session: Session = Depends(get_db)):
        try:
            return queries.get_manifest_integrity_report(session, job_id)
        except queries.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/jobs/{job_id}/manifest/integrity/worker-request", response_model=ManifestIntegrityWorkerRequestResponse)
    def api_request_worker_manifest_integrity(job_id: str, session: Session = Depends(get_db)):
        try:
            return commands.request_worker_manifest_integrity_check(session, job_id)
        except queries.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/jobs/{job_id}/manifest/freeze-report", response_model=ManifestFreezeReportResponse)
    def api_get_manifest_freeze_report(job_id: str, session: Session = Depends(get_db)):
        try:
            return queries.get_manifest_freeze_report(session, job_id)
        except queries.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/jobs/{job_id}/shards", response_model=WorkShardListResponse)
    def api_list_work_shards(
        job_id: str,
        status: str = Query(default="all"),
        worker_id: Optional[str] = None,
        failure_category: Optional[str] = None,
        min_attempt_count: Optional[int] = Query(default=None, ge=0),
        running_longer_than_seconds: Optional[int] = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_db),
    ):
        try:
            items, total = queries.list_work_shards(
                session,
                job_id,
                status=status,
                worker_id=worker_id,
                failure_category=failure_category,
                min_attempt_count=min_attempt_count,
                running_longer_than_seconds=running_longer_than_seconds,
                limit=limit,
                offset=offset,
            )
            return WorkShardListResponse(
                job_id=job_id,
                total=total,
                limit=limit,
                offset=offset,
                has_more=offset + len(items) < total,
                items=[work_shard_to_response(item) for item in items],
            )
        except queries.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/jobs/{job_id}/shards/{shard_id}/attempts", response_model=list[ShardAttemptResponse])
    def api_list_shard_attempts(
        job_id: str,
        shard_id: int,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_db),
    ):
        try:
            return [
                queries.shard_attempt_to_response(attempt)
                for attempt in queries.list_shard_attempts(session, job_id, shard_id, limit=limit, offset=offset)
            ]
        except queries.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/jobs/{job_id}/shards/{shard_id}/attempts/page", response_model=ShardAttemptListResponse)
    def api_list_shard_attempts_page(
        job_id: str,
        shard_id: int,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_db),
    ):
        try:
            return queries.list_shard_attempts_page(session, job_id, shard_id, limit=limit, offset=offset)
        except queries.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/jobs/{job_id}/shards/claim", response_model=Optional[WorkShardResponse])
    def api_claim_next_shard(job_id: str, server_id: str, session: Session = Depends(get_db)):
        try:
            shard = commands.claim_next_pending_shard(session, job_id, server_id)
            return work_shard_to_response(shard) if shard is not None else None
        except queries.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/jobs/{job_id}/manifest", response_model=ManifestResponse)
    def api_register_remote_manifest(job_id: str, request: RemoteManifestRegisterRequest, session: Session = Depends(get_db)):
        try:
            return manifest_to_response(commands.register_remote_manifest(session, job_id, request))
        except queries.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/scan-units/claim", response_model=Optional[ScanUnitResponse])
    def api_claim_scan_unit(server_id: str, session: Session = Depends(get_db)):
        unit = commands.claim_next_scan_unit(session, server_id)
        return scan_unit_to_response(unit) if unit is not None else None

    @router.post("/api/manifest-integrity/claim", response_model=Optional[ManifestIntegrityWorkerTask])
    def api_claim_manifest_integrity(server_id: str, session: Session = Depends(get_db)):
        return commands.claim_worker_manifest_integrity_check(session, server_id)

    @router.post("/api/manifest-integrity/{manifest_id}/complete", response_model=ManifestIntegrityWorkerRequestResponse)
    def api_complete_manifest_integrity(
        manifest_id: int,
        server_id: str,
        request: ManifestIntegrityWorkerCompleteRequest,
        session: Session = Depends(get_db),
    ):
        try:
            return commands.complete_worker_manifest_integrity_check(session, manifest_id, server_id, request)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/scan-units/{scan_unit_id}/complete", response_model=ScanUnitResponse)
    def api_complete_scan_unit(scan_unit_id: int, request: ScanUnitCompleteRequest, session: Session = Depends(get_db)):
        try:
            return scan_unit_to_response(commands.complete_scan_unit(session, scan_unit_id, request))
        except commands.ScanUnitAttemptConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/scan-units/{scan_unit_id}/fail", response_model=ScanUnitResponse)
    def api_fail_scan_unit(scan_unit_id: int, request: ScanUnitFailRequest, session: Session = Depends(get_db)):
        try:
            return scan_unit_to_response(commands.fail_scan_unit(session, scan_unit_id, request))
        except commands.ScanUnitAttemptConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/shards/{shard_id}", response_model=WorkShardResponse)
    def api_update_work_shard(shard_id: int, request: WorkShardUpdateRequest, session: Session = Depends(get_db)):
        try:
            return work_shard_to_response(commands.update_work_shard(session, shard_id, request))
        except commands.ShardAttemptConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
