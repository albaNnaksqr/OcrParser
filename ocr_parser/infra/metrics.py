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

CIRCUIT_BREAKER_STATE = Gauge(
    "ocr_circuit_breaker_open",
    "1 if circuit breaker is open (VLM considered down), 0 otherwise",
)


def start_metrics_server(port: int) -> None:
    """Start a Prometheus HTTP metrics server on the given port."""
    _start_http_server(port)
