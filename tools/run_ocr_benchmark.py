#!/usr/bin/env python3
"""Run OCR parser benchmark fixtures and write CSV/Markdown summaries."""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


DEFAULT_PYTHON = Path("/Users/alabnnak/miniforge3/envs/ocrparser-310/bin/python")


def load_manifest(input_dir: Path) -> dict[str, dict]:
    manifest_path = input_dir / "manifest.json"
    return {
        item["filename"]: item
        for item in json.loads(manifest_path.read_text(encoding="utf-8"))
    }


def combined_markdown_path(output_base: Path, engine: str, pdf_stem: str) -> Path:
    dots_path = output_base / pdf_stem / f"{pdf_stem}.md"
    if engine == "dotsocr":
        return dots_path
    native_path = output_base / pdf_stem / "native" / engine / f"{pdf_stem}.md"
    return native_path if native_path.exists() else dots_path


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    index = max(0, int(len(values) * pct + 0.9999) - 1)
    return sorted(values)[index]


def read_api_key_from_stdin() -> str:
    key = sys.stdin.readline().strip()
    if not key:
        raise SystemExit("--api-key-stdin was set but stdin did not contain a key")
    return key


def run_one(
    *,
    python: Path,
    engine: str,
    engine_config: Path,
    pdf: Path,
    output_base: Path,
    log_path: Path,
    page_concurrency: int,
    timeout: float,
    max_retries: int,
    retry_delay: float,
    api_key: str | None,
    meta: dict,
) -> dict[str, str]:
    cmd = [
        str(python),
        "ocr_parser_cli.py",
        "--engine",
        engine,
        "--engine_config",
        str(engine_config),
        "--input_file",
        str(pdf),
        "--output_dir",
        str(output_base),
        "--num_cpu_workers",
        "1",
        "--md_gen_concurrency",
        "1",
        "--page_concurrency",
        str(page_concurrency),
        "--timeout",
        str(timeout),
        "--max_retries",
        str(max_retries),
        "--retry_delay",
        str(retry_delay),
        "--no_warmup",
        "--force_reprocess",
        "--disable_resume",
        "--flatten_output",
    ]

    env = os.environ.copy()
    if api_key is not None:
        env["API_KEY"] = api_key

    start = time.perf_counter()
    with log_path.open("w", encoding="utf-8") as log:
        prefix = "API_KEY=<redacted> " if api_key is not None else ""
        log.write("$ " + prefix + " ".join(cmd) + "\n\n")
        log.flush()
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, text=True, env=env)
    duration = time.perf_counter() - start

    combined = combined_markdown_path(output_base, engine, pdf.stem)
    ok = proc.returncode == 0 and combined.exists() and combined.stat().st_size > 0
    return {
        "engine": engine,
        "file": pdf.name,
        "pages": str(meta["pages"]),
        "category": meta["category"],
        "page_concurrency": str(page_concurrency),
        "exit_code": str(proc.returncode),
        "duration_s": f"{duration:.3f}",
        "seconds_per_page": f"{duration / max(meta['pages'], 1):.3f}",
        "status": "ok" if ok else "failed",
        "output_dir": str(output_base),
        "log_path": str(log_path),
        "combined_md_path": str(combined) if combined.exists() else "",
        "combined_md_bytes": str(combined.stat().st_size) if combined.exists() else "0",
    }


def write_summary(root: Path, rows: list[dict[str, str]], page_concurrency: int, api_key_used: bool) -> None:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["engine"], []).append(row)

    lines = [
        f"# OCR Benchmark c={page_concurrency}",
        "",
        f"Run directory: `{root}`",
        "",
        f"Settings: `page_concurrency={page_concurrency}`, `num_cpu_workers=1`, `md_gen_concurrency=1`, `no_warmup`, `force_reprocess`, `disable_resume`.",
    ]
    if api_key_used:
        lines.extend(["", "API key source: process environment only; logs are redacted."])
    lines.extend(
        [
            "",
            "| Engine | Files | Pages | OK | Total s | Avg s/page | p50 file s/page | p95 file s/page |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for engine, items in sorted(grouped.items()):
        total_pages = sum(int(item["pages"]) for item in items)
        total_s = sum(float(item["duration_s"]) for item in items)
        spp = [float(item["seconds_per_page"]) for item in items]
        ok = sum(1 for item in items if item["status"] == "ok")
        lines.append(
            f"| {engine} | {len(items)} | {total_pages} | {ok} | {total_s:.3f} | "
            f"{total_s / total_pages:.3f} | {statistics.median(spp):.3f} | {percentile(spp, 0.95):.3f} |"
        )

    lines.extend(["", "## File Results", "", "| Engine | File | Category | Pages | Duration s | s/page | Status |", "| --- | --- | --- | ---: | ---: | ---: | --- |"])
    for row in rows:
        lines.append(
            f"| {row['engine']} | `{row['file']}` | {row['category']} | {row['pages']} | "
            f"{float(row['duration_s']):.3f} | {float(row['seconds_per_page']):.3f} | {row['status']} |"
        )
    (root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run OCR benchmark PDFs.")
    parser.add_argument("--input-dir", type=Path, default=Path("data/benchmark_pdfs"))
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--engine-config", type=Path, default=Path(".local/ocr-engines.yaml"))
    parser.add_argument("--engines", nargs="+", required=True, choices=["dotsocr", "mineru", "paddleocr-vl"])
    parser.add_argument("--page-concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--retry-delay", type=float, default=0.1)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--api-key-stdin", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.page_concurrency <= 0:
        raise SystemExit("--page-concurrency must be positive")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = args.output_root or Path("data/benchmark_results") / f"benchmark_c{args.page_concurrency}_{timestamp}"
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(args.input_dir)
    pdfs = sorted(args.input_dir.glob("*.pdf"))
    api_key = read_api_key_from_stdin() if args.api_key_stdin else None

    rows: list[dict[str, str]] = []
    csv_path = root / "results.csv"
    fieldnames = [
        "engine",
        "file",
        "pages",
        "category",
        "page_concurrency",
        "exit_code",
        "duration_s",
        "seconds_per_page",
        "status",
        "output_dir",
        "log_path",
        "combined_md_path",
        "combined_md_bytes",
    ]
    for engine in args.engines:
        output_base = root / engine
        output_base.mkdir(parents=True, exist_ok=True)
        for pdf in pdfs:
            meta = manifest[pdf.name]
            log_path = logs_dir / f"{engine}__{pdf.stem}.log"
            print(f"START engine={engine} file={pdf.name} pages={meta['pages']}", flush=True)
            row = run_one(
                python=args.python,
                engine=engine,
                engine_config=args.engine_config,
                pdf=pdf,
                output_base=output_base,
                log_path=log_path,
                page_concurrency=args.page_concurrency,
                timeout=args.timeout,
                max_retries=args.max_retries,
                retry_delay=args.retry_delay,
                api_key=api_key,
                meta=meta,
            )
            rows.append(row)
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            print(
                f"DONE engine={engine} file={pdf.name} status={row['status']} "
                f"exit={row['exit_code']} duration_s={row['duration_s']}",
                flush=True,
            )

    write_summary(root, rows, args.page_concurrency, api_key_used=api_key is not None)
    print(f"WROTE {csv_path}", flush=True)
    print(f"WROTE {root / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
