"""Deterministic tests for deployment release manifest generation."""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path


BUNDLE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = BUNDLE_ROOT / "scripts" / "generate_release_manifest.py"
SCHEMA = BUNDLE_ROOT / "schema" / "deployment-release.schema.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("ocrparser_generate_release_manifest", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generator = _load_module()


def _wheel(
    path: Path,
    revision: str,
    *,
    dirty: bool = False,
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ocr_platform/_build_info.json",
            json.dumps(
                {
                    "source_revision": revision,
                    "dirty": dirty,
                }
            ),
        )


def test_manifest_schema_is_valid_json_and_fixed_to_v1():
    payload = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert payload["properties"]["schema"]["const"] == generator.SCHEMA
    assert payload["properties"]["release_build"]["const"] is True


def test_clean_wheel_generates_deterministic_manifest(tmp_path):
    revision = "a" * 40
    wheel = tmp_path / "ocrparser_platform-0.4.1-py3-none-any.whl"
    _wheel(wheel, revision)
    first = generator.build_manifest(
        tag="v0.4.1",
        source_revision=revision,
        source_repository="https://github.com/albaNnaksqr/OcrParser.git",
        wheel_url=f"https://example.invalid/releases/{wheel.name}",
        wheel_path=wheel,
    )
    second = generator.build_manifest(
        tag="v0.4.1",
        source_revision=revision,
        source_repository="https://github.com/albaNnaksqr/OcrParser.git",
        wheel_url=f"https://example.invalid/releases/{wheel.name}",
        wheel_path=wheel,
    )
    assert first == second
    assert first["release_build"] is True
    assert first["source_revision"] == revision
    assert len(first["wheel"]["sha256"]) == 64


def test_dirty_or_mismatched_wheel_is_rejected(tmp_path):
    wheel = tmp_path / "release.whl"
    _wheel(wheel, "a" * 40, dirty=True)
    try:
        generator.build_manifest(
            tag="v0.4.1",
            source_revision="a" * 40,
            source_repository="https://github.com/albaNnaksqr/OcrParser.git",
            wheel_url="https://example.invalid/release.whl",
            wheel_path=wheel,
        )
    except ValueError as exc:
        assert "clean release" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("dirty wheel was accepted")

    _wheel(wheel, "b" * 40)
    try:
        generator.build_manifest(
            tag="v0.4.1",
            source_revision="a" * 40,
            source_repository="https://github.com/albaNnaksqr/OcrParser.git",
            wheel_url="https://example.invalid/release.whl",
            wheel_path=wheel,
        )
    except ValueError as exc:
        assert "revision" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("mismatched wheel was accepted")


def test_manifest_generator_rejects_a_noncanonical_repository(tmp_path):
    wheel = tmp_path / "release.whl"
    _wheel(wheel, "a" * 40)
    try:
        generator.build_manifest(
            tag="v0.4.1",
            source_revision="a" * 40,
            source_repository="https://example.invalid/fork.git",
            wheel_url="https://example.invalid/release.whl",
            wheel_path=wheel,
        )
    except ValueError as exc:
        assert "canonical" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("noncanonical source repository was accepted")
