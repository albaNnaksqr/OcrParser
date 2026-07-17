from __future__ import annotations

from typing import Callable, Generator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...schemas import (
    JobCreateRequest,
    JobEventRequest,
    JobFileResponse,
    JobListResponse,
    JobLogListResponse,
    JobLogRequest,
    JobPreflightResponse,
    JobRecentErrorListResponse,
    JobResponse,
    JobSummaryListResponse,
    JobSummaryResponse,
)
from . import commands, queries
from ..workers.core import preflight_job
from .schemas import job_file_to_response, job_to_response


GetDb = Callable[[], Generator[Session, None, None]]


def create_router(get_db: GetDb) -> APIRouter:
    router = APIRouter()

    @router.post("/api/jobs/preflight", response_model=JobPreflightResponse)
    def api_preflight_job(request: JobCreateRequest, session: Session = Depends(get_db)):
        return preflight_job(session, request)

    @router.post("/api/jobs", response_model=JobResponse)
    def api_create_job(request: JobCreateRequest, session: Session = Depends(get_db)):
        try:
            return job_to_response(commands.create_job(session, request), session)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/jobs", response_model=list[JobResponse])
    def api_list_jobs(
        status: Optional[str] = None,
        include_archived: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_db),
    ):
        try:
            jobs = queries.list_jobs(
                session,
                status=status,
                include_archived=include_archived,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return [job_to_response(job, session) for job in jobs]

    @router.get("/api/jobs/page", response_model=JobListResponse)
    def api_list_jobs_page(
        status: Optional[str] = None,
        include_archived: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_db),
    ):
        try:
            return queries.list_jobs_page(
                session,
                status=status,
                include_archived=include_archived,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/jobs/summary", response_model=list[JobSummaryResponse])
    def api_list_job_summaries(
        status: Optional[str] = None,
        include_archived: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_db),
    ):
        try:
            return queries.list_job_summaries(
                session,
                status=status,
                limit=limit,
                offset=offset,
                include_archived=include_archived,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/jobs/summary/page", response_model=JobSummaryListResponse)
    def api_list_job_summaries_page(
        status: Optional[str] = None,
        include_archived: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_db),
    ):
        try:
            return queries.list_job_summaries_page(
                session,
                status=status,
                limit=limit,
                offset=offset,
                include_archived=include_archived,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/jobs/{job_id}", response_model=JobResponse)
    def api_get_job(job_id: str, session: Session = Depends(get_db)):
        try:
            return job_to_response(queries.get_job_or_raise(session, job_id), session)
        except commands.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/jobs/{job_id}/summary", response_model=JobSummaryResponse)
    def api_get_job_summary(job_id: str, session: Session = Depends(get_db)):
        try:
            return queries.get_job_summary(session, job_id)
        except commands.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/jobs/{job_id}/recent-files", response_model=list[JobFileResponse])
    def api_list_recent_job_files(
        job_id: str,
        kind: str = "processed",
        limit: int = Query(default=20, ge=1, le=100),
        session: Session = Depends(get_db),
    ):
        try:
            return [
                job_file_to_response(item)
                for item in queries.list_recent_job_files(session, job_id, kind, limit)
            ]
        except commands.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/jobs/{job_id}/recent-errors/page", response_model=JobRecentErrorListResponse)
    def api_list_recent_job_errors_page(
        job_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        failure_category: Optional[str] = None,
        session: Session = Depends(get_db),
    ):
        try:
            return queries.list_recent_job_errors_page(
                session,
                job_id,
                limit=limit,
                offset=offset,
                failure_category=failure_category,
            )
        except commands.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/jobs/{job_id}/events", response_model=JobResponse)
    def api_record_event(job_id: str, request: JobEventRequest, session: Session = Depends(get_db)):
        try:
            return job_to_response(commands.record_event(session, job_id, request), session)
        except commands.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/jobs/{job_id}/logs")
    def api_record_log(job_id: str, request: JobLogRequest, session: Session = Depends(get_db)):
        try:
            commands.record_log(session, job_id, request)
            return {"ok": True}
        except commands.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/jobs/{job_id}/logs/page", response_model=JobLogListResponse)
    def api_list_job_logs_page(
        job_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        server_id: Optional[str] = None,
        stream: Optional[str] = None,
        session: Session = Depends(get_db),
    ):
        try:
            return queries.list_job_logs_page(
                session,
                job_id,
                limit=limit,
                offset=offset,
                server_id=server_id,
                stream=stream,
            )
        except commands.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/jobs/{job_id}/request-stop", response_model=JobResponse)
    def api_request_stop(job_id: str, session: Session = Depends(get_db)):
        try:
            return job_to_response(commands.request_stop(session, job_id), session)
        except commands.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/jobs/{job_id}/archive")
    def api_archive_job(job_id: str, session: Session = Depends(get_db)):
        try:
            job = commands.archive_job(session, job_id)
            return {"ok": True, "archived": True, "job_id": job.id}
        except commands.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except commands.JobNotTerminalError as exc:
            raise HTTPException(
                status_code=409,
                detail="Only succeeded, failed, or stopped jobs can be archived.",
            ) from exc

    @router.delete("/api/jobs/{job_id}")
    def api_delete_job(job_id: str, session: Session = Depends(get_db)):
        try:
            commands.delete_job(session, job_id)
            return {"ok": True}
        except commands.UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except commands.JobNotTerminalError as exc:
            raise HTTPException(
                status_code=409,
                detail="Only succeeded, failed, or stopped jobs can be deleted.",
            ) from exc

    return router
