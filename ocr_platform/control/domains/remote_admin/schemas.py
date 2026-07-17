from ...remote_workers import RemoteWorkerResult, RemoteWorkerScaleResult, RemoteWorkerTarget
from ...schemas import (
    RemoteWorkerOperationResponse,
    RemoteWorkerScaleResponse,
    RemoteWorkerTargetResponse,
)


def operation_response(result: RemoteWorkerResult) -> RemoteWorkerOperationResponse:
    return RemoteWorkerOperationResponse(
        ok=result.ok,
        action=result.action,
        host=result.host,
        command=result.command,
        return_code=result.return_code,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def scale_response(result: RemoteWorkerScaleResult) -> RemoteWorkerScaleResponse:
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


def target_response(target: RemoteWorkerTarget) -> RemoteWorkerTargetResponse:
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
