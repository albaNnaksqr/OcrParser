#!/usr/bin/env python3
"""Generate a deterministic deployment release manifest from a clean wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse


SCHEMA = "ocrparser.deployment-release/1"
CANONICAL_SOURCE_REPOSITORY = "https://github.com/albaNnaksqr/OcrParser.git"
TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.]+)?$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def wheel_provenance(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        payload = json.loads(archive.read("ocr_platform/_build_info.json"))
    if not isinstance(payload, dict):
        raise ValueError("wheel build provenance must be a JSON object")
    return payload


def build_manifest(
    *,
    tag: str,
    source_revision: str,
    source_repository: str,
    wheel_url: str,
    wheel_path: Path,
) -> dict[str, object]:
    if not TAG_RE.fullmatch(tag):
        raise ValueError("tag must use vMAJOR.MINOR.PATCH form")
    if not COMMIT_RE.fullmatch(source_revision):
        raise ValueError("source revision must be a full lowercase commit")
    if source_repository != CANONICAL_SOURCE_REPOSITORY:
        raise ValueError("source repository must be the canonical public repository")
    parsed_url = urlparse(wheel_url)
    if parsed_url.scheme != "https" or Path(parsed_url.path).name != wheel_path.name:
        raise ValueError("wheel URL must be https and end with the wheel filename")

    provenance = wheel_provenance(wheel_path)
    if provenance.get("source_revision") != source_revision:
        raise ValueError("wheel source revision does not match the manifest")
    if provenance.get("dirty") is not False:
        raise ValueError("wheel is not a clean release build")

    return {
        "schema": SCHEMA,
        "tag": tag,
        "source_revision": source_revision,
        "source_repository": source_repository,
        "release_build": True,
        "wheel": {
            "filename": wheel_path.name,
            "url": wheel_url,
            "sha256": hashlib.sha256(wheel_path.read_bytes()).hexdigest(),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--wheel-url", required=True)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = build_manifest(
            tag=args.tag,
            source_revision=args.source_revision,
            source_repository=args.source_repository,
            wheel_url=args.wheel_url,
            wheel_path=args.wheel,
        )
    except (OSError, ValueError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        print(f"error: cannot generate deployment manifest: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
