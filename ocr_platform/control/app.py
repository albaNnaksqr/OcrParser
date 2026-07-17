from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ocr_platform.legal import agpl_license_text, source_offer

from . import database
from .models import Job, ModelProfile, ScanUnit, Server, WorkShard
from .schemas import (
    DatabaseStatusResponse,
    JobCreateRequest,
    JobEventRequest,
    JobLogListResponse,
    JobFileResponse,
    JobLogRequest,
    JobListResponse,
    JobPreflightResponse,
    JobRecentErrorListResponse,
    JobResponse,
    ManifestFreezeReportResponse,
    ManifestIntegrityResponse,
    ManifestIntegrityWorkerCompleteRequest,
    ManifestIntegrityWorkerRequestResponse,
    ManifestIntegrityWorkerTask,
    ModelProfileRequest,
    ModelProfileResponse,
    RemoteWorkerInstallDryRunRequest,
    RemoteWorkerOperationResponse,
    RemoteWorkerPreflightRequest,
    RemoteWorkerScaleRequest,
    RemoteWorkerScaleResponse,
    RemoteWorkerServiceRequest,
    RemoteWorkerTargetListResponse,
    RemoteWorkerTargetResponse,
    ScanUnitCompleteRequest,
    ScanUnitFailRequest,
    ScanUnitResponse,
    ServerEligibilityItem,
    ServerEligibilityResponse,
    ShardAttemptListResponse,
    ShardAttemptResponse,
    ManifestResponse,
    RemoteManifestRegisterRequest,
    JobSummaryListResponse,
    JobSummaryResponse,
    ServerHeartbeatRequest,
    ServerRegisterRequest,
    ServerResponse,
    WorkShardResponse,
    WorkShardListResponse,
    WorkShardUpdateRequest,
)
from .remote_workers import (
    RemoteWorkerExecutor,
    RemoteWorkerResult,
    RemoteWorkerScaleResult,
    RemoteWorkerTarget,
    default_ssh_user,
    load_remote_worker_targets,
    validate_ssh_token,
)
from .service import (
    JobNotTerminalError,
    ScanUnitAttemptConflictError,
    ServerArchiveError,
    ShardAttemptConflictError,
    UnknownServerError,
    UnknownJobError,
    archive_job,
    archive_server,
    claim_next_scan_unit,
    claim_next_pending_shard,
    claim_next_job,
    count_active_jobs_for_server,
    count_running_shards_for_server,
    complete_scan_unit,
    complete_worker_manifest_integrity_check,
    create_job,
    delete_job,
    effective_server_status,
    fail_scan_unit,
    get_job_summary,
    get_job_or_raise,
    get_manifest_freeze_report,
    get_manifest_integrity_report,
    heartbeat_server,
    is_server_stale,
    has_static_shards,
    json_loads_list,
    json_loads_object,
    list_job_summaries,
    list_job_summaries_page,
    list_job_logs_page,
    list_recent_job_errors_page,
    list_model_profiles,
    list_server_eligibility,
    list_work_shards,
    list_recent_job_files,
    record_event,
    record_log,
    register_server,
    register_remote_manifest,
    public_assigned_server_id,
    request_stop,
    list_shard_attempts,
    list_shard_attempts_page,
    list_servers,
    model_profile_to_response,
    preflight_job,
    claim_worker_manifest_integrity_check,
    _resolve_model_profile_api_key,
    shard_attempt_to_response,
    update_work_shard,
    request_worker_manifest_integrity_check,
    upsert_model_profile,
    _normalized_status_filter,
)


API_TOKEN_ENV = "OCR_PLATFORM_API_TOKEN"
REQUIRE_API_TOKEN_ENV = "OCR_PLATFORM_REQUIRE_API_TOKEN"
REQUIRE_CURRENT_MIGRATIONS_ENV = "OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS"
ENABLE_REMOTE_ADMIN_ENV = "OCR_PLATFORM_ENABLE_REMOTE_ADMIN"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUTHY_ENV_VALUES


def _require_remote_admin() -> None:
    if _env_truthy(ENABLE_REMOTE_ADMIN_ENV):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "Remote worker administration is disabled; set "
            "OCR_PLATFORM_ENABLE_REMOTE_ADMIN=1 on the control server to enable it."
        ),
    )


def _configured_api_token() -> str | None:
    token = os.environ.get(API_TOKEN_ENV)
    return token or None


def _validate_api_token_config() -> None:
    if _env_truthy(REQUIRE_API_TOKEN_ENV) and not _configured_api_token():
        raise RuntimeError(
            "API token is required when OCR_PLATFORM_REQUIRE_API_TOKEN=1; "
            "set OCR_PLATFORM_API_TOKEN to a high-entropy shared secret."
        )


def _validate_current_migrations_for_engine(db_engine) -> None:
    if not _env_truthy(REQUIRE_CURRENT_MIGRATIONS_ENV):
        return
    status = database.describe_database_status(db_engine)
    if status.get("dialect") != "postgresql":
        return
    if status.get("is_current"):
        return
    missing = ", ".join(str(item) for item in status.get("missing_migrations") or [])
    latest = status.get("latest_applied_migration") or "none"
    if not status.get("schema_migrations_table_exists"):
        detail = "schema_migrations table is missing"
    elif missing:
        detail = f"missing migrations: {missing}"
    else:
        detail = f"latest applied migration: {latest}"
    raise RuntimeError(
        "PostgreSQL database migrations are not current when "
        f"{REQUIRE_CURRENT_MIGRATIONS_ENV}=1; {detail}."
    )


def _request_api_token(request: Request) -> str | None:
    generic_header_token = request.headers.get("X-API-Key")
    if generic_header_token:
        return generic_header_token
    header_token = request.headers.get("X-OCR-Platform-Token")
    if header_token:
        return header_token
    authorization = request.headers.get("Authorization") or ""
    prefix = "Bearer "
    if authorization.startswith(prefix):
        return authorization[len(prefix) :]
    return None


def _api_auth_status() -> dict[str, bool]:
    token_configured = _configured_api_token() is not None
    return {
        "enabled": token_configured,
        "required": _env_truthy(REQUIRE_API_TOKEN_ENV),
        "configured": token_configured,
    }


def _server_has_writable_shared_path(capabilities: dict[str, object]) -> bool:
    shared_paths = capabilities.get("shared_paths")
    if isinstance(shared_paths, list):
        for item in shared_paths:
            if not isinstance(item, dict):
                continue
            if item.get("exists") and item.get("readable") and item.get("writable"):
                return True
    shared_roots = capabilities.get("shared_roots")
    return isinstance(shared_roots, list) and any(str(root).strip() for root in shared_roots)


def _worker_diagnostics(session: Session) -> dict[str, int]:
    servers = list_servers(session)
    visible_servers = [server for server in servers if server.archived_at is None]
    ready = 0
    stale = 0
    with_shared_roots = 0
    resource_constrained = 0
    for server in visible_servers:
        capabilities = json_loads_object(server.capabilities_json)
        if is_server_stale(server):
            stale += 1
        if _server_has_writable_shared_path(capabilities):
            with_shared_roots += 1
        resource_pressure = capabilities.get("resource_pressure")
        if isinstance(resource_pressure, dict) and resource_pressure.get("constrained"):
            resource_constrained += 1
        if (
            effective_server_status(server) in {"idle", "online", "busy"}
            and not is_server_stale(server)
            and _server_has_writable_shared_path(capabilities)
        ):
            ready += 1
    return {
        "total": len(visible_servers),
        "ready": ready,
        "stale": stale,
        "with_shared_roots": with_shared_roots,
        "resource_constrained": resource_constrained,
    }


def _system_diagnostics(session: Session, *, strict_production: bool = False) -> dict[str, object]:
    database_status = database.describe_database_status(session.get_bind())
    api_auth = _api_auth_status()
    workers = _worker_diagnostics(session)
    issues: list[dict[str, object]] = []

    if database_status.get("dialect") != "postgresql":
        issues.append(
            {
                "severity": "warning",
                "code": "database_not_postgres",
                "message": "Production control deployments should use PostgreSQL.",
            }
        )
    if not database_status.get("schema_migrations_table_exists"):
        issues.append(
            {
                "severity": "warning",
                "code": "database_migrations_missing",
                "message": "schema_migrations table is missing.",
            }
        )
    elif not database_status.get("is_current"):
        issues.append(
            {
                "severity": "warning",
                "code": "database_migration_not_current",
                "message": "Control database migrations are not current.",
                "details": {"missing_migrations": database_status.get("missing_migrations") or []},
            }
        )
    if not api_auth["enabled"]:
        issues.append(
            {
                "severity": "warning",
                "code": "api_auth_disabled",
                "message": "Control API auth is disabled.",
            }
        )
    if workers["total"] == 0:
        issues.append(
            {
                "severity": "warning",
                "code": "no_workers",
                "message": "No workers have registered with the control API.",
            }
        )
    elif workers["ready"] == 0:
        issues.append(
            {
                "severity": "error",
                "code": "no_ready_workers",
                "message": "No ready workers are reporting writable shared roots.",
            }
        )
    if workers["resource_constrained"]:
        issues.append(
            {
                "severity": "warning",
                "code": "resource_constrained_workers",
                "message": "One or more workers report resource pressure.",
                "details": {"count": workers["resource_constrained"]},
            }
        )

    ok = not any(issue["severity"] == "error" for issue in issues)
    if (strict_production or _env_truthy("OCR_PLATFORM_REQUIRE_POSTGRES")) and database_status.get("dialect") != "postgresql":
        ok = False
    if _env_truthy(REQUIRE_CURRENT_MIGRATIONS_ENV) and not database_status.get("is_current"):
        ok = False
    return {
        "ok": ok,
        "service": "ocr-platform-control",
        "database": database_status,
        "api_auth": api_auth,
        "workers": workers,
        "issues": issues,
    }


def server_to_response(server: Server, session: Session) -> ServerResponse:
    return ServerResponse(
        id=server.id,
        name=server.name,
        host=server.host,
        status=effective_server_status(server),
        capacity_slots=server.capacity_slots,
        capabilities=json_loads_object(server.capabilities_json),
        last_heartbeat_at=server.last_heartbeat_at,
        is_stale=is_server_stale(server),
        active_jobs=count_active_jobs_for_server(session, server.id),
        running_shards=count_running_shards_for_server(session, server.id),
    )


def job_to_response(job: Job, session: Session, include_secrets: bool = False) -> JobResponse:
    extra_args = json_loads_object(job.extra_args_json)
    if include_secrets:
        api_key_env_var = extra_args.pop("api_key_env_var", None)
        if api_key_env_var and "api_key" not in extra_args:
            api_key_from_env = os.environ.get(str(api_key_env_var))
            if api_key_from_env:
                extra_args["api_key"] = api_key_from_env
        if job.model_profile_id and "api_key" not in extra_args:
            profile = session.get(ModelProfile, job.model_profile_id)
            profile_api_key = _resolve_model_profile_api_key(profile) if profile is not None else None
            if profile_api_key:
                extra_args["api_key"] = profile_api_key
    else:
        extra_args.pop("api_key", None)
    return JobResponse(
        id=job.id,
        input_dir=job.input_dir,
        output_dir=job.output_dir,
        engine=job.engine,
        model_profile_id=job.model_profile_id,
        input_mode=job.input_mode,
        manifest_root=job.manifest_root,
        target_files_per_shard=job.target_files_per_shard,
        max_shard_attempts=job.max_shard_attempts,
        assigned_server_id=public_assigned_server_id(job),
        allowed_server_ids=json_loads_list(job.allowed_server_ids_json),
        status=job.status,
        failure_category=job.failure_category,
        error_message=job.error_message,
        stop_requested=job.stop_requested,
        force_reprocess=job.force_reprocess,
        archived_at=job.archived_at,
        engine_config=job.engine_config,
        ip=job.ip,
        port=job.port,
        model_name=job.model_name,
        page_concurrency=job.page_concurrency,
        has_static_shards=has_static_shards(session, job.id),
        extra_args=extra_args,
        command=json_loads_list(job.command_json),
        files=[
            JobFileResponse(
                file_path=item.file_path,
                filename=item.filename,
                status=item.status,
                total_pages=item.total_pages,
                done_pages=item.done_pages,
                output_path=item.output_path,
                error=item.error,
            )
            for item in job.files
        ],
    )


def job_file_to_response(item) -> JobFileResponse:
    return JobFileResponse(
        file_path=item.file_path,
        filename=item.filename,
        status=item.status,
        total_pages=item.total_pages,
        done_pages=item.done_pages,
        output_path=item.output_path,
        error=item.error,
        failure_category=item.failure_category,
    )


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


def create_app(
    session_factory: Optional[sessionmaker[Session]] = None,
    remote_worker_executor: Optional[RemoteWorkerExecutor] = None,
) -> FastAPI:
    _validate_api_token_config()
    remote_worker_executor = remote_worker_executor or RemoteWorkerExecutor()

    if session_factory is None:

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            database.init_db()
            if database.engine is not None:
                _validate_current_migrations_for_engine(database.engine)
            yield

        app = FastAPI(title="OCR Platform Control API", lifespan=lifespan)
    else:
        with session_factory() as session:
            _validate_current_migrations_for_engine(session.get_bind())
        app = FastAPI(title="OCR Platform Control API")

    @app.middleware("http")
    async def api_token_auth(request: Request, call_next):
        configured_token = _configured_api_token()
        if configured_token and request.url.path.startswith("/api/"):
            request_token = _request_api_token(request)
            if request_token is None or not secrets.compare_digest(
                request_token,
                configured_token,
            ):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing or invalid API token"},
                )
        return await call_next(request)

    @app.api_route("/source", methods=["GET", "HEAD"], include_in_schema=False)
    def corresponding_source() -> RedirectResponse:
        """Public AGPLv3 corresponding-source offer; intentionally unauthenticated."""

        return RedirectResponse(url=str(source_offer()["source_url"]))

    @app.get("/source.json", include_in_schema=False)
    def corresponding_source_metadata() -> dict[str, object]:
        return source_offer()

    @app.get("/legal/agpl-3.0", include_in_schema=False)
    def agpl_license() -> PlainTextResponse:
        return PlainTextResponse(agpl_license_text(), media_type="text/plain; charset=utf-8")

    ui_path = Path(__file__).resolve().parent / "ui"
    if ui_path.exists():

        @app.get("/", include_in_schema=False)
        def api_root() -> RedirectResponse:
            return RedirectResponse(url="/ui/")

        app.mount("/ui", StaticFiles(directory=str(ui_path), html=True), name="ui")

    else:

        @app.get("/", include_in_schema=False)
        def api_root() -> dict[str, str]:
            return {"message": "OCR Platform Control API"}

    if session_factory is None:

        def get_db() -> Generator[Session, None, None]:
            yield from database.get_session()

    else:

        def get_db() -> Generator[Session, None, None]:
            with session_factory() as session:
                yield session

    @app.get("/healthz")
    def api_healthz() -> dict[str, object]:
        return {"ok": True, "service": "ocr-platform-control"}

    @app.get("/readyz")
    def api_readyz(session: Session = Depends(get_db)):
        try:
            payload = _system_diagnostics(session)
        except Exception as exc:  # pragma: no cover - defensive readiness path
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "service": "ocr-platform-control",
                    "error": str(exc),
                },
            )
        status_code = 200 if payload["ok"] else 503
        return JSONResponse(status_code=status_code, content=payload)

    @app.get("/api/system/database", response_model=DatabaseStatusResponse)
    def api_database_status(session: Session = Depends(get_db)):
        return database.describe_database_status(session.get_bind())

    @app.get("/api/system/diagnostics")
    def api_system_diagnostics(session: Session = Depends(get_db)):
        return _system_diagnostics(session, strict_production=True)

    @app.post("/api/servers/register", response_model=ServerResponse)
    def api_register_server(request: ServerRegisterRequest, session: Session = Depends(get_db)):
        return server_to_response(register_server(session, request), session)

    @app.post("/api/servers/{server_id}/heartbeat", response_model=ServerResponse)
    def api_server_heartbeat(
        server_id: str,
        request: ServerHeartbeatRequest,
        session: Session = Depends(get_db),
    ):
        return server_to_response(heartbeat_server(session, server_id, request), session)

    @app.get("/api/model-profiles", response_model=list[ModelProfileResponse])
    def api_list_model_profiles(session: Session = Depends(get_db)):
        return [model_profile_to_response(profile) for profile in list_model_profiles(session)]

    @app.put("/api/model-profiles/{profile_id}", response_model=ModelProfileResponse)
    def api_upsert_model_profile(
        profile_id: str,
        request: ModelProfileRequest,
        session: Session = Depends(get_db),
    ):
        try:
            return model_profile_to_response(upsert_model_profile(session, profile_id, request))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/jobs/preflight", response_model=JobPreflightResponse)
    def api_preflight_job(request: JobCreateRequest, session: Session = Depends(get_db)):
        return preflight_job(session, request)

    @app.post("/api/jobs", response_model=JobResponse)
    def api_create_job(request: JobCreateRequest, session: Session = Depends(get_db)):
        try:
            return job_to_response(create_job(session, request), session)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/jobs", response_model=list[JobResponse])
    def api_list_jobs(
        status: Optional[str] = None,
        include_archived: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_db),
    ):
        stmt = select(Job).order_by(Job.created_at.desc()).offset(offset).limit(limit)
        try:
            status = _normalized_status_filter(status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if status:
            stmt = stmt.where(Job.status == status)
        if not include_archived:
            stmt = stmt.where(Job.archived_at.is_(None))
        jobs = session.execute(stmt).scalars().all()
        return [job_to_response(job, session) for job in jobs]

    @app.get("/api/jobs/page", response_model=JobListResponse)
    def api_list_jobs_page(
        status: Optional[str] = None,
        include_archived: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_db),
    ):
        count_stmt = select(func.count(Job.id))
        item_stmt = select(Job).order_by(Job.created_at.desc())
        try:
            status = _normalized_status_filter(status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if status:
            count_stmt = count_stmt.where(Job.status == status)
            item_stmt = item_stmt.where(Job.status == status)
        if not include_archived:
            count_stmt = count_stmt.where(Job.archived_at.is_(None))
            item_stmt = item_stmt.where(Job.archived_at.is_(None))
        total = int(session.execute(count_stmt).scalar_one() or 0)
        jobs = session.execute(item_stmt.offset(offset).limit(limit)).scalars().all()
        return JobListResponse(
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(jobs) < total,
            items=[job_to_response(job, session) for job in jobs],
        )

    @app.get("/api/jobs/summary", response_model=list[JobSummaryResponse])
    def api_list_job_summaries(
        status: Optional[str] = None,
        include_archived: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_db),
    ):
        try:
            return list_job_summaries(
                session,
                status=status,
                limit=limit,
                offset=offset,
                include_archived=include_archived,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/jobs/summary/page", response_model=JobSummaryListResponse)
    def api_list_job_summaries_page(
        status: Optional[str] = None,
        include_archived: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_db),
    ):
        try:
            return list_job_summaries_page(
                session,
                status=status,
                limit=limit,
                offset=offset,
                include_archived=include_archived,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/servers", response_model=list[ServerResponse])
    def api_list_servers(
        include_archived: bool = False,
        session: Session = Depends(get_db),
    ):
        return [
            server_to_response(server, session)
            for server in list_servers(session, include_archived=include_archived)
        ]

    @app.delete("/api/servers/{server_id}")
    def api_archive_server(server_id: str, session: Session = Depends(get_db)):
        try:
            archive_server(session, server_id)
            return {"ok": True, "archived": True}
        except UnknownServerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ServerArchiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _remote_worker_response(result: RemoteWorkerResult) -> RemoteWorkerOperationResponse:
        return RemoteWorkerOperationResponse(
            ok=result.ok,
            action=result.action,
            host=result.host,
            command=result.command,
            return_code=result.return_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def _remote_worker_scale_response(result: RemoteWorkerScaleResult) -> RemoteWorkerScaleResponse:
        return RemoteWorkerScaleResponse(
            ok=result.ok,
            action=result.action,
            host=result.host,
            command=result.command,
            return_code=result.return_code,
            stdout=result.stdout,
            stderr=result.stderr,
            plan_items=result.plan_items,
        )

    def _remote_worker_target_response(target: RemoteWorkerTarget) -> RemoteWorkerTargetResponse:
        return RemoteWorkerTargetResponse(
            id=target.id,
            host=target.host,
            hostname=target.hostname,
            ssh_user=target.ssh_user,
            server_id=target.server_id,
            service_user=target.service_user,
            service_group=target.service_group,
            repo_dir=target.repo_dir,
            control_url=target.control_url,
            shared_roots=list(target.shared_roots),
        )

    def _validate_remote_worker_target(request) -> None:
        try:
            validate_ssh_token(request.host, field_name="host")
            if request.ssh_user:
                validate_ssh_token(request.ssh_user, field_name="ssh_user")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _validate_remote_worker_scale_request(request: RemoteWorkerScaleRequest) -> None:
        _validate_remote_worker_target(request)
        try:
            validate_ssh_token(request.server_id_prefix, field_name="server_id_prefix")
            if request.seed_server_id:
                validate_ssh_token(request.seed_server_id, field_name="seed_server_id")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _with_default_ssh_user(request):
        if request.ssh_user:
            return request
        return request.copy(update={"ssh_user": default_ssh_user() or None})

    @app.get("/api/remote-workers/targets", response_model=RemoteWorkerTargetListResponse)
    def api_remote_worker_targets():
        _require_remote_admin()
        return RemoteWorkerTargetListResponse(
            targets=[
                _remote_worker_target_response(target)
                for target in load_remote_worker_targets()
            ]
        )

    @app.post("/api/remote-workers/preflight", response_model=RemoteWorkerOperationResponse)
    def api_remote_worker_preflight(request: RemoteWorkerPreflightRequest):
        _require_remote_admin()
        request = _with_default_ssh_user(request)
        _validate_remote_worker_target(request)
        return _remote_worker_response(remote_worker_executor.preflight(request))

    @app.post("/api/remote-workers/install-dry-run", response_model=RemoteWorkerOperationResponse)
    def api_remote_worker_install_dry_run(request: RemoteWorkerInstallDryRunRequest):
        _require_remote_admin()
        request = _with_default_ssh_user(request)
        _validate_remote_worker_target(request)
        return _remote_worker_response(remote_worker_executor.install_dry_run(request))

    @app.post("/api/remote-workers/install-apply", response_model=RemoteWorkerOperationResponse)
    def api_remote_worker_install_apply(request: RemoteWorkerInstallDryRunRequest):
        _require_remote_admin()
        request = _with_default_ssh_user(request)
        _validate_remote_worker_target(request)
        return _remote_worker_response(remote_worker_executor.install_apply(request))

    @app.post("/api/remote-workers/scale-plan", response_model=RemoteWorkerScaleResponse)
    def api_remote_worker_scale_plan(request: RemoteWorkerScaleRequest):
        _require_remote_admin()
        request = _with_default_ssh_user(request)
        _validate_remote_worker_scale_request(request)
        return _remote_worker_scale_response(remote_worker_executor.scale_plan(request))

    @app.post("/api/remote-workers/scale-apply", response_model=RemoteWorkerScaleResponse)
    def api_remote_worker_scale_apply(request: RemoteWorkerScaleRequest):
        _require_remote_admin()
        request = _with_default_ssh_user(request)
        _validate_remote_worker_scale_request(request)
        return _remote_worker_scale_response(remote_worker_executor.scale_apply(request))

    @app.post("/api/remote-workers/service", response_model=RemoteWorkerOperationResponse)
    def api_remote_worker_service_action(request: RemoteWorkerServiceRequest):
        _require_remote_admin()
        request = _with_default_ssh_user(request)
        _validate_remote_worker_target(request)
        return _remote_worker_response(remote_worker_executor.service_action(request))

    @app.get("/api/servers/eligibility", response_model=ServerEligibilityResponse)
    def api_server_eligibility(input_dir: str, session: Session = Depends(get_db)):
        items = [
            ServerEligibilityItem(**item)
            for item in list_server_eligibility(session, input_dir)
        ]
        return ServerEligibilityResponse(
            input_dir=input_dir,
            total_servers=len(items),
            eligible_servers=sum(1 for item in items if item.can_access),
            servers=items,
        )

    @app.get("/api/jobs/{job_id}", response_model=JobResponse)
    def api_get_job(job_id: str, session: Session = Depends(get_db)):
        try:
            return job_to_response(get_job_or_raise(session, job_id), session)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/summary", response_model=JobSummaryResponse)
    def api_get_job_summary(job_id: str, session: Session = Depends(get_db)):
        try:
            return get_job_summary(session, job_id)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/manifest/integrity", response_model=ManifestIntegrityResponse)
    def api_get_manifest_integrity(job_id: str, session: Session = Depends(get_db)):
        try:
            return get_manifest_integrity_report(session, job_id)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/api/jobs/{job_id}/manifest/integrity/worker-request",
        response_model=ManifestIntegrityWorkerRequestResponse,
    )
    def api_request_worker_manifest_integrity(job_id: str, session: Session = Depends(get_db)):
        try:
            return request_worker_manifest_integrity_check(session, job_id)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/manifest/freeze-report", response_model=ManifestFreezeReportResponse)
    def api_get_manifest_freeze_report(job_id: str, session: Session = Depends(get_db)):
        try:
            return get_manifest_freeze_report(session, job_id)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/shards", response_model=WorkShardListResponse)
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
            items, total = list_work_shards(
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
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/shards/{shard_id}/attempts", response_model=list[ShardAttemptResponse])
    def api_list_shard_attempts(
        job_id: str,
        shard_id: int,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_db),
    ):
        try:
            return [
                shard_attempt_to_response(attempt)
                for attempt in list_shard_attempts(
                    session,
                    job_id,
                    shard_id,
                    limit=limit,
                    offset=offset,
                )
            ]
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/shards/{shard_id}/attempts/page", response_model=ShardAttemptListResponse)
    def api_list_shard_attempts_page(
        job_id: str,
        shard_id: int,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_db),
    ):
        try:
            return list_shard_attempts_page(
                session,
                job_id,
                shard_id,
                limit=limit,
                offset=offset,
            )
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/shards/claim", response_model=Optional[WorkShardResponse])
    def api_claim_next_shard(job_id: str, server_id: str, session: Session = Depends(get_db)):
        try:
            shard = claim_next_pending_shard(session, job_id, server_id)
            return work_shard_to_response(shard) if shard is not None else None
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/manifest", response_model=ManifestResponse)
    def api_register_remote_manifest(
        job_id: str,
        request: RemoteManifestRegisterRequest,
        session: Session = Depends(get_db),
    ):
        try:
            return manifest_to_response(register_remote_manifest(session, job_id, request))
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/scan-units/claim", response_model=Optional[ScanUnitResponse])
    def api_claim_scan_unit(server_id: str, session: Session = Depends(get_db)):
        unit = claim_next_scan_unit(session, server_id)
        return scan_unit_to_response(unit) if unit is not None else None

    @app.post("/api/manifest-integrity/claim", response_model=Optional[ManifestIntegrityWorkerTask])
    def api_claim_manifest_integrity(server_id: str, session: Session = Depends(get_db)):
        return claim_worker_manifest_integrity_check(session, server_id)

    @app.post(
        "/api/manifest-integrity/{manifest_id}/complete",
        response_model=ManifestIntegrityWorkerRequestResponse,
    )
    def api_complete_manifest_integrity(
        manifest_id: int,
        server_id: str,
        request: ManifestIntegrityWorkerCompleteRequest,
        session: Session = Depends(get_db),
    ):
        try:
            return complete_worker_manifest_integrity_check(
                session,
                manifest_id,
                server_id,
                request,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/scan-units/{scan_unit_id}/complete", response_model=ScanUnitResponse)
    def api_complete_scan_unit(
        scan_unit_id: int,
        request: ScanUnitCompleteRequest,
        session: Session = Depends(get_db),
    ):
        try:
            return scan_unit_to_response(complete_scan_unit(session, scan_unit_id, request))
        except ScanUnitAttemptConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/scan-units/{scan_unit_id}/fail", response_model=ScanUnitResponse)
    def api_fail_scan_unit(
        scan_unit_id: int,
        request: ScanUnitFailRequest,
        session: Session = Depends(get_db),
    ):
        try:
            return scan_unit_to_response(fail_scan_unit(session, scan_unit_id, request))
        except ScanUnitAttemptConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/shards/{shard_id}", response_model=WorkShardResponse)
    def api_update_work_shard(
        shard_id: int,
        request: WorkShardUpdateRequest,
        session: Session = Depends(get_db),
    ):
        try:
            return work_shard_to_response(update_work_shard(session, shard_id, request))
        except ShardAttemptConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/recent-files", response_model=list[JobFileResponse])
    def api_list_recent_job_files(
        job_id: str,
        kind: str = "processed",
        limit: int = Query(default=20, ge=1, le=100),
        session: Session = Depends(get_db),
    ):
        try:
            return [job_file_to_response(item) for item in list_recent_job_files(session, job_id, kind, limit)]
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/recent-errors/page", response_model=JobRecentErrorListResponse)
    def api_list_recent_job_errors_page(
        job_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        failure_category: Optional[str] = None,
        session: Session = Depends(get_db),
    ):
        try:
            return list_recent_job_errors_page(
                session,
                job_id,
                limit=limit,
                offset=offset,
                failure_category=failure_category,
            )
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/agents/{server_id}/next-job", response_model=Optional[JobResponse])
    def api_next_job(server_id: str, session: Session = Depends(get_db)):
        job = claim_next_job(session, server_id)
        return job_to_response(job, session, include_secrets=True) if job is not None else None

    @app.post("/api/jobs/{job_id}/events", response_model=JobResponse)
    def api_record_event(job_id: str, request: JobEventRequest, session: Session = Depends(get_db)):
        try:
            return job_to_response(record_event(session, job_id, request), session)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/logs")
    def api_record_log(job_id: str, request: JobLogRequest, session: Session = Depends(get_db)):
        try:
            record_log(session, job_id, request)
            return {"ok": True}
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/logs/page", response_model=JobLogListResponse)
    def api_list_job_logs_page(
        job_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        server_id: Optional[str] = None,
        stream: Optional[str] = None,
        session: Session = Depends(get_db),
    ):
        try:
            return list_job_logs_page(
                session,
                job_id,
                limit=limit,
                offset=offset,
                server_id=server_id,
                stream=stream,
            )
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/request-stop", response_model=JobResponse)
    def api_request_stop(job_id: str, session: Session = Depends(get_db)):
        try:
            return job_to_response(request_stop(session, job_id), session)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/archive")
    def api_archive_job(job_id: str, session: Session = Depends(get_db)):
        try:
            job = archive_job(session, job_id)
            return {"ok": True, "archived": True, "job_id": job.id}
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except JobNotTerminalError as exc:
            raise HTTPException(
                status_code=409,
                detail="Only succeeded, failed, or stopped jobs can be archived.",
            ) from exc

    @app.delete("/api/jobs/{job_id}")
    def api_delete_job(job_id: str, session: Session = Depends(get_db)):
        try:
            delete_job(session, job_id)
            return {"ok": True}
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except JobNotTerminalError as exc:
            raise HTTPException(
                status_code=409,
                detail="Only succeeded, failed, or stopped jobs can be deleted.",
            ) from exc

    return app


app = create_app()
