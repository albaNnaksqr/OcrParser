from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .contracts import ManifestItem
from .contracts.execution import execution_metadata

from .config import DEFAULT_MAX_COMPLETION_TOKENS, DEFAULT_MODEL_DIR, ParserConfig


@dataclass(frozen=True)
class CollectedInput:
    path: Path
    rel_parent: Path
    manifest_item: ManifestItem | None = None
    output_stem: str | None = None


class OCRArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        raw_args = list(sys.argv[1:] if args is None else args)
        parsed = super().parse_args(args, namespace)
        parsed._provided_options = self._provided_option_dests(raw_args)
        if parsed.rename and not parsed.input_file:
            self.error("--rename can only be used with --input_file")
        if parsed.input_manifest and parsed.flatten_output:
            self.error("--flatten_output cannot be used with --input_manifest")
        return parsed

    def _provided_option_dests(self, raw_args: List[str]) -> set[str]:
        provided = set()
        for action in self._actions:
            for option in action.option_strings:
                if option in raw_args or any(item.startswith(f"{option}=") for item in raw_args):
                    provided.add(action.dest)
                    break
        return provided


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _port(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return parsed


_ENGINE_CONFIG_KEYS = {
    "ip",
    "port",
    "model_name",
    "api_key",
    "timeout",
    "temperature",
    "top_p",
    "max_completion_tokens",
    "file_concurrency",
    "page_concurrency",
    "api_concurrency",
    "api_concurrency_start",
    "api_concurrency_max",
    "enable_api_autotune",
    "api_autotune_interval",
    "mineru_layout_reserved_api_slots",
    "mineru_recognition_api_concurrency",
    "mineru_min_block_area_ratio",
    "mineru_max_blocks_per_page",
    "mineru_skip_visual_block_recognition",
    "render_concurrency",
    "encode_concurrency",
    "postprocess_concurrency",
    "block_concurrency",
    "paddle_layout_concurrency",
    "paddle_block_backpressure_high_watermark",
    "paddle_block_backpressure_low_watermark",
    "layout_detection_url",
    "concurrent_retries",
    "max_retries",
    "retry_delay",
}

PARSER_PROFILES: Dict[str, Dict[str, Any]] = {
    "local": {
        "page_concurrency": 4,
        "api_concurrency_start": 4,
        "api_concurrency_max": 4,
        "render_concurrency": 4,
        "encode_concurrency": 4,
        "postprocess_concurrency": 4,
        "num_cpu_workers": 8,
        "skip_blank_pages": True,
    },
    "balanced": {
        "page_concurrency": 16,
        "api_concurrency_start": 16,
        "api_concurrency_max": 16,
        "render_concurrency": 8,
        "encode_concurrency": 8,
        "postprocess_concurrency": 8,
        "num_cpu_workers": 16,
        "skip_blank_pages": True,
    },
    "throughput": {
        "page_concurrency": 80,
        "file_concurrency": 8,
        "api_concurrency_start": 80,
        "api_concurrency_max": 80,
        "render_concurrency": 32,
        "encode_concurrency": 32,
        "postprocess_concurrency": 32,
        "num_cpu_workers": 56,
        "skip_blank_pages": True,
    },
}


def _load_engine_config(config_path: str | Path, engine_name: str) -> Dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"engine_config file not found: {path}")

    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    elif path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        raise ValueError("engine_config must be a .json, .yaml, or .yml file")

    if not isinstance(data, dict):
        raise ValueError("engine_config root must be a mapping")

    defaults = data.get("defaults", {}) or {}
    engine_map = data.get("engines", data)
    if not isinstance(defaults, dict) or not isinstance(engine_map, dict):
        raise ValueError("engine_config defaults and engines must be mappings")

    if engine_name in engine_map:
        engine_config = engine_map.get(engine_name) or {}
    else:
        engine_config = data if any(key in _ENGINE_CONFIG_KEYS for key in data) else {}

    if not isinstance(engine_config, dict):
        raise ValueError(f"engine_config for {engine_name!r} must be a mapping")

    merged = {**defaults, **engine_config}
    unknown = sorted(set(merged) - _ENGINE_CONFIG_KEYS)
    if unknown:
        raise ValueError(f"unknown engine_config key(s): {', '.join(unknown)}")
    return ParserConfig.validate_option_dict(merged, context="engine_config")


def _apply_engine_config(args, parser_kwargs: Dict[str, Any]) -> None:
    if not getattr(args, "engine_config", None):
        return
    engine_config = _load_engine_config(args.engine_config, args.engine)
    provided_options = getattr(args, "_provided_options", set())
    for key, value in engine_config.items():
        if key not in provided_options:
            parser_kwargs[key] = value


def _apply_profile_defaults(args, parser_kwargs: Dict[str, Any]) -> None:
    profile_name = getattr(args, "profile", None)
    if not profile_name:
        return
    profile = PARSER_PROFILES[profile_name]
    provided_options = getattr(args, "_provided_options", set())
    for key, value in profile.items():
        if key not in provided_options:
            if key == "skip_blank_pages":
                args.skip_blank_pages = value
            else:
                parser_kwargs[key] = value


def _emit_job_event(ocr, event_type: str, **payload: Any) -> None:
    event_writer = getattr(ocr, "event_writer", None)
    if event_writer is None:
        return
    try:
        event_writer.emit(event_type, **payload)
    except Exception as exc:
        console_write = getattr(ocr, "_console_write", None)
        if callable(console_write):
            console_write(f"Failed to emit OCR event {event_type}: {exc}", level="warning")


def _file_results_failed(ocr, results: Any) -> bool:
    contentful_success_statuses = {"success", "success_fallback_text", "success_fallback_image"}
    if not results:
        return True
    has_contentful_success = False
    for row in results:
        if row.get("error"):
            return True
        status = row.get("status")
        if not status:
            return True
        if status in contentful_success_statuses:
            has_contentful_success = True
        elif status != "skipped_blank":
            return True
    return not has_contentful_success


def _first_result_failure(results: Any) -> tuple[str | None, str | None]:
    if not results:
        return "parser_failed", "OCR produced no result rows"
    for row in results:
        if not isinstance(row, dict):
            continue
        failure_category = row.get("failure_category")
        error = row.get("error")
        status = row.get("status")
        if failure_category or error or status not in {None, "success", "success_fallback_text", "success_fallback_image", "skipped_blank"}:
            return (
                str(failure_category) if failure_category else None,
                str(error) if error else None,
            )
    return None, None


async def _api_autotune_loop(ocr, shutdown_event: asyncio.Event) -> None:
    interval = max(1, int(getattr(ocr, "api_autotune_interval", 5) or 5))
    while not shutdown_event.is_set():
        await asyncio.sleep(interval)
        if shutdown_event.is_set():
            break
        tuner = getattr(ocr, "autotune_api_concurrency", None)
        if not callable(tuner):
            continue
        result = await tuner()
        if result.get("changed"):
            snapshot_getter = getattr(ocr, "get_runtime_snapshot", None)
            runtime = snapshot_getter() if callable(snapshot_getter) else None
            _emit_job_event(ocr, "runtime_metrics", autotune=result, runtime=runtime)
            console_write = getattr(ocr, "_console_write", None)
            if callable(console_write):
                console_write(
                    f"[AUTOTUNE] api_concurrency {result['old_limit']} -> {result['new_limit']} "
                    f"(reason={result['reason']})",
                    level="always",
                )


def _read_execution_control_payload(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def _execution_control_loop(
    ocr,
    shutdown_event: asyncio.Event,
    control_file: str,
    interval_seconds: float,
) -> None:
    from . import runtime as runtime_ops

    path = Path(control_file)
    interval = max(0.5, float(interval_seconds or 1.0))
    last_payload: dict[str, Any] | None = None
    while not shutdown_event.is_set():
        payload = _read_execution_control_payload(path)
        if payload is not None and payload != last_payload:
            result = await runtime_ops.apply_execution_control_payload(ocr, payload)
            last_payload = dict(payload)
            if result.get("changed"):
                snapshot_getter = getattr(ocr, "get_runtime_snapshot", None)
                runtime = snapshot_getter() if callable(snapshot_getter) else None
                _emit_job_event(ocr, "runtime_metrics", execution_control=result, runtime=runtime)
                console_write = getattr(ocr, "_console_write", None)
                if callable(console_write):
                    console_write(
                        "[CONTROL] "
                        f"paused={result['paused']} api_concurrency_limit={result['api_concurrency_limit']} "
                        f"reason={result['reason']}",
                        level="always",
                    )
        await asyncio.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = OCRArgumentParser(description="Modular Dots.OCR PDF parser")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--input_dir", type=str)
    input_group.add_argument("--input_file", type=str)
    input_group.add_argument("--input_manifest", type=str)

    parser.add_argument("--input_root", type=str, default=None)
    parser.add_argument("--output_dir", type=str, default="./output")
    parser.add_argument(
        "--profile",
        choices=sorted(PARSER_PROFILES),
        default=None,
        help="Apply named parser defaults before engine_config and explicit CLI flags",
    )
    parser.add_argument("--job_id", type=str, default="")
    parser.add_argument("--job_event_file", type=str, default=None)
    parser.add_argument("--flatten_output", action="store_true")
    parser.add_argument("--rename", type=str, default=None)
    parser.add_argument("--prompt", type=str, default="prompt_layout_all_en")
    parser.add_argument("--skip_blank_pages", action="store_true")
    parser.add_argument("--ip", type=str, default="localhost")
    parser.add_argument("--port", type=_port, default=8000)
    parser.add_argument("--model_name", type=str, default="model")
    parser.add_argument("--engine", choices=["dotsocr", "mineru", "paddleocr-vl"], default="dotsocr")
    parser.add_argument("--engine_config", type=str, default=None)
    parser.add_argument("--layout_detection_url", type=str, default="http://localhost:30002",
                        help="Layout detection service URL (PaddleOCR-VL two-stage mode)")
    parser.add_argument("--paddle_layout_concurrency", type=_non_negative_int, default=0,
                        help="Max concurrent PaddleOCR-VL layout /detect calls (0 = page_concurrency)")
    parser.add_argument("--paddle_block_backpressure_high_watermark", type=_non_negative_int, default=0,
                        help="Pause PaddleOCR-VL layout when pending recognition blocks reach this value (0 = disabled)")
    parser.add_argument("--paddle_block_backpressure_low_watermark", type=_non_negative_int, default=0,
                        help="Resume PaddleOCR-VL layout when pending recognition blocks fall to this value")
    parser.add_argument("--block_concurrency", type=_non_negative_int, default=0,
                        help="Max concurrent per-block VLM calls (0 = unlimited, for single-node use)")
    parser.add_argument("--model_dir", type=str, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--gpu_memory_limit_gb", type=float, default=60.0)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--max_completion_tokens", type=int, default=DEFAULT_MAX_COMPLETION_TOKENS)
    parser.add_argument("--num_cpu_workers", type=int, default=32)
    parser.add_argument(
        "--disable_process_pool",
        action="store_true",
        help="Use the default executor instead of a dedicated process pool (smoke tests and constrained hosts)",
    )
    parser.add_argument("--page_concurrency", type=_positive_int, default=24)
    parser.add_argument("--file_concurrency", type=_positive_int, default=1,
                        help="Max concurrent files for --input_dir (default: 1)")
    parser.add_argument("--api_concurrency", type=_non_negative_int, default=0,
                        help="OpenAI-compatible model API concurrency (0 = page_concurrency)")
    parser.add_argument("--api_concurrency_start", type=_non_negative_int, default=0,
                        help="Initial OpenAI-compatible model API concurrency (0 = api_concurrency_max)")
    parser.add_argument("--api_concurrency_max", type=_non_negative_int, default=0,
                        help="Maximum OpenAI-compatible model API concurrency (0 = api_concurrency or page_concurrency)")
    parser.add_argument("--enable_api_autotune", action="store_true",
                        help="Dynamically raise/lower API concurrency during the run")
    parser.add_argument("--api_autotune_interval", type=_positive_int, default=5,
                        help="Seconds between API concurrency autotune checks")
    parser.add_argument("--execution_control_file", type=str, default=None,
                        help="JSON file used by the platform agent to pause or lower API concurrency at runtime")
    parser.add_argument("--execution_control_poll_interval_seconds", type=float, default=2.0,
                        help="Seconds between execution control file checks")
    parser.add_argument("--mineru_layout_reserved_api_slots", type=_non_negative_int, default=1,
                        help="Global API slots reserved for MinerU layout requests")
    parser.add_argument("--mineru_recognition_api_concurrency", type=_non_negative_int, default=0,
                        help="MinerU recognition API concurrency (0 = api limit minus reserved layout slots)")
    parser.add_argument("--mineru_min_block_area_ratio", type=float, default=0.0,
                        help="Skip MinerU blocks smaller than this normalized page area ratio")
    parser.add_argument("--mineru_max_blocks_per_page", type=_non_negative_int, default=0,
                        help="Maximum MinerU blocks to recognize per page (0 = unlimited)")
    parser.add_argument("--mineru_skip_visual_block_recognition", action="store_true",
                        help="Save MinerU image/chart crops without calling VLM recognition for them")
    parser.add_argument("--render_concurrency", type=_non_negative_int, default=0,
                        help="PDF render/preprocess concurrency (0 = auto)")
    parser.add_argument("--encode_concurrency", type=_non_negative_int, default=0,
                        help="Image payload encoding concurrency (0 = auto)")
    parser.add_argument("--postprocess_concurrency", type=_non_negative_int, default=0,
                        help="Page-level OCR post-processing concurrency (0 = auto)")
    parser.add_argument("--md_gen_concurrency", type=int, default=0)
    parser.add_argument("--queue_size", type=int, default=300)
    parser.add_argument("--dpi", type=_positive_int, default=200)
    parser.add_argument("--min_pixels", type=int, default=None)
    parser.add_argument("--max_pixels", type=int, default=None)
    parser.add_argument("--max_retries", type=int, default=8)
    parser.add_argument("--retry_delay", type=float, default=2.0)
    parser.add_argument("--blank_white_threshold", type=float, default=0.98)
    parser.add_argument("--blank_noise_threshold", type=float, default=0.002)
    parser.add_argument("--api_key", type=str, default=None)
    parser.add_argument("--no_warmup", action="store_true")
    parser.add_argument("--save_page_json", action="store_true")
    parser.add_argument("--save_page_layout", action="store_true")
    parser.add_argument("--enable_table_screenshot", action="store_true")
    parser.add_argument("--enable_table_reparse", action="store_true")
    parser.add_argument("--table_ocr_backend", type=str, default=None)
    parser.add_argument("--table_ocr_server_url", type=str, default=None)
    parser.add_argument("--table_ocr_max_retries", type=int, default=None)
    parser.add_argument("--table_ocr_retry_delay", type=float, default=None)
    parser.add_argument("--table_ocr_device", type=str, default=None)
    parser.add_argument("--skip_uncaptioned_images", action="store_true")
    parser.add_argument("--keep_page_header", action="store_true")
    parser.add_argument("--keep_page_footer", action="store_true")
    parser.add_argument("--skip_footnote", action="store_true")
    parser.add_argument("--filter_author_blocks", action="store_true")
    parser.add_argument("--keyword_filter_config", type=str, default=None)
    parser.add_argument("--trim_first_page_summary", action="store_true")
    parser.add_argument("--concurrent_retries", type=int, default=4)
    parser.add_argument("--no_filter_qr_barcodes", dest="filter_qr_barcodes", action="store_false")
    parser.add_argument("--no_filter_duplicates", dest="filter_duplicates", action="store_false")
    parser.add_argument("--badcase_collection_dir", type=str, default=None)
    parser.add_argument("--disable_badcase_collection", action="store_true")
    parser.add_argument("--add_page_tag", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--normalize_superscript", action="store_true")
    parser.add_argument("--disable_resume", action="store_true")
    parser.add_argument("--force_reprocess", action="store_true")
    parser.add_argument(
        "--metrics_port", type=int, default=0,
        help="Port for Prometheus metrics HTTP server (0 = disabled, e.g. 9101)",
    )
    parser.add_argument(
        "--circuit_breaker_threshold", type=int, default=5,
        help="Consecutive whole-inference-chain failures before circuit opens (default: 5)",
    )
    parser.add_argument(
        "--circuit_breaker_recovery", type=float, default=30.0,
        help="Seconds to wait in OPEN state before probing again (default: 30.0)",
    )
    parser.add_argument(
        "--no_circuit_breaker", dest="circuit_breaker_enabled", action="store_false",
        help="Disable the circuit breaker",
    )
    return parser


def _scan_pdfs_recursive(root: Path) -> List[Tuple[Path, Path]]:
    """BFS scan returning (absolute_path, relative_parent) for every PDF under root."""
    results: List[Tuple[Path, Path]] = []
    dirs = [root]
    while dirs:
        current = dirs.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            dirs.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".pdf"):
                            fp = Path(entry.path)
                            try:
                                rel_parent = fp.relative_to(root).parent
                            except ValueError:
                                rel_parent = Path(".")
                            results.append((fp, rel_parent))
                    except OSError:
                        pass
        except OSError:
            pass
    results.sort()
    return results


def _collect_manifest_inputs(args) -> List[Tuple[Path, Path]]:
    """Return (absolute_pdf_path, relative_parent) pairs from a JSONL manifest."""
    results: List[Tuple[Path, Path]] = []
    manifest_path = Path(args.input_manifest)
    if not manifest_path.exists():
        return results
    seen_relative_paths: set[str] = set()
    with manifest_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = ManifestItem.from_json_line(line)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid manifest row {manifest_path} line {line_number}: {exc}") from exc
            relative_path = _validated_manifest_relative_path(
                manifest_path,
                line_number,
                item.relative_path,
                seen_relative_paths,
            )
            input_path = _validated_manifest_input_path(manifest_path, line_number, item.input_path)
            results.append((input_path, relative_path.parent))
    return results


def _validated_manifest_input_path(
    manifest_path: Path,
    line_number: int,
    input_path_value: str,
) -> Path:
    input_path = Path(input_path_value)
    if not input_path.is_absolute():
        raise ValueError(
            f"invalid manifest row {manifest_path} line {line_number} "
            f"input_path must be absolute: {input_path_value}"
        )
    return input_path.resolve()


def _validated_manifest_relative_path(
    manifest_path: Path,
    line_number: int,
    relative_path_value: str,
    seen_relative_paths: set[str],
) -> Path:
    if "\\" in relative_path_value:
        raise ValueError(
            f"invalid manifest row {manifest_path} line {line_number} "
            f"relative_path must use POSIX '/' separators: {relative_path_value}"
        )
    relative_path = Path(relative_path_value)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(
            f"invalid manifest row {manifest_path} line {line_number} "
            f"invalid relative_path must be relative and may not contain '..': {relative_path_value}"
        )
    if not relative_path.name or relative_path.suffix.lower() != ".pdf":
        raise ValueError(
            f"invalid manifest row {manifest_path} line {line_number} "
            f"relative_path must point to a PDF file: {relative_path_value}"
        )
    relative_key = relative_path.as_posix()
    if relative_key in seen_relative_paths:
        raise ValueError(
            f"invalid manifest row {manifest_path} duplicate relative_path "
            f"would overwrite output: {relative_key} line {line_number}"
        )
    seen_relative_paths.add(relative_key)
    return relative_path


def _collect_manifest_input_records(args) -> List[CollectedInput]:
    records: List[CollectedInput] = []
    manifest_path = Path(args.input_manifest)
    if not manifest_path.exists():
        return records
    seen_relative_paths: set[str] = set()
    with manifest_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = ManifestItem.from_json_line(line)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid manifest row {manifest_path} line {line_number}: {exc}") from exc
            relative_path = _validated_manifest_relative_path(
                manifest_path,
                line_number,
                item.relative_path,
                seen_relative_paths,
            )
            input_path = _validated_manifest_input_path(manifest_path, line_number, item.input_path)
            records.append(
                CollectedInput(
                    path=input_path,
                    rel_parent=relative_path.parent,
                    manifest_item=item,
                    output_stem=relative_path.stem,
                )
            )
    return records


def _collect_inputs(args) -> List[Tuple[Path, Path]]:
    """Return list of (absolute_pdf_path, relative_parent_within_input_root) pairs."""
    if args.input_manifest:
        return _collect_manifest_inputs(args)
    if args.input_file:
        p = Path(args.input_file).resolve()
        return [(p, Path("."))]
    root = Path(args.input_dir).resolve()
    return _scan_pdfs_recursive(root)


def _collect_input_records(args) -> List[CollectedInput]:
    if args.input_manifest:
        return _collect_manifest_input_records(args)
    return [
        CollectedInput(path=path, rel_parent=rel_parent)
        for path, rel_parent in _collect_inputs(args)
    ]


def _validate_manifest_item_freshness(item: ManifestItem) -> tuple[bool, str | None, str | None]:
    path = Path(item.input_path)
    if not path.exists():
        return False, "input_missing", f"input file missing: {path}"
    try:
        stat = path.stat()
    except OSError as exc:
        return False, "input_invalid", str(exc)
    if int(stat.st_size) != int(item.size_bytes) or int(stat.st_mtime_ns) != int(item.mtime_ns):
        return False, "input_changed", (
            f"input file changed since manifest snapshot: {path} "
            f"(size {stat.st_size}!={item.size_bytes} or mtime {stat.st_mtime_ns}!={item.mtime_ns})"
        )
    return True, None, None


def _manifest_freshness_error_type(failure_category: str | None) -> str | None:
    return {
        "input_missing": "InputMissing",
        "input_changed": "InputChanged",
        "input_invalid": "InputInvalid",
    }.get(str(failure_category or ""))


def _classify_empty_inputs(args) -> str:
    if args.input_file and not Path(args.input_file).exists():
        return "input_missing"
    if args.input_dir and not Path(args.input_dir).exists():
        return "input_missing"
    if args.input_manifest and not Path(args.input_manifest).exists():
        return "input_missing"
    return "input_empty"


async def _run(args) -> int:
    from .parser import DotsOCRParser

    if args.metrics_port > 0:
        from .infra.metrics import start_metrics_server
        start_metrics_server(args.metrics_port)
        print(f"Prometheus metrics available at http://0.0.0.0:{args.metrics_port}/metrics")

    # Graceful shutdown: SIGTERM/SIGINT sets this event; the page producer stops
    # queuing new pages, in-flight pages finish, outputs are flushed normally.
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown() -> None:
        if not shutdown_event.is_set():
            print("\nShutdown signal received — finishing in-flight pages, then exiting cleanly.")
            shutdown_event.set()

    loop.add_signal_handler(signal.SIGTERM, _request_shutdown)
    loop.add_signal_handler(signal.SIGINT, _request_shutdown)

    parser_kwargs = vars(args).copy()
    parser_kwargs["enable_resume"] = not args.disable_resume
    parser_kwargs["enable_warmup"] = not args.no_warmup
    parser_kwargs["init_process_pool"] = not args.disable_process_pool
    parser_kwargs.pop("input_dir", None)
    parser_kwargs.pop("input_file", None)
    parser_kwargs.pop("input_manifest", None)
    parser_kwargs.pop("input_root", None)
    parser_kwargs.pop("profile", None)
    parser_kwargs.pop("rename", None)
    parser_kwargs.pop("prompt", None)
    parser_kwargs.pop("skip_blank_pages", None)
    parser_kwargs.pop("disable_resume", None)
    parser_kwargs.pop("no_warmup", None)
    parser_kwargs.pop("disable_process_pool", None)
    parser_kwargs.pop("metrics_port", None)
    parser_kwargs.pop("execution_control_file", None)
    parser_kwargs.pop("execution_control_poll_interval_seconds", None)
    parser_kwargs.pop("_provided_options", None)
    _apply_profile_defaults(args, parser_kwargs)
    _apply_engine_config(args, parser_kwargs)

    ocr = DotsOCRParser(**parser_kwargs)
    runtime = getattr(ocr, "runtime", None)
    if runtime is not None:
        runtime._shutdown_event = shutdown_event
    else:
        ocr._shutdown_event = shutdown_event
    _emit_job_event(
        ocr,
        "job_started",
        input_dir=args.input_dir,
        input_file=args.input_file,
        input_manifest=args.input_manifest,
        input_root=args.input_root,
        output_dir=args.output_dir,
        engine=args.engine,
    )
    inputs = _collect_input_records(args)
    if not inputs:
        message = "No PDF files found."
        print(message)
        _emit_job_event(
            ocr,
            "job_failed",
            output_dir=args.output_dir,
            error=message,
            failure_category=_classify_empty_inputs(args),
        )
        return 1

    output_root = Path(args.output_dir).resolve()
    flatten = bool(getattr(args, "flatten_output", False))

    job_failed = False
    file_failed = False
    file_failure_category = None
    file_failure_error = None
    job_error = None
    autotune_task = None
    execution_control_task = None
    try:
        await ocr.initialize()
        if args.execution_control_file:
            execution_control_task = asyncio.create_task(
                _execution_control_loop(
                    ocr,
                    shutdown_event,
                    args.execution_control_file,
                    args.execution_control_poll_interval_seconds,
                )
            )
        if getattr(ocr, "enable_api_autotune", False):
            autotune_task = asyncio.create_task(_api_autotune_loop(ocr, shutdown_event))
        file_semaphore = asyncio.Semaphore(max(1, int(getattr(args, "file_concurrency", 1) or 1)))

        async def _process_one_file(record: CollectedInput):
            started_monotonic = time.monotonic()
            if shutdown_event.is_set():
                print(f"Shutdown requested — skipping remaining file(s).")
                return None
            path = record.path
            rel_parent = record.rel_parent
            output_stem = record.output_stem or path.stem
            # Mirror input subdirectory structure in output unless --flatten_output.
            if flatten or rel_parent == Path("."):
                target_dir = output_root
            else:
                target_dir = output_root / rel_parent
            target_dir.mkdir(parents=True, exist_ok=True)
            async with file_semaphore:
                if shutdown_event.is_set():
                    print(f"Shutdown requested — skipping remaining file(s).")
                    return None
                if record.manifest_item is not None:
                    is_fresh, failure_category, error_message = _validate_manifest_item_freshness(
                        record.manifest_item
                    )
                    if not is_fresh:
                        from .infra.status_sidecar import write_status_sidecar

                        error_type = _manifest_freshness_error_type(failure_category)
                        write_status_sidecar(
                            parser=ocr,
                            save_dir=str(target_dir / output_stem),
                            input_path=str(path),
                            filename=output_stem,
                            status="failed",
                            error=error_message,
                            result=[
                                {
                                    "file_path": str(path),
                                    "filename": output_stem,
                                    "status": "failed",
                                    "error": error_message,
                                    "failure_category": failure_category,
                                    "error_type": error_type,
                                }
                            ],
                            duration_seconds=time.monotonic() - started_monotonic,
                            error_type=error_type,
                            manifest_input_size_bytes=record.manifest_item.size_bytes,
                            manifest_input_mtime_ns=record.manifest_item.mtime_ns,
                            manifest_relative_path=record.manifest_item.relative_path,
                        )
                        _emit_job_event(
                            ocr,
                            "file_failed",
                            file_path=str(path),
                            filename=output_stem,
                            status="failed",
                            error=error_message,
                            failure_category=failure_category,
                            **execution_metadata(None),
                        )
                        return [
                            {
                                "file_path": str(path),
                                "filename": output_stem,
                                "status": "failed",
                                "error": error_message,
                                "failure_category": failure_category,
                            }
                        ]
                    resume_policy = getattr(ocr, "resume_policy", None)
                    may_reuse_output = (
                        resume_policy.may_reuse_existing_output()
                        if resume_policy is not None
                        else getattr(ocr, "enable_resume", True)
                        and not getattr(ocr, "force_reprocess", False)
                    )
                    if may_reuse_output:
                        from .infra.resume import cleanup_incomplete_output_dir, is_file_already_processed

                        is_processed, md_path = is_file_already_processed(
                            path,
                            target_dir,
                            output_stem,
                            getattr(ocr, "_console_write", lambda message, level="info": None),
                            require_status_sidecar=True,
                            expected_input_size_bytes=record.manifest_item.size_bytes,
                            expected_input_mtime_ns=record.manifest_item.mtime_ns,
                            require_input_snapshot=True,
                            expected_manifest_relative_path=record.manifest_item.relative_path,
                        )
                        if is_processed:
                            _emit_job_event(
                                ocr,
                                "file_done",
                                file_path=str(path),
                                filename=output_stem,
                                status="skipped",
                                output_path=md_path,
                                error=None,
                                **execution_metadata(None),
                            )
                            return [
                                {
                                    "page_no": 0,
                                    "original_page_num": 1,
                                    "file_path": str(path),
                                    "output_md_path": md_path,
                                    "status": "success",
                                    "error": None,
                                    "filename": output_stem,
                                    "skipped": True,
                                }
                            ]
                        cleanup_incomplete_output_dir(
                            target_dir,
                            output_stem,
                            getattr(ocr, "_console_write", lambda message, level="info": None),
                        )
                return await ocr.parse_file(
                    str(path),
                    output_dir=str(target_dir),
                    prompt_mode=args.prompt,
                    skip_blank_pages=args.skip_blank_pages,
                    rename_to=(
                        output_stem
                        if record.manifest_item is not None
                        else args.rename if args.input_file else None
                    ),
                    manifest_input_size_bytes=(
                        record.manifest_item.size_bytes
                        if record.manifest_item is not None
                        else None
                    ),
                    manifest_input_mtime_ns=(
                        record.manifest_item.mtime_ns
                        if record.manifest_item is not None
                        else None
                    ),
                    manifest_relative_path=(
                        record.manifest_item.relative_path
                        if record.manifest_item is not None
                        else None
                    ),
                )

        if args.input_file or getattr(args, "file_concurrency", 1) <= 1:
            for record in inputs:
                result = await _process_one_file(record)
                if result is None:
                    break
                if _file_results_failed(ocr, result):
                    file_failed = True
                    if file_failure_category is None:
                        file_failure_category, file_failure_error = _first_result_failure(result)
        else:
            results = await asyncio.gather(*[_process_one_file(record) for record in inputs])
            for result in results:
                if result is not None and _file_results_failed(ocr, result):
                    file_failed = True
                    if file_failure_category is None:
                        file_failure_category, file_failure_error = _first_result_failure(result)
    except Exception as exc:
        job_failed = True
        job_error = str(exc)
        raise
    finally:
        if autotune_task is not None:
            autotune_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await autotune_task
        if execution_control_task is not None:
            execution_control_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution_control_task
        try:
            await ocr.shutdown()
        except Exception as exc:
            _emit_job_event(ocr, "job_failed", output_dir=args.output_dir, error=str(exc))
            raise

        if job_failed:
            _emit_job_event(ocr, "job_failed", output_dir=args.output_dir, error=job_error)
        elif shutdown_event.is_set():
            _emit_job_event(ocr, "job_stopped", output_dir=args.output_dir)
        elif file_failed:
            payload = {"output_dir": args.output_dir}
            if file_failure_category:
                payload["failure_category"] = file_failure_category
            if file_failure_error:
                payload["error"] = file_failure_error
            _emit_job_event(ocr, "job_failed", **payload)
        else:
            _emit_job_event(ocr, "job_done", output_dir=args.output_dir)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(_run(args))
