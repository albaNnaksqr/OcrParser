from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {"succeeded", "failed", "stopped"}


def create_sample_pdf(path: Path) -> None:
    import fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 120), "Local production-like distributed walkthrough", fontsize=16)
    page.insert_text(
        (72, 160),
        "This PDF is processed through control, worker, shard, and OCR endpoint.",
        fontsize=11,
    )
    doc.save(path)
    doc.close()


def build_job_payload(args: argparse.Namespace) -> dict[str, Any]:
    input_dir = Path(args.shared_root) / "input"
    output_dir = Path(args.shared_root) / "output"
    manifest_root = Path(args.shared_root) / "manifests"
    payload = {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "engine": args.engine,
        "input_mode": "distributed_remote_folder_snapshot",
        "manifest_root": str(manifest_root),
        "allowed_server_ids": [args.worker_id],
        "target_files_per_shard": 1,
        "max_shard_attempts": 1,
        "ip": args.ocr_host,
        "port": args.ocr_port,
        "model_name": args.model_name,
        "page_concurrency": 1,
        "extra_args": {
            "file_concurrency": 1,
            "api_concurrency_start": 1,
            "api_concurrency_max": 1,
            "num_cpu_workers": 1,
            "render_concurrency": 1,
            "encode_concurrency": 1,
            "postprocess_concurrency": 1,
            "max_retries": 1,
            "retry_delay": 0.1,
            "timeout": 30,
            "max_completion_tokens": 512,
            "no_warmup": True,
            "disable_badcase_collection": True,
        },
    }
    if getattr(args, "disable_process_pool", False):
        payload["extra_args"]["disable_process_pool"] = True
    api_key_env_var = getattr(args, "api_key_env_var", None)
    if api_key_env_var:
        payload["extra_args"]["api_key_env_var"] = str(api_key_env_var)
    return payload


def request_json(
    *,
    control_url: str,
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    request = urllib.request.Request(
        control_url.rstrip("/") + path,
        data=body,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read().decode("utf-8")
        return json.loads(data) if data else {}


def compact_summary(summary: dict[str, Any], poll: int) -> dict[str, Any]:
    return {
        "poll": poll,
        "status": summary.get("status"),
        "stage": summary.get("lifecycle_stage"),
        "scan": summary.get("scan_status"),
        "files": [
            summary.get("completed_files"),
            summary.get("failed_files"),
            summary.get("total_files"),
        ],
        "shards": [
            summary.get("pending_shards"),
            summary.get("running_shards"),
            summary.get("succeeded_shards"),
            summary.get("failed_shards"),
            summary.get("total_shards"),
        ],
        "scan_units": [
            summary.get("pending_scan_units"),
            summary.get("running_scan_units"),
            summary.get("succeeded_scan_units"),
            summary.get("failed_scan_units"),
            summary.get("total_scan_units"),
        ],
        "worker_shards": summary.get("worker_shards"),
        "attention_shards": summary.get("attention_shards"),
        "last_event_at": summary.get("last_event_at"),
    }


def run_walkthrough(args: argparse.Namespace) -> int:
    shared_root = Path(args.shared_root)
    input_dir = shared_root / "input"
    output_dir = shared_root / "output"
    manifest_root = shared_root / "manifests"
    for path in (input_dir, output_dir, manifest_root):
        path.mkdir(parents=True, exist_ok=True)
    create_sample_pdf(input_dir / args.pdf_name)

    payload = build_job_payload(args)
    job = request_json(
        control_url=args.control_url,
        token=args.api_token,
        method="POST",
        path="/api/jobs",
        payload=payload,
    )
    job_id = str(job["id"])
    print(f"JOB_ID {job_id}")

    last: dict[str, Any] | None = None
    summary: dict[str, Any] = {}
    for poll in range(1, args.polls + 1):
        summary = request_json(
            control_url=args.control_url,
            token=args.api_token,
            method="GET",
            path=f"/api/jobs/{job_id}/summary",
        )
        compact = compact_summary(summary, poll)
        if compact != last:
            print("SUMMARY " + json.dumps(compact, ensure_ascii=False, sort_keys=True))
            last = compact
        if summary.get("status") in TERMINAL_STATUSES:
            break
        time.sleep(args.interval)
    else:
        print("TIMEOUT")

    final = request_json(
        control_url=args.control_url,
        token=args.api_token,
        method="GET",
        path=f"/api/jobs/{job_id}/summary",
    )
    print("FINAL_SUMMARY " + json.dumps(final, ensure_ascii=False, sort_keys=True))
    print("OUTPUT_FILES")
    for path in sorted(output_dir.rglob("*")):
        if path.is_file():
            print(f"{path.relative_to(output_dir)} {path.stat().st_size}")
    return 0 if final.get("status") == "succeeded" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit and poll a small distributed job against a local production-like stack."
    )
    parser.add_argument("--control-url", default="http://127.0.0.1:38080")
    parser.add_argument("--api-token", default="local-dev-token")
    parser.add_argument("--shared-root", required=True)
    parser.add_argument("--worker-id", default="local-worker-01")
    parser.add_argument("--engine", default="dotsocr")
    parser.add_argument("--ocr-host", default="127.0.0.1")
    parser.add_argument("--ocr-port", type=int, default=18000)
    parser.add_argument("--model-name", default="mock-ocr")
    parser.add_argument(
        "--api-key-env-var",
        help="Control-process environment variable used to resolve the OCR API key.",
    )
    parser.add_argument("--disable-process-pool", action="store_true")
    parser.add_argument("--pdf-name", default="walkthrough-sample.pdf")
    parser.add_argument("--polls", type=int, default=120)
    parser.add_argument("--interval", type=float, default=1.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_walkthrough(args)
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
