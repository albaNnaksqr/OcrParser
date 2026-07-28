from __future__ import annotations

from datetime import datetime

from ...models import Manifest


def begin_manifest_freeze(
    manifest: Manifest,
    *,
    frozen_at: datetime,
) -> bool:
    manifest.status = "ready"
    if manifest.frozen_at is not None:
        return False
    manifest.frozen_at = frozen_at
    return True


def mark_manifest_failed(manifest: Manifest) -> None:
    manifest.status = "failed"
