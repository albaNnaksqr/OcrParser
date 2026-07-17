from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram
from prometheus_client import start_http_server as _start_http_server

INFERENCE_DURATION = Histogram(
    "ocr_inference_duration_seconds",
    "VLM inference call latency in seconds",
    buckets=[0.5, 1, 2, 5, 10, 20, 30, 60, 120],
)

INFERENCE_REQUESTS = Counter(
    "ocr_inference_requests_total",
    "Total VLM inference calls",
    ["status"],  # success | error
)

RETRIES = Counter(
    "ocr_retries_total",
    "Total VLM retry events (sum of extra attempts)",
)

PAGES = Counter(
    "ocr_pages_total",
    "Total pages processed",
    ["status"],  # success | success_fallback_text | success_fallback_image | error | skipped_blank | ...
)

PAGES_IN_FLIGHT = Gauge(
    "ocr_pages_in_flight",
    "Number of pages currently being processed by the VLM",
)

DOCUMENTS = Counter(
    "ocr_documents_total",
    "Total documents processed",
    ["status"],  # success | error
)

ENGINE_STAGE_OUTCOMES = Counter(
    "ocr_engine_stage_outcomes_total",
    "Engine stage outcomes using bounded labels",
    ["engine", "stage", "status", "failure_category"],
)

ENGINE_FALLBACKS = Counter(
    "ocr_engine_fallbacks_total",
    "Engine fallback selections using bounded labels",
    ["engine", "source_stage", "reason"],
)

CIRCUIT_BREAKER_STATE = Gauge(
    "ocr_circuit_breaker_open",
    "1 if circuit breaker is open (VLM considered down), 0 otherwise",
)


def start_metrics_server(port: int) -> None:
    """Start a Prometheus HTTP metrics server on the given port."""
    _start_http_server(port)


_KNOWN_ENGINES = {"dotsocr", "mineru", "paddleocr-vl"}
_KNOWN_STAGES = {
    "layout",
    "recognition",
    "primary_inference",
    "postprocess",
    "text_fallback",
    "image_fallback",
    "single_stage_ocr",
    "output",
}
_KNOWN_STAGE_STATUSES = {"success", "failed", "skipped"}
_KNOWN_FAILURE_CATEGORIES = {
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
    "unknown",
    "other",
}
_KNOWN_FALLBACK_REASONS = {
    "layout_unavailable",
    "layout_empty",
    "layout_output_unusable",
    "primary_stage_failed",
    "text_fallback_unavailable",
    "multiple",
    "other",
}


def _bounded(value: object, known: set[str], *, default: str = "other") -> str:
    normalized = str(value or default)
    return normalized if normalized in known else default


def record_engine_execution_trace(engine: str, payload: dict) -> None:
    """Record trace metrics without allowing exception text into labels."""

    engine_label = _bounded(engine, _KNOWN_ENGINES)
    for stage in payload.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        failure = stage.get("failure_category") or "none"
        ENGINE_STAGE_OUTCOMES.labels(
            engine=engine_label,
            stage=_bounded(stage.get("stage"), _KNOWN_STAGES),
            status=_bounded(stage.get("status"), _KNOWN_STAGE_STATUSES),
            failure_category=_bounded(failure, _KNOWN_FAILURE_CATEGORIES),
        ).inc()

    fallback = payload.get("fallback")
    if isinstance(fallback, dict) and fallback.get("used"):
        ENGINE_FALLBACKS.labels(
            engine=engine_label,
            source_stage=_bounded(fallback.get("source_stage"), _KNOWN_STAGES),
            reason=_bounded(fallback.get("reason"), _KNOWN_FALLBACK_REASONS),
        ).inc()
