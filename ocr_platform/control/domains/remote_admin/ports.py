from __future__ import annotations

from typing import Any, Protocol


class RemoteWorkerPort(Protocol):
    def preflight(self, request: Any) -> Any: ...

    def install_dry_run(self, request: Any) -> Any: ...

    def install_apply(self, request: Any) -> Any: ...

    def scale_plan(self, request: Any) -> Any: ...

    def scale_apply(self, request: Any) -> Any: ...

    def service_action(self, request: Any) -> Any: ...


__all__ = ["RemoteWorkerPort"]
