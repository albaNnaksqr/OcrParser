from __future__ import annotations

import copy
import json
import re
from collections import Counter
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


def _runtime_exports_from_baseline() -> list[dict]:
    return [
        {
            key: item[key]
            for key in ("symbol", "kind", "defining_module", "owners")
        }
        for item in _baseline()["exports"]["symbols"]
    ]


def _write_consumer(tmp_path: Path, relative: str, source: str) -> Path:
    root = tmp_path / "repo"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return root


def test_facade_fixture_matches_runtime_and_consumers() -> None:
    baseline = _baseline()
    actual = control_facade_inventory.build_facade_inventory()

    control_facade_inventory.validate_fixture_shape(baseline)
    control_facade_inventory.validate_decreasing(actual, baseline)


def test_facade_inventory_counts_match_independent_audit() -> None:
    inventory = _baseline()
    exports = {
        item["symbol"]: item for item in inventory["exports"]["symbols"]
    }

    assert inventory["exports"]["count"] == 286
    assert inventory["exports"]["classification_counts"] == {
        "internal_no_compat": 136,
        "scheduling_application_pending": 43,
        "settings_pending": 7,
        "supported_explicit_target": 76,
        "unsupported_leaked": 24,
    }
    assert exports["upsert_model_profile"]["defining_module"] == (
        "ocr_platform.control.domains.model_profiles.commands"
    )
    assert exports["upsert_model_profile"]["target"] == (
        "ocr_platform.control.domains.model_profiles.commands."
        "upsert_model_profile"
    )
    assert exports["record_event"]["defining_module"] == (
        "ocr_platform.control.domains.jobs.commands"
    )
    assert exports["record_event"]["target"] == (
        "ocr_platform.control.domains.jobs.commands.record_event"
    )
    assert exports["record_log"]["defining_module"] == (
        "ocr_platform.control.domains.jobs.commands"
    )
    assert exports["record_log"]["target"] == (
        "ocr_platform.control.domains.jobs.commands.record_log"
    )
    assert inventory["imports"]["ast_count"] == 19
    assert inventory["imports"]["dynamic_count"] == 0
    assert inventory["imports"]["embedded_count"] == 1
    assert inventory["consumers"]["file_count"] == 7
    assert inventory["consumers"]["category_counts"] == {
        "test": 6,
        "tool": 1,
    }
    assert inventory["consumers"]["unique_symbol_count"] == 23
    assert inventory["monkeypatches"]["count"] == 21
    assert inventory["monkeypatches"]["form_counts"] == {
        "object": 20,
        "string": 1,
    }


def test_consumed_symbol_migration_map_is_complete_and_owned() -> None:
    inventory = _baseline()
    migrations = {
        item["symbol"]: item
        for item in inventory["consumed_symbol_migrations"]
    }

    assert set(migrations) == set(
        inventory["consumers"]["unique_symbols"]
    )
    assert Counter(
        item["classification"] for item in migrations.values()
    ) == {
        "supported_explicit_target": 9,
        "settings_pending": 7,
        "scheduling_application_pending": 4,
        "internal_no_compat": 3,
    }
    assert migrations["database"]["target"] == (
        "ocr_platform.control.database"
    )
    assert migrations["POOL_SERVER_ID"]["target"] is None
    assert {
        symbol
        for symbol, item in migrations.items()
        if item["classification"] == "internal_no_compat"
    } == {"POOL_SERVER_ID", "json_loads_object", "utcnow"}


def test_wildcard_dependency_leaks_are_not_supported_targets() -> None:
    exports = {
        item["symbol"]: item for item in _baseline()["exports"]["symbols"]
    }
    leaked = {
        "Any",
        "Integer",
        "ManifestItem",
        "ModuleType",
        "ParserConfig",
        "Path",
        "Session",
        "annotations",
        "case",
        "datetime",
        "delete",
        "distinct",
        "func",
        "json",
        "math",
        "os",
        "posixpath",
        "scan_folder_snapshot",
        "select",
        "sys",
        "timedelta",
        "timezone",
        "update",
        "write_manifest_snapshot",
    }

    assert {
        symbol
        for symbol, item in exports.items()
        if item["classification"] == "unsupported_leaked"
    } == leaked
    assert all(exports[symbol]["target"] is None for symbol in leaked)


def test_facade_site_ids_are_line_independent_and_fingerprinted() -> None:
    inventory = _baseline()
    site_groups = [
        inventory["imports"]["ast_sites"],
        inventory["imports"]["dynamic_sites"],
        inventory["imports"]["embedded_sites"],
        inventory["consumers"]["symbol_use_sites"],
        inventory["monkeypatches"]["sites"],
    ]
    for site in (item for group in site_groups for item in group):
        assert re.fullmatch(r".+:[^:]+:.+@[0-9a-f]{12}:[1-9]\d*", site["id"])
        assert re.fullmatch(r"[0-9a-f]{64}", site["fingerprint"])
        assert re.fullmatch(r".+\.py:\d+", site["source"])


def test_new_export_fails_and_export_deletion_passes() -> None:
    baseline = _baseline()

    added = copy.deepcopy(baseline)
    added["exports"]["symbols"].append(
        {
            "symbol": "new_facade_export",
            "kind": "function",
            "defining_module": "ocr_platform.control.domains.jobs.core",
            "owners": ["ocr_platform.control.domains.jobs.core"],
            "classification": "scheduling_application_pending",
            "target": "planned:owning domain application/command/query",
            "wave": "PR 5-7",
            "reason": "mutation fixture",
            "consumed": False,
        }
    )
    added["exports"]["count"] += 1
    added["exports"]["classification_counts"][
        "scheduling_application_pending"
    ] += 1
    with pytest.raises(ValueError, match="new façade exports"):
        control_facade_inventory.validate_decreasing(added, baseline)

    deleted = copy.deepcopy(baseline)
    removed = deleted["exports"]["symbols"].pop()
    deleted["exports"]["count"] -= 1
    deleted["exports"]["classification_counts"][
        removed["classification"]
    ] -= 1
    control_facade_inventory.validate_decreasing(deleted, baseline)


def test_new_direct_wildcard_and_dynamic_imports_fail(
    tmp_path: Path,
) -> None:
    target = control_facade_inventory.TARGET_MODULE
    cases = {
        "direct": f"import {target} as legacy_service\n",
        "wildcard": f"from {target} import *\n",
        "dynamic": (
            "import importlib\n"
            f"legacy_service = importlib.import_module({target!r})\n"
        ),
    }
    for name, source in cases.items():
        root = _write_consumer(
            tmp_path / name,
            "tests/test_new_consumer.py",
            source,
        )
        actual = control_facade_inventory.build_facade_inventory(
            root,
            runtime_exports=_runtime_exports_from_baseline(),
        )
        with pytest.raises(ValueError, match="new or replaced façade sites"):
            control_facade_inventory.validate_decreasing(
                actual,
                _baseline(),
            )


def test_new_embedded_import_and_string_monkeypatch_fail(
    tmp_path: Path,
) -> None:
    target = control_facade_inventory.TARGET_MODULE
    embedded_source = (
        "import subprocess\n"
        "subprocess.check_output(['python', '-c', "
        + repr(f"import {target} as service\nprint(service.utcnow())\n")
        + "])\n"
    )
    embedded_root = _write_consumer(
        tmp_path / "embedded",
        "tests/test_embedded.py",
        embedded_source,
    )
    embedded = control_facade_inventory.build_facade_inventory(
        embedded_root,
        runtime_exports=_runtime_exports_from_baseline(),
    )
    assert embedded["imports"]["embedded_count"] == 1
    with pytest.raises(ValueError, match="new or replaced façade sites"):
        control_facade_inventory.validate_decreasing(
            embedded,
            _baseline(),
        )

    embedded_dynamic_source = (
        "import subprocess\n"
        "subprocess.check_output(['python', '-c', "
        + repr(
            "import importlib\n"
            f"importlib.import_module({target!r})\n"
        )
        + "])\n"
    )
    embedded_dynamic_root = _write_consumer(
        tmp_path / "embedded-dynamic",
        "tests/test_embedded_dynamic.py",
        embedded_dynamic_source,
    )
    embedded_dynamic = control_facade_inventory.build_facade_inventory(
        embedded_dynamic_root,
        runtime_exports=_runtime_exports_from_baseline(),
    )
    assert embedded_dynamic["imports"]["embedded_count"] == 1
    with pytest.raises(ValueError, match="new or replaced façade sites"):
        control_facade_inventory.validate_decreasing(
            embedded_dynamic,
            _baseline(),
        )

    patch_source = (
        "def test_patch(monkeypatch):\n"
        f"    monkeypatch.setattr({(target + '.JOB_EVENT_DETAIL_LIMIT')!r}, 1)\n"
    )
    patch_root = _write_consumer(
        tmp_path / "patch",
        "tests/test_patch.py",
        patch_source,
    )
    patched = control_facade_inventory.build_facade_inventory(
        patch_root,
        runtime_exports=_runtime_exports_from_baseline(),
    )
    assert patched["monkeypatches"]["count"] == 1
    with pytest.raises(ValueError, match="new or replaced façade sites"):
        control_facade_inventory.validate_decreasing(
            patched,
            _baseline(),
        )


def test_production_facade_consumer_is_always_forbidden(
    tmp_path: Path,
) -> None:
    target = control_facade_inventory.TARGET_MODULE
    root = _write_consumer(
        tmp_path,
        "ocr_platform/control/new_runtime.py",
        f"from {target} import create_job\n",
    )
    actual = control_facade_inventory.build_facade_inventory(
        root,
        runtime_exports=_runtime_exports_from_baseline(),
    )

    with pytest.raises(ValueError, match="production façade consumers"):
        control_facade_inventory.validate_decreasing(
            actual,
            _baseline(),
        )


def test_deleting_a_monkeypatch_site_is_allowed() -> None:
    baseline = _baseline()
    actual = copy.deepcopy(baseline)
    removed = actual["monkeypatches"]["sites"].pop()
    actual["monkeypatches"]["count"] -= 1
    actual["monkeypatches"]["form_counts"][removed["form"]] -= 1
    if actual["monkeypatches"]["form_counts"][removed["form"]] == 0:
        del actual["monkeypatches"]["form_counts"][removed["form"]]

    control_facade_inventory.validate_decreasing(actual, baseline)


def test_deleting_one_symbol_from_multi_import_is_allowed(
    tmp_path: Path,
) -> None:
    target = control_facade_inventory.TARGET_MODULE
    baseline_root = _write_consumer(
        tmp_path / "baseline",
        "tests/test_consumer.py",
        (
            f"from {target} import create_job, upsert_model_profile\n"
            "\n"
            "def use():\n"
            "    return create_job, upsert_model_profile\n"
        ),
    )
    actual_root = _write_consumer(
        tmp_path / "actual",
        "tests/test_consumer.py",
        (
            f"from {target} import create_job\n"
            "\n"
            "def use():\n"
            "    return create_job\n"
        ),
    )
    exports = _runtime_exports_from_baseline()
    mini_baseline = control_facade_inventory.build_facade_inventory(
        baseline_root,
        runtime_exports=exports,
    )
    actual = control_facade_inventory.build_facade_inventory(
        actual_root,
        runtime_exports=exports,
    )

    control_facade_inventory.validate_decreasing(
        actual,
        mini_baseline,
    )


def test_relative_facade_imports_are_production_consumers(
    tmp_path: Path,
) -> None:
    root = _write_consumer(
        tmp_path,
        "ocr_platform/control/new_runtime.py",
        (
            "from . import service as legacy_service\n"
            "from .service import create_job\n"
            "\n"
            "def use():\n"
            "    return legacy_service.archive_job, create_job\n"
        ),
    )
    actual = control_facade_inventory.build_facade_inventory(
        root,
        runtime_exports=_runtime_exports_from_baseline(),
    )

    assert actual["imports"]["ast_count"] == 2
    assert actual["consumers"]["category_counts"] == {"production": 1}
    assert {"archive_job", "create_job"} <= set(
        actual["consumers"]["unique_symbols"]
    )
    with pytest.raises(ValueError, match="production façade consumers"):
        control_facade_inventory.validate_decreasing(
            actual,
            _baseline(),
        )


def test_constant_folded_dynamic_imports_and_alias_use_are_blocked(
    tmp_path: Path,
) -> None:
    root = _write_consumer(
        tmp_path,
        "ocr_platform/control/new_runtime.py",
        (
            "import importlib\n"
            "MODULE = 'ocr_platform.control.' + 'service'\n"
            "legacy = importlib.import_module(MODULE)\n"
            "FIRST = 'ocr_platform.control.'\n"
            "LAST = 'service'\n"
            "legacy_f = __import__(f'{FIRST}{LAST}')\n"
            "value = legacy.create_job\n"
            "other = legacy_f.archive_job\n"
        ),
    )
    actual = control_facade_inventory.build_facade_inventory(
        root,
        runtime_exports=_runtime_exports_from_baseline(),
    )

    assert actual["imports"]["dynamic_count"] == 2
    assert {"archive_job", "create_job"} <= set(
        actual["consumers"]["unique_symbols"]
    )
    with pytest.raises(ValueError, match="production façade consumers"):
        control_facade_inventory.validate_decreasing(
            actual,
            _baseline(),
        )


@pytest.mark.parametrize(
    "source",
    [
        (
            "from importlib import import_module as load\n"
            "legacy = load('ocr_platform.control.service')\n"
            "value = legacy.create_job\n"
        ),
        (
            "import importlib\n"
            "load = importlib.import_module\n"
            "legacy = load('ocr_platform.control.service')\n"
            "value = legacy.create_job\n"
        ),
        (
            "import importlib\n"
            "legacy = importlib.import_module("
            "name='ocr_platform.control.service')\n"
            "value = legacy.create_job\n"
        ),
        (
            "from importlib import import_module\n"
            "legacy = import_module("
            "'.service', 'ocr_platform.control')\n"
            "value = legacy.create_job\n"
        ),
    ],
)
def test_dynamic_import_callable_alias_keyword_and_relative_forms(
    tmp_path: Path,
    source: str,
) -> None:
    root = _write_consumer(
        tmp_path,
        "ocr_platform/control/new_runtime.py",
        source,
    )
    actual = control_facade_inventory.build_facade_inventory(
        root,
        runtime_exports=_runtime_exports_from_baseline(),
    )

    assert actual["imports"]["dynamic_count"] == 1
    assert "create_job" in actual["consumers"]["unique_symbols"]
    with pytest.raises(ValueError, match="production façade consumers"):
        control_facade_inventory.validate_decreasing(
            actual,
            _baseline(),
        )


def test_assigned_service_alias_and_getattr_add_symbol_sites(
    tmp_path: Path,
) -> None:
    source = (
        ROOT / "tests" / "test_control_api.py"
    ).read_text(encoding="utf-8")
    root = _write_consumer(
        tmp_path,
        "tests/test_control_api.py",
        source
        + """

legacy_service_alias = service
new_alias_use = legacy_service_alias.archive_job
new_getattr_use = getattr(service, "delete_job")
""",
    )
    actual = control_facade_inventory.build_facade_inventory(
        root,
        runtime_exports=_runtime_exports_from_baseline(),
    )

    assert {"archive_job", "delete_job"} <= set(
        actual["consumers"]["unique_symbols"]
    )
    with pytest.raises(
        ValueError,
        match="consumed symbol migration map is incomplete",
    ):
        control_facade_inventory.validate_decreasing(
            actual,
            _baseline(),
        )


def test_delattr_and_patch_multiple_are_monkeypatch_sites(
    tmp_path: Path,
) -> None:
    source = (
        ROOT / "tests" / "test_control_api.py"
    ).read_text(encoding="utf-8")
    root = _write_consumer(
        tmp_path,
        "tests/test_control_api.py",
        source
        + """

def added_patch_forms(monkeypatch):
    from unittest.mock import patch
    monkeypatch.delattr(service, "create_job")
    patch.multiple(
        service,
        create_job=None,
        upsert_model_profile=None,
    )
""",
    )
    actual = control_facade_inventory.build_facade_inventory(
        root,
        runtime_exports=_runtime_exports_from_baseline(),
    )

    assert actual["monkeypatches"]["count"] == 23
    assert actual["monkeypatches"]["form_counts"] == {
        "multiple": 2,
        "object": 21,
    }
    with pytest.raises(ValueError, match="new or replaced façade sites"):
        control_facade_inventory.validate_decreasing(
            actual,
            _baseline(),
        )


def test_function_parameter_and_assignment_shadowing_do_not_leak_aliases(
    tmp_path: Path,
) -> None:
    target = control_facade_inventory.TARGET_MODULE
    root = _write_consumer(
        tmp_path,
        "tests/test_shadowing.py",
        (
            f"import {target} as service\n"
            "\n"
            "def shadowed(service):\n"
            "    return service.create_job\n"
            "\n"
            "from importlib import import_module\n"
            "def shadowed_importer(import_module):\n"
            f"    return import_module({target!r})\n"
            "\n"
            "service = object()\n"
            "value = service.create_job\n"
        ),
    )
    actual = control_facade_inventory.build_facade_inventory(
        root,
        runtime_exports=_runtime_exports_from_baseline(),
    )

    assert actual["imports"]["ast_count"] == 1
    assert actual["imports"]["dynamic_count"] == 0
    assert actual["consumers"]["symbol_use_count"] == 0
    assert actual["consumers"]["unique_symbol_count"] == 0
