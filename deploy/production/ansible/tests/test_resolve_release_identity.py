"""Pure resolver tests; no network is used."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


BUNDLE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = BUNDLE_ROOT / "scripts" / "resolve_release_identity.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("ocrparser_resolve_release_identity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


resolver = _load_module()


def manifest(tag: str = "v0.4.1") -> dict:
    return {
        "schema": resolver.SCHEMA,
        "tag": tag,
        "source_revision": "a" * 40,
        "source_repository": resolver.CANONICAL_SOURCE_REPOSITORY,
        "release_build": True,
        "wheel": {
            "filename": "ocrparser_platform-0.4.1-py3-none-any.whl",
            "url": "https://example.invalid/ocrparser_platform-0.4.1-py3-none-any.whl",
            "sha256": "b" * 64,
        },
    }


def test_tag_only_uses_manifest_and_returns_allow_listed_identity():
    requested_urls: list[str] = []

    def fetch(url: str):
        requested_urls.append(url)
        return manifest()

    identity = resolver.resolve_identity(tag="v0.4.1", fetcher=fetch)
    assert requested_urls == [resolver.DEFAULT_MANIFEST_URL.format(tag="v0.4.1")]
    assert set(identity) == {
        "source_revision", "source_repository", "wheel_url", "wheel_sha256"
    }


def test_manifest_override_is_used_but_must_match_tag_and_digest_contract():
    url = "https://mirror.example.invalid/deployment-manifest.json"
    identity = resolver.resolve_identity(
        tag="v0.4.1", manifest_url=url, fetcher=lambda requested: manifest()
    )
    assert identity["wheel_sha256"] == "b" * 64

    bad_tag = manifest("v9.9.9")
    with pytest.raises(resolver.ReleaseIdentityError, match="tag"):
        resolver.resolve_identity(tag="v0.4.1", fetcher=lambda requested: bad_tag)

    bad_digest = manifest()
    bad_digest["wheel"]["sha256"] = "invalid"
    with pytest.raises(resolver.ReleaseIdentityError, match="sha256"):
        resolver.resolve_identity(tag="v0.4.1", fetcher=lambda requested: bad_digest)

    noncanonical = manifest()
    noncanonical["source_repository"] = "https://example.invalid/fork.git"
    with pytest.raises(resolver.ReleaseIdentityError, match="canonical"):
        resolver.resolve_identity(tag="v0.4.1", fetcher=lambda requested: noncanonical)


def test_complete_explicit_identity_works_offline_and_partial_or_mixed_fails():
    identity = resolver.resolve_identity(
        tag="v0.4.0",
        source_revision="a" * 40,
        source_repository="https://github.com/albaNnaksqr/OcrParser.git",
        wheel_url="https://example.invalid/release.whl",
        wheel_sha256="b" * 64,
        fetcher=lambda url: (_ for _ in ()).throw(AssertionError("network used")),
    )
    assert identity["source_repository"] == "https://github.com/albaNnaksqr/OcrParser.git"

    with pytest.raises(resolver.ReleaseIdentityError, match="partial"):
        resolver.resolve_identity(tag="v0.4.0", source_revision="a" * 40)
    with pytest.raises(resolver.ReleaseIdentityError, match="mixed"):
        resolver.resolve_identity(
            tag="v0.4.0",
            manifest_url="https://example.invalid/deployment-manifest.json",
            source_revision="a" * 40,
            source_repository="https://github.com/albaNnaksqr/OcrParser.git",
            wheel_url="https://example.invalid/release.whl",
            wheel_sha256="b" * 64,
        )
