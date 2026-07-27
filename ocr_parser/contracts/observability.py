"""Shared low-cardinality vocabulary for OCR observability labels."""

from __future__ import annotations

from .execution import STAGE_STATUSES


ENGINE_LABEL_VALUES = frozenset(
    {"dotsocr", "mineru", "paddleocr-vl", "other"}
)
STAGE_LABEL_VALUES = frozenset(
    {
        "layout",
        "recognition",
        "primary_inference",
        "postprocess",
        "text_fallback",
        "image_fallback",
        "single_stage_ocr",
        "output",
        "other",
    }
)
STATUS_LABEL_VALUES = frozenset(
    {
        *STAGE_STATUSES,
        "online",
        "idle",
        "busy",
        "offline",
        "queued",
        "running",
        "stopping",
        "pending",
        "retrying",
        "stale",
        "succeeded",
        "stopped",
        "success_fallback_text",
        "success_fallback_image",
        "skipped_blank",
        "other",
    }
)
FAILURE_CATEGORY_LABEL_VALUES = frozenset(
    {
        "none",
        "process_killed",
        "process_failed",
        "input_missing",
        "api_timeout",
        "model_unreachable",
        "model_output_invalid",
        "model_auth_failed",
        "model_rate_limited",
        "model_unavailable",
        "model_error",
        "resource_exhausted",
        "output_unwritable",
        "input_invalid",
        "artifact_missing",
        "input_changed",
        "parser_failed",
        "operator_stopped",
        "unknown",
        "other",
    }
)
FALLBACK_CATEGORY_LABEL_VALUES = frozenset(
    {
        "layout_unavailable",
        "layout_empty",
        "layout_output_unusable",
        "primary_stage_failed",
        "text_fallback_unavailable",
        "multiple",
        "other",
    }
)


def bounded_label(
    value: object,
    allowed: frozenset[str],
    *,
    absent: str = "other",
) -> str:
    normalized = str(value).strip() if value is not None else absent
    return normalized if normalized in allowed else "other"


def engine_label(value: object) -> str:
    return bounded_label(value, ENGINE_LABEL_VALUES)


def stage_label(value: object) -> str:
    return bounded_label(value, STAGE_LABEL_VALUES)


def status_label(value: object) -> str:
    return bounded_label(value, STATUS_LABEL_VALUES)


def failure_category_label(value: object) -> str:
    return bounded_label(
        value,
        FAILURE_CATEGORY_LABEL_VALUES,
        absent="none",
    )


def fallback_category_label(value: object) -> str:
    return bounded_label(value, FALLBACK_CATEGORY_LABEL_VALUES)


__all__ = [
    "ENGINE_LABEL_VALUES",
    "FAILURE_CATEGORY_LABEL_VALUES",
    "FALLBACK_CATEGORY_LABEL_VALUES",
    "STAGE_LABEL_VALUES",
    "STATUS_LABEL_VALUES",
    "bounded_label",
    "engine_label",
    "failure_category_label",
    "fallback_category_label",
    "stage_label",
    "status_label",
]
