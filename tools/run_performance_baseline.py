#!/usr/bin/env python3
"""Compare OCR performance across code variants on the same PDF fixtures."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


DEFAULT_PYTHON = Path("/Users/alabnnak/miniforge3/envs/ocrparser-310/bin/python")
API_KEY_ARGV_WRAPPER = (
    "import os, runpy, sys; "
    "entrypoint = sys.argv[1]; "
    "sys.argv = [entrypoint, *sys.argv[2:], '--api_key', os.environ['API_KEY']]; "
    "runpy.run_path(entrypoint, run_name='__main__')"
)
FIELDNAMES = [
    "variant",
    "kind",
    "engine",
    "run_mode",
    "file",
    "pages",
    "category",
    "file_concurrency",
    "page_concurrency",
    "exit_code",
    "duration_s",
    "seconds_per_page",
    "measured_pages_per_sec",
    "status",
    "output_dir",
    "log_path",
    "combined_md_path",
    "combined_md_bytes",
    "log_total_pages",
    "log_total_time_s",
    "log_inference_requests",
    "log_avg_inference_s",
    "log_throughput_pages_per_sec",
    "autotune_events",
    "concurrent_retry_events",
]

LOCAL_CONFIG_KEYS = {
    "DOTSOCR_API_KEY",
    "API_KEY",
    "DOTSOCR_IP",
    "DOTSOCR_PORT",
    "DOTSOCR_MODEL_NAME",
    "BENCHMARK_INPUT_DIR",
    "BENCHMARK_OUTPUT_ROOT",
    "BENCHMARK_RUN_MODE",
    "BENCHMARK_FILE_CONCURRENCY",
    "BENCHMARK_PAGE_CONCURRENCY",
    "BENCHMARK_NUM_CPU_WORKERS",
    "BENCHMARK_MD_GEN_CONCURRENCY",
    "BENCHMARK_TIMEOUT",
    "BENCHMARK_MAX_COMPLETION_TOKENS",
    "BENCHMARK_MAX_RETRIES",
    "BENCHMARK_RETRY_DELAY",
    "BENCHMARK_SAVE_PAGE_JSON",
    "BENCHMARK_SKIP_BLANK_PAGES",
}


@dataclass(frozen=True)
class Variant:
    name: str
    cwd: Path
    entrypoint: str = "ocr_parser_cli.py"
    kind: str = "modular"


def parse_variant_spec(value: str) -> Variant:
    """Parse NAME=/repo/path[:entrypoint]."""
    if "=" not in value:
        raise argparse.ArgumentTypeError("variant must be NAME=/path/to/repo[:entrypoint]")
    name, rest = value.split("=", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError("variant name cannot be empty")

    if ":" in rest:
        cwd_text, entrypoint = rest.rsplit(":", 1)
    else:
        cwd_text, entrypoint = rest, "ocr_parser_cli.py"

    cwd = Path(cwd_text).expanduser()
    entrypoint = entrypoint.strip() or "ocr_parser_cli.py"
    kind = "legacy" if re.match(r"parser_async_v\d+\.py$", Path(entrypoint).name) else "modular"
    return Variant(name=name, cwd=cwd, entrypoint=entrypoint, kind=kind)


def load_manifest(input_dir: Path) -> dict[str, dict]:
    manifest_path = input_dir / "manifest.json"
    if not manifest_path.exists():
        return {}
    return {
        item["filename"]: item
        for item in json.loads(manifest_path.read_text(encoding="utf-8"))
    }


def resolve_pdf_meta(pdf: Path, manifest: dict[str, dict]) -> dict:
    if pdf.name in manifest:
        return manifest[pdf.name]

    pages = 1
    try:
        import fitz

        with fitz.open(str(pdf)) as doc:
            pages = doc.page_count or 1
    except Exception:
        pages = 1
    return {"pages": pages, "category": ""}


def _strip_config_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_local_config(path: Optional[Path]) -> dict[str, str]:
    if path is None:
        return {}
    if not path.exists():
        raise SystemExit(f"local config file not found: {path}")

    config: dict[str, str] = {}
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"invalid local config line {line_no}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in LOCAL_CONFIG_KEYS:
            raise SystemExit(f"unknown local config key on line {line_no}: {key}")
        config[key] = _strip_config_quotes(value.strip())
    return config


def load_local_configs(paths: list[Path]) -> dict[str, str]:
    config: dict[str, str] = {}
    for path in paths:
        config.update(load_local_config(path))
    return config


def _config_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SystemExit(f"invalid boolean value in local config: {value!r}")


def apply_local_config(args: argparse.Namespace, config: dict[str, str]) -> None:
    api_key = config.get("DOTSOCR_API_KEY") or config.get("API_KEY")
    if api_key:
        args.api_key = api_key
    if "DOTSOCR_IP" in config:
        args.ip = config["DOTSOCR_IP"]
    if "DOTSOCR_PORT" in config:
        args.port = int(config["DOTSOCR_PORT"])
    if "DOTSOCR_MODEL_NAME" in config:
        args.model_name = config["DOTSOCR_MODEL_NAME"]
    if "BENCHMARK_INPUT_DIR" in config:
        args.input_dir = Path(config["BENCHMARK_INPUT_DIR"])
    if "BENCHMARK_OUTPUT_ROOT" in config:
        args.output_root = Path(config["BENCHMARK_OUTPUT_ROOT"])
    if "BENCHMARK_RUN_MODE" in config:
        args.run_mode = config["BENCHMARK_RUN_MODE"]
    if "BENCHMARK_FILE_CONCURRENCY" in config:
        args.file_concurrency = int(config["BENCHMARK_FILE_CONCURRENCY"])
    if "BENCHMARK_PAGE_CONCURRENCY" in config:
        args.page_concurrency = int(config["BENCHMARK_PAGE_CONCURRENCY"])
    if "BENCHMARK_NUM_CPU_WORKERS" in config:
        args.num_cpu_workers = int(config["BENCHMARK_NUM_CPU_WORKERS"])
    if "BENCHMARK_MD_GEN_CONCURRENCY" in config:
        args.md_gen_concurrency = int(config["BENCHMARK_MD_GEN_CONCURRENCY"])
    if "BENCHMARK_TIMEOUT" in config:
        args.timeout = float(config["BENCHMARK_TIMEOUT"])
    if "BENCHMARK_MAX_COMPLETION_TOKENS" in config:
        args.max_completion_tokens = int(config["BENCHMARK_MAX_COMPLETION_TOKENS"])
    if "BENCHMARK_MAX_RETRIES" in config:
        args.max_retries = int(config["BENCHMARK_MAX_RETRIES"])
    if "BENCHMARK_RETRY_DELAY" in config:
        args.retry_delay = float(config["BENCHMARK_RETRY_DELAY"])
    if "BENCHMARK_SAVE_PAGE_JSON" in config:
        args.save_page_json = _config_bool(config["BENCHMARK_SAVE_PAGE_JSON"])
    if "BENCHMARK_SKIP_BLANK_PAGES" in config:
        args.skip_blank_pages = _config_bool(config["BENCHMARK_SKIP_BLANK_PAGES"])


def build_command(
    *,
    variant: Variant,
    python: Path,
    pdf: Path,
    output_dir: Path,
    engine: str,
    engine_config: Optional[Path],
    ip: str,
    port: int,
    model_name: str,
    page_concurrency: int,
    num_cpu_workers: int,
    md_gen_concurrency: int,
    timeout: float,
    max_retries: int,
    retry_delay: float,
    max_completion_tokens: int,
    save_page_json: bool,
    skip_blank_pages: bool,
    api_key_from_env: bool = False,
) -> list[str]:
    command = [
        str(python),
        variant.entrypoint,
        "--input_file",
        str(pdf),
        "--output_dir",
        str(output_dir),
        "--ip",
        ip,
        "--port",
        str(port),
        "--model_name",
        model_name,
        "--timeout",
        str(timeout),
        "--max_completion_tokens",
        str(max_completion_tokens),
        "--page_concurrency",
        str(page_concurrency),
        "--num_cpu_workers",
        str(num_cpu_workers),
        "--md_gen_concurrency",
        str(md_gen_concurrency),
        "--max_retries",
        str(max_retries),
        "--retry_delay",
        str(retry_delay),
        "--no_warmup",
        "--force_reprocess",
        "--disable_resume",
    ]
    if api_key_from_env:
        command = [str(python), "-c", API_KEY_ARGV_WRAPPER, *command[1:]]
    if variant.kind == "modular":
        command.extend(["--engine", engine])
        if engine_config is not None:
            command.extend(["--engine_config", str(engine_config)])
        command.append("--flatten_output")
    if skip_blank_pages:
        command.append("--skip_blank_pages")
    if save_page_json:
        command.append("--save_page_json")
    return command


def build_directory_command(
    *,
    variant: Variant,
    python: Path,
    input_dir: Path,
    output_dir: Path,
    engine: str,
    engine_config: Optional[Path],
    ip: str,
    port: int,
    model_name: str,
    page_concurrency: int,
    file_concurrency: int,
    num_cpu_workers: int,
    md_gen_concurrency: int,
    timeout: float,
    max_retries: int,
    retry_delay: float,
    max_completion_tokens: int,
    save_page_json: bool,
    skip_blank_pages: bool,
    api_key_from_env: bool = False,
) -> list[str]:
    if variant.kind != "modular":
        raise ValueError("directory run mode is only supported for modular variants")

    command = [
        str(python),
        variant.entrypoint,
        "--input_dir",
        str(input_dir),
        "--output_dir",
        str(output_dir),
        "--engine",
        engine,
        "--ip",
        ip,
        "--port",
        str(port),
        "--model_name",
        model_name,
        "--timeout",
        str(timeout),
        "--max_completion_tokens",
        str(max_completion_tokens),
        "--file_concurrency",
        str(file_concurrency),
        "--page_concurrency",
        str(page_concurrency),
        "--num_cpu_workers",
        str(num_cpu_workers),
        "--md_gen_concurrency",
        str(md_gen_concurrency),
        "--max_retries",
        str(max_retries),
        "--retry_delay",
        str(retry_delay),
        "--no_warmup",
        "--force_reprocess",
        "--disable_resume",
    ]
    if engine_config is not None:
        command.extend(["--engine_config", str(engine_config)])
    if skip_blank_pages:
        command.append("--skip_blank_pages")
    if save_page_json:
        command.append("--save_page_json")
    if api_key_from_env:
        command = [str(python), "-c", API_KEY_ARGV_WRAPPER, *command[1:]]
    return command


def _first_float(pattern: str, text: str) -> Optional[float]:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return float(match.group(1)) if match else None


def _first_int(pattern: str, text: str) -> Optional[int]:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def extract_log_metrics(text: str) -> dict[str, str]:
    pages = _first_int(r"Total pages processed \(incl\. blank\):\s*(\d+)", text)
    total_time = _first_float(r"Total processing time:\s*([0-9.]+)s", text)
    inference_requests = _first_int(r"Total inference requests:\s*(\d+)", text)
    avg_inference = _first_float(r"Average inference time:\s*([0-9.]+)s", text)
    throughput = _first_float(r"Overall throughput:\s*([0-9.]+)\s*pages/sec", text)
    return {
        "log_total_pages": str(pages) if pages is not None else "",
        "log_total_time_s": f"{total_time:.3f}" if total_time is not None else "",
        "log_inference_requests": str(inference_requests) if inference_requests is not None else "",
        "log_avg_inference_s": f"{avg_inference:.3f}" if avg_inference is not None else "",
        "log_throughput_pages_per_sec": f"{throughput:.3f}" if throughput is not None else "",
        "autotune_events": str(len(re.findall(r"\[AUTOTUNE\]", text))),
        "concurrent_retry_events": str(len(re.findall(r"Concurrent retry", text, flags=re.IGNORECASE))),
    }


def find_markdown_output(output_dir: Path, pdf_stem: str) -> Optional[Path]:
    preferred = [
        output_dir / pdf_stem / f"{pdf_stem}.md",
        output_dir / f"{pdf_stem}.md",
    ]
    for path in preferred:
        if path.exists() and path.stat().st_size > 0:
            return path
    for path in sorted(output_dir.rglob("*.md")):
        try:
            if path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


def _has_failure_marker(log_text: str) -> bool:
    failure_markers = (
        "AuthenticationError",
        "Error code: 401",
        "failed all attempts",
        "No content could be generated",
    )
    return any(marker in log_text for marker in failure_markers)


def classify_run_status(exit_code: int, md_path: Optional[Path], log_text: str) -> str:
    if exit_code != 0 or md_path is None:
        return "failed"

    if _has_failure_marker(log_text):
        return "failed"

    return "ok"


def find_markdown_outputs(output_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(output_dir.rglob("*.md")):
        try:
            if path.stat().st_size > 0:
                paths.append(path)
        except OSError:
            continue
    return paths


def find_document_markdown_outputs(output_dir: Path) -> list[Path]:
    return [
        path
        for path in find_markdown_outputs(output_dir)
        if not path.stem.endswith("_origin")
    ]


def classify_directory_run_status(exit_code: int, output_dir: Path, log_text: str) -> str:
    if exit_code != 0 or not find_document_markdown_outputs(output_dir):
        return "failed"
    if _has_failure_marker(log_text):
        return "failed"

    return "ok"


def _read_api_key_from_stdin() -> str:
    value = sys.stdin.readline().strip()
    if not value:
        raise SystemExit("--api-key-stdin was set but stdin did not contain a key")
    return value


def run_one(
    *,
    variant: Variant,
    python: Path,
    pdf: Path,
    output_dir: Path,
    log_path: Path,
    engine: str,
    engine_config: Optional[Path],
    ip: str,
    port: int,
    model_name: str,
    page_concurrency: int,
    num_cpu_workers: int,
    md_gen_concurrency: int,
    timeout: float,
    max_retries: int,
    retry_delay: float,
    max_completion_tokens: int,
    save_page_json: bool,
    skip_blank_pages: bool,
    api_key: Optional[str],
    meta: dict,
) -> dict[str, str]:
    command = build_command(
        variant=variant,
        python=python,
        pdf=pdf,
        output_dir=output_dir,
        engine=engine,
        engine_config=engine_config,
        ip=ip,
        port=port,
        model_name=model_name,
        page_concurrency=page_concurrency,
        num_cpu_workers=num_cpu_workers,
        md_gen_concurrency=md_gen_concurrency,
        timeout=timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
        max_completion_tokens=max_completion_tokens,
        save_page_json=save_page_json,
        skip_blank_pages=skip_blank_pages,
        api_key_from_env=api_key is not None,
    )
    env = os.environ.copy()
    if api_key:
        env["API_KEY"] = api_key

    log_path.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    with log_path.open("w", encoding="utf-8") as log:
        prefix = "API_KEY=<redacted> " if api_key else ""
        log.write("$ " + prefix + " ".join(command) + "\n\n")
        log.flush()
        proc = subprocess.run(command, cwd=variant.cwd, stdout=log, stderr=subprocess.STDOUT, text=True, env=env)
    duration = time.perf_counter() - start

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    metrics = extract_log_metrics(log_text)
    md_path = find_markdown_output(output_dir, pdf.stem)
    pages = int(meta.get("pages") or 1)
    status = classify_run_status(proc.returncode, md_path, log_text)
    row = {
        "variant": variant.name,
        "kind": variant.kind,
        "engine": engine if variant.kind == "modular" else "dotsocr",
        "run_mode": "file",
        "file": pdf.name,
        "pages": str(pages),
        "category": str(meta.get("category", "")),
        "file_concurrency": "1",
        "page_concurrency": str(page_concurrency),
        "exit_code": str(proc.returncode),
        "duration_s": f"{duration:.3f}",
        "seconds_per_page": f"{duration / max(pages, 1):.3f}",
        "measured_pages_per_sec": f"{max(pages, 1) / duration:.3f}" if duration > 0 else "",
        "status": status,
        "output_dir": str(output_dir),
        "log_path": str(log_path),
        "combined_md_path": str(md_path) if md_path else "",
        "combined_md_bytes": str(md_path.stat().st_size) if md_path else "0",
    }
    row.update(metrics)
    return row


def run_directory(
    *,
    variant: Variant,
    python: Path,
    input_dir: Path,
    output_dir: Path,
    log_path: Path,
    engine: str,
    engine_config: Optional[Path],
    ip: str,
    port: int,
    model_name: str,
    page_concurrency: int,
    file_concurrency: int,
    num_cpu_workers: int,
    md_gen_concurrency: int,
    timeout: float,
    max_retries: int,
    retry_delay: float,
    max_completion_tokens: int,
    save_page_json: bool,
    skip_blank_pages: bool,
    api_key: Optional[str],
    total_pages: int,
) -> dict[str, str]:
    command = build_directory_command(
        variant=variant,
        python=python,
        input_dir=input_dir,
        output_dir=output_dir,
        engine=engine,
        engine_config=engine_config,
        ip=ip,
        port=port,
        model_name=model_name,
        page_concurrency=page_concurrency,
        file_concurrency=file_concurrency,
        num_cpu_workers=num_cpu_workers,
        md_gen_concurrency=md_gen_concurrency,
        timeout=timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
        max_completion_tokens=max_completion_tokens,
        save_page_json=save_page_json,
        skip_blank_pages=skip_blank_pages,
        api_key_from_env=api_key is not None,
    )
    env = os.environ.copy()
    if api_key:
        env["API_KEY"] = api_key

    log_path.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    with log_path.open("w", encoding="utf-8") as log:
        prefix = "API_KEY=<redacted> " if api_key else ""
        log.write("$ " + prefix + " ".join(command) + "\n\n")
        log.flush()
        proc = subprocess.run(command, cwd=variant.cwd, stdout=log, stderr=subprocess.STDOUT, text=True, env=env)
    duration = time.perf_counter() - start

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    metrics = extract_log_metrics(log_text)
    md_paths = find_document_markdown_outputs(output_dir)
    status = classify_directory_run_status(proc.returncode, output_dir, log_text)
    pages = max(total_pages, 1)
    row = {
        "variant": variant.name,
        "kind": variant.kind,
        "engine": engine,
        "run_mode": "directory",
        "file": input_dir.name or str(input_dir),
        "pages": str(pages),
        "category": "directory",
        "file_concurrency": str(file_concurrency),
        "page_concurrency": str(page_concurrency),
        "exit_code": str(proc.returncode),
        "duration_s": f"{duration:.3f}",
        "seconds_per_page": f"{duration / pages:.3f}",
        "measured_pages_per_sec": f"{pages / duration:.3f}" if duration > 0 else "",
        "status": status,
        "output_dir": str(output_dir),
        "log_path": str(log_path),
        "combined_md_path": "",
        "combined_md_bytes": str(sum(path.stat().st_size for path in md_paths)),
    }
    row.update(metrics)
    return row


def _pct(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    index = max(0, int(len(values) * pct + 0.9999) - 1)
    return sorted(values)[index]


def write_outputs(root: Path, rows: list[dict[str, str]], settings: str) -> None:
    csv_path = root / "results.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["variant"], []).append(row)

    lines = [
        "# OCR Performance Baseline",
        "",
        f"Run directory: `{root}`",
        "",
        f"Settings: `{settings}`",
        "",
        "| Variant | Files | Pages | OK | Total s | Avg s/page | Measured pages/s | p95 file s/page |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant, items in sorted(grouped.items()):
        total_pages = sum(int(item["pages"]) for item in items)
        total_s = sum(float(item["duration_s"]) for item in items)
        seconds_per_page = [float(item["seconds_per_page"]) for item in items]
        ok_count = sum(1 for item in items if item["status"] == "ok")
        measured = total_pages / total_s if total_s > 0 else 0.0
        lines.append(
            f"| {variant} | {len(items)} | {total_pages} | {ok_count} | {total_s:.3f} | "
            f"{(total_s / max(total_pages, 1)):.3f} | {measured:.3f} | {_pct(seconds_per_page, 0.95):.3f} |"
        )

    lines.extend(
        [
            "",
            "## File Results",
            "",
            "| Variant | Engine | Mode | File | Category | Pages | Duration s | s/page | Status | Log throughput |",
            "| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: |",
        ]
    )
    for row in rows:
        lines.append(
            f"| {row['variant']} | {row['engine']} | {row.get('run_mode', 'file')} | `{row['file']}` | {row['category']} | "
            f"{row['pages']} | {float(row['duration_s']):.3f} | {float(row['seconds_per_page']):.3f} | "
            f"{row['status']} | {row['log_throughput_pages_per_sec'] or ''} |"
        )
    (root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare OCR parser performance variants.")
    parser.add_argument("--variant", action="append", type=parse_variant_spec, required=True)
    parser.add_argument("--local-config", action="append", type=Path, default=[])
    parser.add_argument("--input-dir", type=Path, default=Path("data/benchmark_pdfs"))
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--run-mode", choices=["file", "directory"], default="file")
    parser.add_argument("--engine", default="dotsocr", choices=["dotsocr", "mineru", "paddleocr-vl"])
    parser.add_argument("--engine-config", type=Path, default=None)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--ip", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model-name", default="DotsOCR")
    parser.add_argument("--page-concurrency", type=int, default=16)
    parser.add_argument("--file-concurrency", type=int, default=1)
    parser.add_argument("--num-cpu-workers", type=int, default=4)
    parser.add_argument("--md-gen-concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--max-completion-tokens", dest="max_completion_tokens", type=int, default=16384)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--retry-delay", type=float, default=0.1)
    parser.add_argument("--save-page-json", dest="save_page_json", action="store_true")
    parser.add_argument("--skip-blank-pages", action="store_true")
    parser.add_argument("--api-key-stdin", action="store_true")
    parser.add_argument("--api-key", dest="api_key", default=None, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if args.run_mode not in {"file", "directory"}:
        raise SystemExit("--run-mode must be one of: file, directory")
    if args.run_mode == "directory":
        legacy_variants = [variant.name for variant in args.variant if variant.kind != "modular"]
        if legacy_variants:
            raise SystemExit(
                "directory run mode only supports modular variants; "
                f"unsupported: {', '.join(legacy_variants)}"
            )
    if args.page_concurrency <= 0:
        raise SystemExit("--page-concurrency must be positive")
    if args.file_concurrency <= 0:
        raise SystemExit("--file-concurrency must be positive")
    if args.num_cpu_workers <= 0:
        raise SystemExit("--num-cpu-workers must be positive")
    if args.md_gen_concurrency <= 0:
        raise SystemExit("--md-gen-concurrency must be positive")


def main() -> None:
    args = parse_args()
    apply_local_config(args, load_local_configs(args.local_config))
    validate_args(args)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = args.output_root or Path("data/benchmark_results") / f"perf_baseline_{timestamp}"
    logs_dir = root / "logs"
    manifest = load_manifest(args.input_dir)
    pdfs = sorted(args.input_dir.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"No PDFs found under {args.input_dir}")
    api_key = args.api_key or (_read_api_key_from_stdin() if args.api_key_stdin else None)

    rows: list[dict[str, str]] = []
    settings = (
        f"mode={args.run_mode}, engine={args.engine}, file_concurrency={args.file_concurrency}, "
        f"page_concurrency={args.page_concurrency}, num_cpu_workers={args.num_cpu_workers}, "
        f"md_gen_concurrency={args.md_gen_concurrency}"
    )
    if args.run_mode == "directory":
        total_pages = sum(int(resolve_pdf_meta(pdf, manifest).get("pages") or 1) for pdf in pdfs)
        for variant in args.variant:
            output_dir = root / "outputs" / variant.name
            log_path = logs_dir / f"{variant.name}__directory.log"
            print(f"START variant={variant.name} directory={args.input_dir}", flush=True)
            row = run_directory(
                variant=variant,
                python=args.python,
                input_dir=args.input_dir.resolve(),
                output_dir=output_dir.resolve(),
                log_path=log_path,
                engine=args.engine,
                engine_config=args.engine_config,
                ip=args.ip,
                port=args.port,
                model_name=args.model_name,
                page_concurrency=args.page_concurrency,
                file_concurrency=args.file_concurrency,
                num_cpu_workers=args.num_cpu_workers,
                md_gen_concurrency=args.md_gen_concurrency,
                timeout=args.timeout,
                max_retries=args.max_retries,
                retry_delay=args.retry_delay,
                max_completion_tokens=args.max_completion_tokens,
                save_page_json=args.save_page_json,
                skip_blank_pages=args.skip_blank_pages,
                api_key=api_key,
                total_pages=total_pages,
            )
            rows.append(row)
            write_outputs(root, rows, settings)
            print(
                f"DONE variant={variant.name} directory={args.input_dir} status={row['status']} "
                f"duration_s={row['duration_s']}",
                flush=True,
            )
    else:
        for variant in args.variant:
            for pdf in pdfs:
                meta = resolve_pdf_meta(pdf, manifest)
                output_dir = root / "outputs" / variant.name / pdf.stem
                log_path = logs_dir / f"{variant.name}__{pdf.stem}.log"
                print(f"START variant={variant.name} file={pdf.name}", flush=True)
                row = run_one(
                    variant=variant,
                    python=args.python,
                    pdf=pdf.resolve(),
                    output_dir=output_dir.resolve(),
                    log_path=log_path,
                    engine=args.engine,
                    engine_config=args.engine_config,
                    ip=args.ip,
                    port=args.port,
                    model_name=args.model_name,
                    page_concurrency=args.page_concurrency,
                    num_cpu_workers=args.num_cpu_workers,
                    md_gen_concurrency=args.md_gen_concurrency,
                    timeout=args.timeout,
                    max_retries=args.max_retries,
                    retry_delay=args.retry_delay,
                    max_completion_tokens=args.max_completion_tokens,
                    save_page_json=args.save_page_json,
                    skip_blank_pages=args.skip_blank_pages,
                    api_key=api_key,
                    meta=meta,
                )
                rows.append(row)
                write_outputs(root, rows, settings)
                print(
                    f"DONE variant={variant.name} file={pdf.name} status={row['status']} "
                    f"duration_s={row['duration_s']}",
                    flush=True,
                )

    print(f"WROTE {root / 'results.csv'}")
    print(f"WROTE {root / 'summary.md'}")


if __name__ == "__main__":
    main()
