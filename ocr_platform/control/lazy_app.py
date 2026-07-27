from __future__ import annotations

import threading
from typing import Any, Callable


class _LazyControlApp:
    """Resolve the compatibility ASGI application on first actual use."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._application: Any | None = None
        self._lock = threading.Lock()

    def _resolve(self):
        application = self._application
        if application is not None:
            return application
        with self._lock:
            application = self._application
            if application is None:
                application = self._factory()
                self._application = application
        return application

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Any],
        send: Callable[..., Any],
    ) -> None:
        await self._resolve()(scope, receive, send)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)

    def __repr__(self) -> str:
        state = "resolved" if self._application is not None else "unresolved"
        return f"<ocr-platform-control ASGI app ({state})>"


__all__ = ["_LazyControlApp"]
