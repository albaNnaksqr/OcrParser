#!/usr/bin/env python3
"""Validate machine-readable engine certification provenance."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


HEX_REVISION = re.compile(r"^[0-9a-f]{7,64}$")
SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("docs/engine-certification-records.json"),
    )
    args = parser.parse_args()
    payload = json.loads(args.path.read_text(encoding="utf-8"))
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        parser.error("certification records must be a non-empty list")
    engines: set[str] = set()
    for record in records:
        engine = str(record.get("engine") or "")
        if engine in engines or not engine:
            parser.error(f"missing or duplicate engine: {engine or '<empty>'}")
        engines.add(engine)
        parser_commit = str(record.get("parser_commit") or "")
        if not HEX_REVISION.fullmatch(parser_commit):
            parser.error(f"{engine}: invalid parser_commit")
        status = str(record.get("status") or "")
        if status == "certified":
            if not record.get("model_revision"):
                parser.error(f"{engine}: certified record requires model_revision")
            digest = str(record.get("runtime_digest") or "")
            if not SHA256_DIGEST.fullmatch(digest):
                parser.error(f"{engine}: certified record requires immutable runtime_digest")
    print(f"Validated {len(records)} engine certification records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
