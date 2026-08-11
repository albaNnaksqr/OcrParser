#!/usr/bin/env python3
"""Resolve a tag-only or complete explicit production release identity."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlparse
from urllib.request import urlopen


SCHEMA = "ocrparser.deployment-release/1"
CANONICAL_SOURCE_REPOSITORY = "https://github.com/albaNnaksqr/OcrParser.git"
DEFAULT_MANIFEST_URL = (
    "https://github.com/albaNnaksqr/OcrParser/releases/download/{tag}/"
    "deployment-manifest.json"
)
TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.]+)?$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_REPOSITORY_RE = re.compile(
    r"^(?:https://[^\s/]+/[^\s]+|ssh://[^\s/]+/[^\s]+|git@[^\s:]+:[^\s]+)$"
)


class ReleaseIdentityError(ValueError):
    """A release identity is partial, mixed, malformed, or unavailable."""


def _https_url(value: str, *, suffix: str | None = None) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and (
        suffix is None or parsed.path.endswith(suffix)
    )


def validate_manifest(payload: Any, *, expected_tag: str) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ReleaseIdentityError("deployment manifest must be a JSON object")
    if set(payload) != {
        "schema", "tag", "source_revision", "source_repository", "release_build", "wheel"
    }:
        raise ReleaseIdentityError("deployment manifest has missing or unknown top-level fields")
    wheel = payload.get("wheel")
    if not isinstance(wheel, dict) or set(wheel) != {"filename", "url", "sha256"}:
        raise ReleaseIdentityError("deployment manifest wheel object is malformed")
    if payload.get("schema") != SCHEMA or payload.get("tag") != expected_tag:
        raise ReleaseIdentityError("deployment manifest schema or tag does not match the request")
    if payload.get("release_build") is not True:
        raise ReleaseIdentityError("deployment manifest does not declare a clean release build")

    revision = payload.get("source_revision")
    repository = payload.get("source_repository")
    filename = wheel.get("filename")
    wheel_url = wheel.get("url")
    digest = wheel.get("sha256")
    if not isinstance(revision, str) or not COMMIT_RE.fullmatch(revision):
        raise ReleaseIdentityError("deployment manifest source revision is invalid")
    if repository != CANONICAL_SOURCE_REPOSITORY:
        raise ReleaseIdentityError("deployment manifest source repository is not canonical")
    if not isinstance(wheel_url, str) or not _https_url(wheel_url, suffix=".whl"):
        raise ReleaseIdentityError("deployment manifest wheel URL is invalid")
    if not isinstance(filename, str) or filename != Path(urlparse(wheel_url).path).name:
        raise ReleaseIdentityError("deployment manifest wheel filename does not match its URL")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ReleaseIdentityError("deployment manifest wheel sha256 is invalid")
    return {
        "source_revision": revision,
        "source_repository": repository,
        "wheel_url": wheel_url,
        "wheel_sha256": digest,
    }


def fetch_manifest(url: str) -> Any:
    if not _https_url(url, suffix=".json"):
        raise ReleaseIdentityError("deployment manifest URL must be an https JSON URL")
    with urlopen(url, timeout=30) as response:  # noqa: S310 - URL is validated above
        return json.load(response)


def resolve_identity(
    *,
    tag: str,
    manifest_url: str = "",
    source_revision: str = "",
    source_repository: str = "",
    wheel_url: str = "",
    wheel_sha256: str = "",
    fetcher: Callable[[str], Any] = fetch_manifest,
) -> dict[str, str]:
    if not TAG_RE.fullmatch(tag):
        raise ReleaseIdentityError("release tag must use vMAJOR.MINOR.PATCH form")
    explicit = [source_revision, source_repository, wheel_url, wheel_sha256]
    explicit_count = sum(bool(value) for value in explicit)
    if explicit_count not in (0, len(explicit)):
        raise ReleaseIdentityError("explicit release identity is partial")
    if explicit_count:
        if manifest_url:
            raise ReleaseIdentityError("manifest URL cannot be mixed with explicit release identity")
        if not COMMIT_RE.fullmatch(source_revision):
            raise ReleaseIdentityError("explicit source revision is invalid")
        if not SOURCE_REPOSITORY_RE.fullmatch(source_repository):
            raise ReleaseIdentityError("explicit source repository is invalid")
        if not _https_url(wheel_url, suffix=".whl"):
            raise ReleaseIdentityError("explicit wheel URL is invalid")
        if not SHA256_RE.fullmatch(wheel_sha256):
            raise ReleaseIdentityError("explicit wheel sha256 is invalid")
        return {
            "source_revision": source_revision,
            "source_repository": source_repository,
            "wheel_url": wheel_url,
            "wheel_sha256": wheel_sha256,
        }
    effective_url = manifest_url or DEFAULT_MANIFEST_URL.format(tag=tag)
    if not _https_url(effective_url, suffix=".json"):
        raise ReleaseIdentityError("deployment manifest URL must be an https JSON URL")
    return validate_manifest(fetcher(effective_url), expected_tag=tag)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--manifest-url", default="")
    parser.add_argument("--source-revision", default="")
    parser.add_argument("--source-repository", default="")
    parser.add_argument("--wheel-url", default="")
    parser.add_argument("--wheel-sha256", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        identity = resolve_identity(
            tag=args.tag,
            manifest_url=args.manifest_url,
            source_revision=args.source_revision,
            source_repository=args.source_repository,
            wheel_url=args.wheel_url,
            wheel_sha256=args.wheel_sha256,
        )
    except (OSError, json.JSONDecodeError, ReleaseIdentityError) as exc:
        print(f"error: release identity resolution failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(identity, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
