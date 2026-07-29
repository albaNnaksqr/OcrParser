from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import control_facade_inventory


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "contracts"
    / "control_facade_inventory.json"
)


def _baseline() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _write_source(
    tmp_path: Path,
    relative: str,
    source: str,
) -> Path:
    root = tmp_path / "repo"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return root


def test_facade_tombstone_fixture_matches_repository() -> None:
    expected = _baseline()
    actual = control_facade_inventory.build_facade_inventory()

    control_facade_inventory.validate_removed(expected)
    control_facade_inventory.validate_removed(actual)
    assert actual == expected


def test_all_consumed_symbols_have_completed_explicit_migrations() -> None:
    migrations = {
        item["symbol"]: item
        for item in _baseline()["consumed_symbol_migrations"]
    }

    assert len(migrations) == 24
    assert all(item["status"] == "migrated" for item in migrations.values())
    assert all(item["target"] for item in migrations.values())
    assert migrations["create_job"]["target"] == (
        "ocr_platform.control.domains.jobs.commands.create_job"
    )
    assert migrations["upsert_model_profile"]["target"] == (
        "ocr_platform.control.domains.model_profiles.commands."
        "upsert_model_profile"
    )
    assert migrations["database"]["target"] == (
        "ocr_platform.control.database"
    )
    assert migrations["_database_migration_preflight_issue"]["target"] == (
        "ocr_platform.control.domains.workers.preflight."
        "database_migration_preflight_issue"
    )


@pytest.mark.parametrize(
    ("relative", "source", "kind"),
    [
        (
            "ocr_platform/control/runtime.py",
            "import ocr_platform.control.service as legacy\n",
            "ast_import",
        ),
        (
            "ocr_platform/control/runtime.py",
            "from ocr_platform.control.service import create_job\n",
            "ast_import",
        ),
        (
            "ocr_platform/control/runtime.py",
            "from . import service\n",
            "ast_import",
        ),
        (
            "ocr_platform/control/domains/jobs/runtime.py",
            "from ...service import create_job\n",
            "ast_import",
        ),
        (
            "ocr_platform/control/runtime.py",
            "import importlib\n"
            "legacy = importlib.import_module("
            "'ocr_platform.control.service')\n",
            "dynamic_import",
        ),
        (
            "ocr_platform/control/runtime.py",
            "from importlib import import_module\n"
            "legacy = import_module("
            "'.service', package='ocr_platform.control')\n",
            "dynamic_import",
        ),
        (
            "tools/runtime.py",
            "SOURCE = 'import ocr_platform.control.service as service\\n'\n",
            "embedded_import",
        ),
        (
            "tests/runtime.py",
            "TARGET = 'ocr_platform.control.service.create_job'\n",
            "string_reference",
        ),
    ],
)
def test_any_legacy_facade_reference_is_rejected(
    tmp_path: Path,
    relative: str,
    source: str,
    kind: str,
) -> None:
    root = _write_source(tmp_path, relative, source)
    payload = control_facade_inventory.build_facade_inventory(root)

    assert payload["reference_count"] == 1
    assert payload["references"][0]["kind"] == kind
    with pytest.raises(
        ValueError,
        match="legacy Control service façade references are forbidden",
    ):
        control_facade_inventory.validate_removed(payload)


def test_unrelated_domain_service_import_is_allowed(
    tmp_path: Path,
) -> None:
    root = _write_source(
        tmp_path,
        "ocr_platform/control/domains/remote_admin/router.py",
        "from . import service\n",
    )
    payload = control_facade_inventory.build_facade_inventory(root)

    assert payload["reference_count"] == 0
    control_facade_inventory.validate_removed(payload)


def test_facade_package_reappearance_is_rejected(tmp_path: Path) -> None:
    root = _write_source(
        tmp_path,
        "ocr_platform/control/service/__init__.py",
        "",
    )
    payload = control_facade_inventory.build_facade_inventory(root)

    assert payload["facade_exists"] is True
    with pytest.raises(
        ValueError,
        match="ocr_platform.control.service must be removed",
    ):
        control_facade_inventory.validate_removed(payload)
