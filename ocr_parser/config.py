from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Dict, Optional


DEFAULT_MODEL_DIR = "/home/ocr_user/workspace/models"
DEFAULT_MAX_COMPLETION_TOKENS = 4096


def _coerce_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise ValueError(f"{name} must be a boolean")


def _coerce_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _coerce_float(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc


def _coerce_optional_int(name: str, value: Any) -> int | None:
    if value is None or value == "":
        return None
    return _coerce_int(name, value)


def _coerce_optional_str(name: str, value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _coerce_str(name: str, value: Any) -> str:
    if value is None:
        raise ValueError(f"{name} must be a string")
    return str(value)


_BOOL_OPTION_KEYS = {
    "enable_warmup",
    "enable_api_autotune",
    "mineru_skip_visual_block_recognition",
    "enable_resume",
    "force_reprocess",
    "save_page_json",
    "save_page_layout",
    "add_page_tag",
    "filter_duplicates",
    "verbose",
    "run_data_index",
    "flatten_output",
    "no_warmup",
    "skip_blank_pages",
    "enable_table_screenshot",
    "enable_table_reparse",
    "skip_uncaptioned_images",
    "keep_page_header",
    "keep_page_footer",
    "skip_footnote",
    "filter_author_blocks",
    "disable_badcase_collection",
    "filter_qr_barcodes",
    "normalize_superscript",
    "trim_first_page_summary",
    "circuit_breaker_enabled",
    "disable_process_pool",
}

_INT_OPTION_KEYS = {
    "port",
    "max_completion_tokens",
    "dpi",
    "max_retries",
    "page_concurrency",
    "file_concurrency",
    "api_concurrency",
    "api_concurrency_start",
    "api_concurrency_max",
    "api_autotune_interval",
    "mineru_layout_reserved_api_slots",
    "mineru_recognition_api_concurrency",
    "mineru_max_blocks_per_page",
    "render_concurrency",
    "encode_concurrency",
    "postprocess_concurrency",
    "block_concurrency",
    "paddle_layout_concurrency",
    "paddle_block_backpressure_high_watermark",
    "paddle_block_backpressure_low_watermark",
    "num_cpu_workers",
    "md_gen_concurrency",
    "queue_size",
    "index_page_limit",
    "concurrent_retries",
    "table_ocr_max_retries",
    "metrics_port",
    "circuit_breaker_threshold",
}

_FLOAT_OPTION_KEYS = {
    "gpu_memory_limit_gb",
    "temperature",
    "top_p",
    "timeout",
    "retry_delay",
    "blank_white_threshold",
    "blank_noise_threshold",
    "mineru_min_block_area_ratio",
    "table_ocr_retry_delay",
    "circuit_breaker_recovery",
}

_OPTIONAL_INT_OPTION_KEYS = {
    "min_pixels",
    "max_pixels",
}

_STRING_OPTION_KEYS = {
    "engine",
    "engine_config",
    "layout_detection_url",
    "model_dir",
    "ip",
    "model_name",
    "output_dir",
    "job_id",
    "job_event_file",
    "api_key",
    "keyword_filter_config",
    "badcase_collection_dir",
    "table_ocr_backend",
    "table_ocr_server_url",
    "table_ocr_device",
    "api_key_env_var",
}

_PARSER_OPTION_KEYS = (
    _BOOL_OPTION_KEYS
    | _INT_OPTION_KEYS
    | _FLOAT_OPTION_KEYS
    | _OPTIONAL_INT_OPTION_KEYS
    | _STRING_OPTION_KEYS
)


@dataclass
class ParserConfig:
    engine: str = "dotsocr"
    engine_config: Optional[str] = None
    layout_detection_url: str = "http://localhost:30002"
    paddle_layout_concurrency: int = 0
    paddle_block_backpressure_high_watermark: int = 0
    paddle_block_backpressure_low_watermark: int = 0
    block_concurrency: int = 0
    model_dir: str = DEFAULT_MODEL_DIR
    gpu_memory_limit_gb: float = 60.0
    ip: str = "localhost"
    port: int = 8000
    model_name: str = "model"
    temperature: float = 0.1
    top_p: float = 0.9
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS
    dpi: int = 200
    output_dir: str = "./output"
    timeout: float = 900.0
    enable_warmup: bool = True
    max_retries: int = 3
    retry_delay: float = 1.0
    blank_white_threshold: float = 0.98
    blank_noise_threshold: float = 0.002
    page_concurrency: int = 24
    file_concurrency: int = 1
    api_concurrency: int = 0
    api_concurrency_start: int = 0
    api_concurrency_max: int = 0
    enable_api_autotune: bool = False
    api_autotune_interval: int = 5
    mineru_layout_reserved_api_slots: int = 1
    mineru_recognition_api_concurrency: int = 0
    mineru_min_block_area_ratio: float = 0.0
    mineru_max_blocks_per_page: int = 0
    mineru_skip_visual_block_recognition: bool = False
    render_concurrency: int = 0
    encode_concurrency: int = 0
    postprocess_concurrency: int = 0
    num_cpu_workers: int = 32
    md_gen_concurrency: int = 0
    queue_size: int = 300
    api_key: Optional[str] = None
    enable_resume: bool = True
    force_reprocess: bool = False
    job_id: str = ""
    job_event_file: Optional[str] = None
    save_page_json: bool = False
    save_page_layout: bool = False
    add_page_tag: bool = False
    filter_duplicates: bool = True
    min_pixels: Optional[int] = None
    max_pixels: Optional[int] = None
    verbose: bool = False
    run_data_index: bool = False
    index_page_limit: int = 0
    flatten_output: bool = False
    concurrent_retries: int = 4
    enable_table_screenshot: bool = False
    enable_table_reparse: bool = False
    skip_uncaptioned_images: bool = False
    keep_page_header: bool = False
    keep_page_footer: bool = False
    skip_footnote: bool = False
    filter_author_blocks: bool = False
    disable_badcase_collection: bool = False
    badcase_collection_dir: Optional[str] = None
    filter_qr_barcodes: bool = True
    keyword_filter_config: Optional[str] = None
    trim_first_page_summary: bool = False
    normalize_superscript: bool = False
    table_ocr_backend: Optional[str] = None
    table_ocr_server_url: Optional[str] = None
    table_ocr_max_retries: Optional[int] = None
    table_ocr_retry_delay: Optional[float] = None
    table_ocr_device: Optional[str] = None
    circuit_breaker_enabled: bool = True
    circuit_breaker_threshold: int = 5
    circuit_breaker_recovery: float = 30.0
    use_hf: bool = False
    debug_matching: bool = False
    init_process_pool: bool = True
    init_md_semaphore: bool = True

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> "ParserConfig":
        known_fields = {item.name for item in fields(cls) if item.init}
        unknown = sorted(str(key) for key in kwargs if str(key) not in known_fields)
        if unknown:
            joined = ", ".join(unknown)
            plural = "arguments" if len(unknown) != 1 else "argument"
            raise ValueError(f"unknown ParserConfig {plural}: {joined}")
        return cls(**kwargs)

    @classmethod
    def known_option_keys(cls) -> set[str]:
        return set(_PARSER_OPTION_KEYS)

    @classmethod
    def validate_option_dict(
        cls,
        options: Dict[str, Any],
        *,
        context: str = "parser options",
    ) -> Dict[str, Any]:
        if not isinstance(options, dict):
            raise ValueError(f"{context} must be a mapping")

        normalized: Dict[str, Any] = {}
        unknown = sorted(str(key) for key in options if str(key) not in _PARSER_OPTION_KEYS)
        if unknown:
            joined = ", ".join(unknown)
            plural = "keys" if len(unknown) != 1 else "key"
            raise ValueError(f"unknown {context} {plural}: {joined}")

        for raw_key, value in options.items():
            key = str(raw_key)
            if key in _BOOL_OPTION_KEYS:
                normalized[key] = _coerce_bool(key, value)
            elif key in _INT_OPTION_KEYS:
                normalized[key] = _coerce_int(key, value)
            elif key in _FLOAT_OPTION_KEYS:
                normalized[key] = _coerce_float(key, value)
            elif key in _OPTIONAL_INT_OPTION_KEYS:
                normalized[key] = _coerce_optional_int(key, value)
            elif key in _STRING_OPTION_KEYS:
                normalized[key] = _coerce_optional_str(key, value)
            else:
                normalized[key] = value
        return normalized

    def to_runtime_kwargs(self) -> Dict[str, Any]:
        return {
            item.name: getattr(self, item.name)
            for item in fields(self)
        }
