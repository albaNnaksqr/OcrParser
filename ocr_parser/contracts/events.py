from __future__ import annotations

from typing import Any, TypedDict

from .failure import FailureCategory


class JobEventPayload(TypedDict, total=False):
    type: str
    payload: dict[str, Any]
    failure_category: FailureCategory
