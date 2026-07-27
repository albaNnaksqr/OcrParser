from __future__ import annotations

from .ports import RemoteWorkerPort


def preflight(executor: RemoteWorkerPort, request):
    return executor.preflight(request)


def install_dry_run(executor: RemoteWorkerPort, request):
    return executor.install_dry_run(request)


def install_apply(executor: RemoteWorkerPort, request):
    return executor.install_apply(request)


def scale_plan(executor: RemoteWorkerPort, request):
    return executor.scale_plan(request)


def scale_apply(executor: RemoteWorkerPort, request):
    return executor.scale_apply(request)


def service_action(executor: RemoteWorkerPort, request):
    return executor.service_action(request)
