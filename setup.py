"""Setuptools hooks for immutable build provenance."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


ROOT = Path(__file__).resolve().parent


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def build_provenance() -> dict[str, object]:
    revision = (
        os.environ.get("OCRPARSER_BUILD_REVISION", "").strip()
        or os.environ.get("GITHUB_SHA", "").strip()
        or _git("rev-parse", "HEAD")
        or "unknown"
    )
    dirty_override = os.environ.get("OCRPARSER_BUILD_DIRTY", "").strip().lower()
    if dirty_override:
        dirty = dirty_override in {"1", "true", "yes", "on"}
    else:
        dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if source_date_epoch:
        timestamp = datetime.fromtimestamp(int(source_date_epoch), timezone.utc)
    else:
        timestamp = datetime.now(timezone.utc)
    return {
        "source_revision": revision,
        "build_timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "dirty": dirty,
    }


class ProvenanceBuildPy(build_py):
    def run(self) -> None:
        super().run()
        target = Path(self.build_lib) / "ocr_platform" / "_build_info.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(build_provenance(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


setup(cmdclass={"build_py": ProvenanceBuildPy})
