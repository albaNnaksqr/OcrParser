"""Runtime legal notices and the AGPL corresponding-source offer."""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path
from urllib.parse import quote


PACKAGE_NAME = "ocrparser-platform"
SOURCE_REPOSITORY = "https://github.com/albaNnaksqr/OcrParser"
SOURCE_REVISION_ENV = "OCR_PLATFORM_SOURCE_REVISION"
SOURCE_URL_ENV = "OCR_PLATFORM_SOURCE_URL"
VERSION_FALLBACK = "0.2.1"


def package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return VERSION_FALLBACK


def source_offer() -> dict[str, object]:
    """Return the public corresponding-source offer for this deployment."""

    deployed_version = package_version()
    explicit_revision = os.getenv(SOURCE_REVISION_ENV, "").strip()
    revision = explicit_revision or f"v{deployed_version}"
    explicit_url = os.getenv(SOURCE_URL_ENV, "").strip()
    source_url = explicit_url or f"{SOURCE_REPOSITORY}/tree/{quote(revision, safe='')}"
    return {
        "project": "OcrParser",
        "version": deployed_version,
        "source_revision": revision,
        "source_url": source_url,
        "source_revision_explicit": bool(explicit_revision or explicit_url),
        "license": "GNU Affero General Public License v3 for deployments using the AGPL build of PyMuPDF",
        "license_url": "/legal/agpl-3.0",
        "copyright": "Copyright (c) 2026 OCR Parser contributors",
        "warranty": "This program is provided without warranty; see GNU AGPLv3 for details.",
    }


def agpl_license_text() -> str:
    """Load the bundled GNU AGPLv3 text in source and installed-wheel layouts."""

    try:
        package_distribution = distribution(PACKAGE_NAME)
    except PackageNotFoundError:
        package_distribution = None

    if package_distribution is not None:
        for item in package_distribution.files or []:
            if Path(str(item)).name == "AGPL_3.0.txt":
                candidate = Path(package_distribution.locate_file(item))
                if candidate.is_file():
                    return candidate.read_text(encoding="utf-8")

    source_checkout = Path(__file__).resolve().parents[1] / "third_party" / "licenses" / "AGPL_3.0.txt"
    return source_checkout.read_text(encoding="utf-8")
