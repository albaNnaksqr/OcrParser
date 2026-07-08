#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ocr_parser.infra.output_audit import audit_manifest_outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit OCR output sidecars and declared artifacts for a manifest JSONL file.",
    )
    parser.add_argument("--manifest", required=True, help="Manifest or shard JSONL path.")
    parser.add_argument("--output-dir", required=True, help="OCR output root directory.")
    parser.add_argument(
        "--check-input",
        action="store_true",
        help="Also verify input file size and mtime still match the manifest snapshot.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=20,
        help="Maximum issue samples to include in the JSON report.",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="Optional maximum number of manifest rows to audit.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = audit_manifest_outputs(
        manifest_path=args.manifest,
        output_dir=args.output_dir,
        check_input=args.check_input,
        sample_limit=args.sample_limit,
        max_items=args.max_items,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
