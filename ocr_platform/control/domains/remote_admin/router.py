from __future__ import annotations

from fastapi import APIRouter

from ...remote_workers import RemoteWorkerExecutor
from ...schemas import (
    RemoteWorkerInstallDryRunRequest,
    RemoteWorkerOperationResponse,
    RemoteWorkerPreflightRequest,
    RemoteWorkerScaleRequest,
    RemoteWorkerScaleResponse,
    RemoteWorkerServiceRequest,
    RemoteWorkerTargetListResponse,
)
from . import commands, queries, service
from .schemas import operation_response, scale_response, target_response


def create_router(executor: RemoteWorkerExecutor) -> APIRouter:
    router = APIRouter()

    @router.get("/api/remote-workers/targets", response_model=RemoteWorkerTargetListResponse)
    def api_remote_worker_targets():
        service.require_enabled()
        return RemoteWorkerTargetListResponse(
            targets=[target_response(target) for target in queries.load_remote_worker_targets()]
        )

    @router.post("/api/remote-workers/preflight", response_model=RemoteWorkerOperationResponse)
    def api_remote_worker_preflight(request: RemoteWorkerPreflightRequest):
        service.require_enabled()
        request = service.with_default_ssh_user(request)
        service.validate_target(request)
        return operation_response(commands.preflight(executor, request))

    @router.post("/api/remote-workers/install-dry-run", response_model=RemoteWorkerOperationResponse)
    def api_remote_worker_install_dry_run(request: RemoteWorkerInstallDryRunRequest):
        service.require_enabled()
        request = service.with_default_ssh_user(request)
        service.validate_target(request)
        return operation_response(commands.install_dry_run(executor, request))

    @router.post("/api/remote-workers/install-apply", response_model=RemoteWorkerOperationResponse)
    def api_remote_worker_install_apply(request: RemoteWorkerInstallDryRunRequest):
        service.require_enabled()
        request = service.with_default_ssh_user(request)
        service.validate_target(request)
        return operation_response(commands.install_apply(executor, request))

    @router.post("/api/remote-workers/scale-plan", response_model=RemoteWorkerScaleResponse)
    def api_remote_worker_scale_plan(request: RemoteWorkerScaleRequest):
        service.require_enabled()
        request = service.with_default_ssh_user(request)
        service.validate_scale_request(request)
        return scale_response(commands.scale_plan(executor, request))

    @router.post("/api/remote-workers/scale-apply", response_model=RemoteWorkerScaleResponse)
    def api_remote_worker_scale_apply(request: RemoteWorkerScaleRequest):
        service.require_enabled()
        request = service.with_default_ssh_user(request)
        service.validate_scale_request(request)
        return scale_response(commands.scale_apply(executor, request))

    @router.post("/api/remote-workers/service", response_model=RemoteWorkerOperationResponse)
    def api_remote_worker_service_action(request: RemoteWorkerServiceRequest):
        service.require_enabled()
        request = service.with_default_ssh_user(request)
        service.validate_target(request)
        return operation_response(commands.service_action(executor, request))

    return router
