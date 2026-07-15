from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from dots_ocr.utils.consts import ABSOLUTE_MAX_PIXELS, MIN_PIXELS

from .config import DEFAULT_MAX_COMPLETION_TOKENS, DEFAULT_MODEL_DIR, ParserConfig
from .runtime import ResizableAsyncLimiter


def _require_positive_int(name: str, value, default: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _require_non_negative_int(name: str, value, default: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def _require_port(value) -> int:
    port = _require_positive_int("port", value, 8000)
    if port > 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def configure_runtime(self, config: ParserConfig, console_write) -> None:
    from .infra.monitoring import PerformanceMonitor

    kwargs = config.to_runtime_kwargs()
    init_process_pool = kwargs.pop("init_process_pool", True)
    init_md_semaphore = kwargs.pop("init_md_semaphore", True)

    self.ip = kwargs.get("ip", "localhost")
    self.port = _require_port(kwargs.get("port", 8000))
    self.model_name = kwargs.get("model_name", "model")
    self.engine = kwargs.get("engine", "dotsocr")
    self.engine_config = kwargs.get("engine_config", None)
    self.layout_detection_url = kwargs.get("layout_detection_url", "http://localhost:30002")
    self.paddle_layout_concurrency = _require_non_negative_int(
        "paddle_layout_concurrency",
        kwargs.get("paddle_layout_concurrency", 0),
        0,
    )
    self.paddle_block_backpressure_high_watermark = _require_non_negative_int(
        "paddle_block_backpressure_high_watermark",
        kwargs.get("paddle_block_backpressure_high_watermark", 0),
        0,
    )
    self.paddle_block_backpressure_low_watermark = _require_non_negative_int(
        "paddle_block_backpressure_low_watermark",
        kwargs.get("paddle_block_backpressure_low_watermark", 0),
        0,
    )
    self.block_concurrency = _require_non_negative_int("block_concurrency", kwargs.get("block_concurrency", 0), 0)
    self.model_dir = kwargs.get("model_dir", DEFAULT_MODEL_DIR)
    try:
        self.gpu_memory_limit_gb = float(kwargs.get("gpu_memory_limit_gb", 60.0))
    except (TypeError, ValueError):
        self.gpu_memory_limit_gb = 60.0
    console_write(f"Engine: {self.engine}")
    console_write(f"Model directory: {self.model_dir}")
    console_write(f"GPU memory validation limit: {self.gpu_memory_limit_gb:g} GB")
    self.temperature = kwargs.get("temperature", 0.1)
    self.top_p = kwargs.get("top_p", 0.9)
    self.max_completion_tokens = kwargs.get("max_completion_tokens", DEFAULT_MAX_COMPLETION_TOKENS)
    self.mineru_layout_reserved_api_slots = _require_non_negative_int(
        "mineru_layout_reserved_api_slots",
        kwargs.get("mineru_layout_reserved_api_slots", 1),
        1,
    )
    self.mineru_recognition_api_concurrency = _require_non_negative_int(
        "mineru_recognition_api_concurrency",
        kwargs.get("mineru_recognition_api_concurrency", 0),
        0,
    )
    try:
        self.mineru_min_block_area_ratio = max(
            0.0,
            float(kwargs.get("mineru_min_block_area_ratio", 0.0) or 0.0),
        )
    except (TypeError, ValueError):
        self.mineru_min_block_area_ratio = 0.0
    self.mineru_max_blocks_per_page = _require_non_negative_int(
        "mineru_max_blocks_per_page",
        kwargs.get("mineru_max_blocks_per_page", 0),
        0,
    )
    self.mineru_skip_visual_block_recognition = bool(
        kwargs.get("mineru_skip_visual_block_recognition", False)
    )
    self.dpi = _require_positive_int("dpi", kwargs.get("dpi", 200), 200)
    self.output_dir = kwargs.get("output_dir", "./output")
    self.flatten_output = bool(kwargs.get("flatten_output", False))
    self.run_data_index = bool(kwargs.get("run_data_index", False))
    try:
        self.index_page_limit = max(0, int(kwargs.get("index_page_limit", 0) or 0))
    except (TypeError, ValueError):
        self.index_page_limit = 0

    self.min_pixels = kwargs.get("min_pixels", None)
    self.max_pixels = kwargs.get("max_pixels", None)
    if self.max_pixels is None:
        self.max_pixels = ABSOLUTE_MAX_PIXELS
    elif isinstance(self.max_pixels, (int, float)):
        if self.max_pixels <= 0:
            console_write("Max pixel guard disabled via non-positive value; enforcing absolute ceiling instead.", level="warning")
            self.max_pixels = ABSOLUTE_MAX_PIXELS
        else:
            if self.max_pixels > ABSOLUTE_MAX_PIXELS:
                console_write(
                    f"Requested max_pixels={self.max_pixels} exceeds absolute ceiling {ABSOLUTE_MAX_PIXELS}; clamping to safe limit.",
                    level="warning",
                )
            self.max_pixels = min(int(self.max_pixels), ABSOLUTE_MAX_PIXELS)
    else:
        self.max_pixels = ABSOLUTE_MAX_PIXELS

    self.timeout = kwargs.get("timeout", 900.0)
    self.use_hf = kwargs.get("use_hf", False)
    self.enable_warmup = kwargs.get("enable_warmup", True)
    self.max_retries = kwargs.get("max_retries", 3)
    self.retry_delay = kwargs.get("retry_delay", 1.0)
    self.debug_matching = kwargs.get("debug_matching", False)
    self.blank_white_threshold = kwargs.get("blank_white_threshold", 0.98)
    self.blank_noise_threshold = kwargs.get("blank_noise_threshold", 0.002)
    self.page_concurrency = _require_positive_int("page_concurrency", kwargs.get("page_concurrency", 24), 24)
    self.file_concurrency = _require_positive_int("file_concurrency", kwargs.get("file_concurrency", 1), 1)
    requested_api_concurrency = _require_non_negative_int("api_concurrency", kwargs.get("api_concurrency", 0), 0)
    requested_api_concurrency_max = _require_non_negative_int(
        "api_concurrency_max", kwargs.get("api_concurrency_max", 0), 0
    )
    requested_api_concurrency_start = _require_non_negative_int(
        "api_concurrency_start", kwargs.get("api_concurrency_start", 0), 0
    )
    requested_render_concurrency = _require_non_negative_int("render_concurrency", kwargs.get("render_concurrency", 0), 0)
    requested_encode_concurrency = _require_non_negative_int("encode_concurrency", kwargs.get("encode_concurrency", 0), 0)
    requested_postprocess_concurrency = _require_non_negative_int(
        "postprocess_concurrency", kwargs.get("postprocess_concurrency", 0), 0
    )
    self.enable_api_autotune = bool(kwargs.get("enable_api_autotune", False))
    self.api_autotune_interval = _require_positive_int(
        "api_autotune_interval", kwargs.get("api_autotune_interval", 5), 5
    )
    self.num_cpu_workers = kwargs.get("num_cpu_workers", 32)
    num_workers = self.num_cpu_workers if self.num_cpu_workers > 0 else max(os.cpu_count() // 2, 32)

    self.api_concurrency_max = (
        requested_api_concurrency_max
        if requested_api_concurrency_max > 0
        else requested_api_concurrency
        if requested_api_concurrency > 0
        else self.page_concurrency
    )
    self.api_concurrency_start = (
        min(requested_api_concurrency_start, self.api_concurrency_max)
        if requested_api_concurrency_start > 0
        else self.api_concurrency_max
    )
    self.api_concurrency = self.api_concurrency_max
    self.render_concurrency = (
        requested_render_concurrency
        if requested_render_concurrency > 0
        else max(1, min(num_workers, self.page_concurrency))
    )
    self.encode_concurrency = (
        requested_encode_concurrency
        if requested_encode_concurrency > 0
        else max(1, min(num_workers, self.api_concurrency, 32))
    )

    md_gen_concurrency = kwargs.get("md_gen_concurrency", 0)
    if md_gen_concurrency <= 0:
        num_workers_for_md = self.num_cpu_workers if self.num_cpu_workers > 0 else max(os.cpu_count() // 2, 32)
        self.md_gen_concurrency = num_workers_for_md
    else:
        self.md_gen_concurrency = md_gen_concurrency

    if init_md_semaphore:
        self.md_gen_semaphore = asyncio.Semaphore(self.md_gen_concurrency)
        console_write(f"MD Generation Concurrency set to: {self.md_gen_concurrency}")
    else:
        self.md_gen_semaphore = None

    self.postprocess_concurrency = (
        requested_postprocess_concurrency
        if requested_postprocess_concurrency > 0
        else max(1, min(num_workers, self.md_gen_concurrency if self.md_gen_concurrency > 0 else num_workers))
    )

    self.queue_size = kwargs.get("queue_size", 300)
    self.api_key = kwargs.get("api_key") or os.environ.get("API_KEY", "0")
    self.enable_resume = kwargs.get("enable_resume", True)
    self.force_reprocess = kwargs.get("force_reprocess", False)
    self.job_id = kwargs.get("job_id", "")
    self.job_event_file = kwargs.get("job_event_file", None)
    from .infra.events import build_event_writer

    self.event_writer = build_event_writer(self.job_event_file, self.job_id)
    self.concurrent_retries = kwargs.get("concurrent_retries", 4)
    self.enable_table_screenshot = kwargs.get("enable_table_screenshot", False)
    self.enable_table_reparse = kwargs.get("enable_table_reparse", False)
    self.skip_uncaptioned_images = kwargs.get("skip_uncaptioned_images", False)
    self.disable_badcase_collection = bool(kwargs.get("disable_badcase_collection", False))
    badcase_dir = kwargs.get("badcase_collection_dir", None)
    self.badcase_collection_dir = None if self.disable_badcase_collection else badcase_dir
    self.keep_page_header = kwargs.get("keep_page_header", False)
    self.keep_page_footer = kwargs.get("keep_page_footer", False)
    self.skip_footnote = kwargs.get("skip_footnote", False)
    self.filter_author_blocks = kwargs.get("filter_author_blocks", False)
    self.save_page_json = kwargs.get("save_page_json", False)
    self.save_page_layout = kwargs.get("save_page_layout", False)
    self.add_page_tag = kwargs.get("add_page_tag", False)
    self.filter_qr_barcodes = kwargs.get("filter_qr_barcodes", True)
    self.filter_duplicates = kwargs.get("filter_duplicates", True)
    self.trim_first_page_summary = kwargs.get("trim_first_page_summary", False)
    self.client = None
    self.monitor = PerformanceMonitor()
    self.verbose = kwargs.get("verbose", False)
    self.keyword_filter_config = kwargs.get("keyword_filter_config", None)
    self.filter_keywords, self.categories_to_filter = self._load_filter_keywords()
    self.normalize_superscript = kwargs.get("normalize_superscript", False)
    self.generate_origin_md = bool(self.keyword_filter_config or self.normalize_superscript or self.trim_first_page_summary)
    self._table_reparse_stack = 0

    table_backend_default = os.getenv("TABLE_OCR_BACKEND")
    table_server_default = os.getenv("TABLE_OCR_SERVER_URL")
    table_retry_default = os.getenv("TABLE_OCR_MAX_RETRIES")
    table_retry_delay_default = os.getenv("TABLE_OCR_RETRY_DELAY")
    table_device_default = os.getenv("TABLE_OCR_DEVICE")
    self.table_ocr_backend = kwargs.get("table_ocr_backend", table_backend_default or "vllm-server")
    self.table_ocr_server_url = kwargs.get("table_ocr_server_url", table_server_default or "http://127.0.0.1:8118/v1")
    table_retries = kwargs.get("table_ocr_max_retries", table_retry_default)
    try:
        self.table_ocr_max_retries = max(1, int(table_retries))
    except (TypeError, ValueError):
        self.table_ocr_max_retries = 2
    retry_delay = kwargs.get("table_ocr_retry_delay", table_retry_delay_default)
    try:
        self.table_ocr_retry_delay = max(0.1, float(retry_delay))
    except (TypeError, ValueError):
        self.table_ocr_retry_delay = 1.5
    self.table_ocr_device = kwargs.get("table_ocr_device", table_device_default)
    self._table_ocr_pipeline_error = None
    self._table_ocr_pipeline_lock = threading.Lock()
    self._table_ocr_thread_local = threading.local()
    self._table_ocr_cls = None
    self._table_ocr_init_kwargs = None
    self._table_ocr_pipeline_ready_logged = False
    self._table_ocr_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="table-ocr")
    self._table_ocr_semaphore = asyncio.Semaphore(5)
    self._document_json_registry = {}
    self.init_kwargs = kwargs.copy()

    self.qr_scanner_available = False
    if self.filter_qr_barcodes:
        try:
            import cv2  # noqa: F401
            import numpy as np  # noqa: F401
            from pyzbar import pyzbar  # noqa: F401

            self.qr_scanner_available = True
            console_write("QR/barcode scanning dependencies (pyzbar, opencv) are available.")
        except ImportError:
            console_write(
                "WARNING: QR/barcode filtering is enabled, but 'pyzbar' or its dependency 'libzbar' is not installed. This feature will be disabled.",
                level="warning",
            )

    from .infra.circuit_breaker import CircuitBreaker

    cb_enabled = bool(kwargs.get("circuit_breaker_enabled", True))
    if cb_enabled:
        cb_threshold = int(kwargs.get("circuit_breaker_threshold", 5))
        cb_recovery = float(kwargs.get("circuit_breaker_recovery", 30.0))
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=cb_threshold,
            recovery_timeout=cb_recovery,
        )
        console_write(
            f"Circuit breaker: ENABLED (open after {cb_threshold} failures, "
            f"probe after {cb_recovery:.0f}s)"
        )
    else:
        self.circuit_breaker = None
        console_write("Circuit breaker: DISABLED")

    self.page_semaphore = asyncio.Semaphore(self.page_concurrency)
    self.api_limiter = ResizableAsyncLimiter(self.api_concurrency_start)
    self.api_semaphore = self.api_limiter
    self.render_semaphore = asyncio.Semaphore(self.render_concurrency)
    self.encode_semaphore = asyncio.Semaphore(self.encode_concurrency)
    self.postprocess_semaphore = asyncio.Semaphore(self.postprocess_concurrency)
    self._api_inflight = 0
    self._api_inflight_peak = 0
    self._api_waiting = 0
    self._api_call_count = 0
    self._api_wait_seconds_total = 0.0
    self._api_latency_seconds_total = 0.0
    self._api_latency_count = 0
    self._api_error_count = 0
    self._api_timeout_count = 0
    self._api_inflight_started = {}
    self._api_inflight_seq = 0
    self.api_autotune_last_error_count = 0
    self.api_autotune_last_timeout_count = 0
    if init_process_pool:
        console_write(f"Initializing ProcessPoolExecutor with {num_workers} workers.")
        self.process_pool = ProcessPoolExecutor(max_workers=num_workers)
        console_write("Using vLLM model with optimized producer-consumer model.")
        console_write(
            f"Global Page Concurrency: {self.page_concurrency} | API: {self.api_concurrency} | "
            f"API Start: {self.api_concurrency_start} | API Max: {self.api_concurrency_max} | "
            f"API Autotune: {'ON' if self.enable_api_autotune else 'OFF'} | "
            f"Render: {self.render_concurrency} | Encode: {self.encode_concurrency} | "
            f"Postprocess: {self.postprocess_concurrency} | CPU Workers: {num_workers} | Queue Size: {self.queue_size}"
        )
    else:
        self.process_pool = None

    console_write(f"Feature: Save page JSON -> {'ENABLED' if self.save_page_json else 'DISABLED'}")
    console_write(f"Feature: Save page layout image -> {'ENABLED' if self.save_page_layout else 'DISABLED'}")
    console_write(f"Feature: Table reparse -> {'ENABLED' if self.enable_table_reparse else 'DISABLED'}")

    assert self.min_pixels is None or self.min_pixels >= MIN_PIXELS
    assert self.max_pixels is None or self.max_pixels <= ABSOLUTE_MAX_PIXELS
