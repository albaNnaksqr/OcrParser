#!/usr/bin/env python3
"""Verify that a wheel was built cleanly from the expected tag commit."""

from __future__ import annotations

import argparse
import json
import subprocess
import zipfile
from pathlib import Path


def tag_commit(tag: str) -> str:
    return subprocess.check_output(
        ["git", "rev-list", "-n", "1", tag],
        text=True,
    ).strip()


def wheel_provenance(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read("ocr_platform/_build_info.json"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()

    payload = wheel_provenance(args.wheel)
    expected_revision = tag_commit(args.tag)
    actual_revision = str(payload.get("source_revision") or "")
    if payload.get("dirty") is not False:
        parser.error("release wheel is marked dirty")
    if actual_revision != expected_revision:
        parser.error(
            f"release wheel revision {actual_revision or 'missing'} does not match "
            f"{args.tag} commit {expected_revision}"
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
