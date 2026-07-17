from __future__ import annotations

from ...remote_workers import RemoteWorkerExecutor


def preflight(executor: RemoteWorkerExecutor, request):
    return executor.preflight(request)


def install_dry_run(executor: RemoteWorkerExecutor, request):
    return executor.install_dry_run(request)


def install_apply(executor: RemoteWorkerExecutor, request):
    return executor.install_apply(request)


def scale_plan(executor: RemoteWorkerExecutor, request):
    return executor.scale_plan(request)


def scale_apply(executor: RemoteWorkerExecutor, request):
    return executor.scale_apply(request)


def service_action(executor: RemoteWorkerExecutor, request):
    return executor.service_action(request)
