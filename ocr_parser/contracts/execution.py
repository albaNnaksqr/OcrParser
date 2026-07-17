from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple


STAGE_STATUSES = frozenset({"success", "failed", "skipped"})


@dataclass(frozen=True)
class StageOutcome:
    """Stable, low-cardinality outcome for one engine execution stage."""

    stage: str
    status: str
    failure_category: Optional[str] = None
    duration_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.stage or not self.stage.strip():
            raise ValueError("stage must be a non-empty string")
        if self.status not in STAGE_STATUSES:
            choices = ", ".join(sorted(STAGE_STATUSES))
            raise ValueError(f"status must be one of: {choices}")
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise ValueError("duration_seconds must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stage": self.stage,
            "status": self.status,
        }
        if self.failure_category is not None:
            payload["failure_category"] = self.failure_category
        if self.duration_seconds is not None:
            payload["duration_seconds"] = round(self.duration_seconds, 6)
        return payload


@dataclass(frozen=True)
class FallbackInfo:
    """Whether execution used a degraded path and why it was selected."""

    used: bool = False
    reason: Optional[str] = None
    source_stage: Optional[str] = None

    def __post_init__(self) -> None:
        if self.used and (not self.reason or not self.source_stage):
            raise ValueError("used fallback requires reason and source_stage")
        if not self.used and (self.reason is not None or self.source_stage is not None):
            raise ValueError("unused fallback cannot have reason or source_stage")

    def to_dict(self) -> dict[str, Any]:
        return {
            "used": self.used,
            "reason": self.reason,
            "source_stage": self.source_stage,
        }


@dataclass(frozen=True)
class EngineExecutionTrace:
    """Execution metadata carried from an engine through events and artifacts."""

    stages: Tuple[StageOutcome, ...] = field(default_factory=tuple)
    fallback: FallbackInfo = field(default_factory=FallbackInfo)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stages": [stage.to_dict() for stage in self.stages],
            "fallback": self.fallback.to_dict(),
        }


def execution_metadata(value: Mapping[str, Any] | EngineExecutionTrace | None) -> dict[str, Any]:
    """Return a JSON-safe trace, using explicit non-fallback defaults when absent."""

    if isinstance(value, EngineExecutionTrace):
        return value.to_dict()
    if not isinstance(value, Mapping):
        return EngineExecutionTrace().to_dict()
    stages = value.get("stages")
    fallback = value.get("fallback")
    return {
        "stages": list(stages) if isinstance(stages, Sequence) and not isinstance(stages, (str, bytes)) else [],
        "fallback": {
            "used": bool(fallback.get("used", False)) if isinstance(fallback, Mapping) else False,
            "reason": fallback.get("reason") if isinstance(fallback, Mapping) else None,
            "source_stage": fallback.get("source_stage") if isinstance(fallback, Mapping) else None,
        },
    }


def aggregate_execution_metadata(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate page traces for document events, sidecars, and artifacts."""

    stages: list[dict[str, Any]] = []
    fallback_reasons: set[str] = set()
    fallback_sources: set[str] = set()
    fallback_used = False

    for row in rows:
        metadata = execution_metadata(row)
        page_no = row.get("page_no")
        for stage in metadata["stages"]:
            if not isinstance(stage, Mapping):
                continue
            entry = dict(stage)
            if page_no is not None:
                entry["page_no"] = page_no
            stages.append(entry)
        fallback = metadata["fallback"]
        if fallback["used"]:
            fallback_used = True
            if fallback["reason"]:
                fallback_reasons.add(str(fallback["reason"]))
            if fallback["source_stage"]:
                fallback_sources.add(str(fallback["source_stage"]))

    def collapse(values: set[str]) -> Optional[str]:
        if not values:
            return None
        if len(values) == 1:
            return next(iter(values))
        return "multiple"

    return {
        "stages": stages,
        "fallback": {
            "used": fallback_used,
            "reason": collapse(fallback_reasons),
            "source_stage": collapse(fallback_sources),
        },
    }
