"""Deterministic tests for scripts/validate_inventory.py.

No SSH, systemd, or network: this only parses YAML documents (the shipped
example files, plus small synthetic mutations of them) through the
validator's pure-Python entry points.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = BUNDLE_ROOT / "scripts"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


validate_inventory = _load_module(
    "ocrparser_bundle_validate_inventory", SCRIPTS_DIR / "validate_inventory.py"
)


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def example_inventory() -> dict:
    return copy.deepcopy(_load_yaml(BUNDLE_ROOT / "inventory.example.yml"))


def example_group_vars() -> dict:
    return copy.deepcopy(_load_yaml(BUNDLE_ROOT / "group_vars" / "all.example.yml"))


def validate(inventory: dict, group_vars: dict | None = None) -> list:
    extra = [("all.example.yml", group_vars)] if group_vars is not None else ()
    return validate_inventory.validate_documents(inventory, extra_documents=extra)


# ---- valid baseline --------------------------------------------------------


def test_shipped_example_inventory_and_group_vars_satisfy_the_contract():
    findings = validate(example_inventory(), example_group_vars())
    assert findings == []


def test_shipped_examples_only_reference_the_reserved_invalid_tld():
    inventory_text = (BUNDLE_ROOT / "inventory.example.yml").read_text(encoding="utf-8")
    for token in ("control-1.example.invalid", "worker-1.example.invalid"):
        assert token in inventory_text


# ---- invalid inventory: one finding per contract rule ----------------------


def test_missing_required_all_var_is_reported():
    inventory = example_inventory()
    del inventory["all"]["vars"]["ocr_release_tag"]
    findings = validate(inventory, example_group_vars())
    codes = {f.code for f in findings}
    assert validate_inventory.MISSING_REQUIRED_VAR in codes


def test_malformed_release_commit_is_reported():
    inventory = example_inventory()
    inventory["all"]["vars"]["ocr_release_commit"] = "not-a-commit"
    inventory["all"]["vars"]["ocr_source_repo_url"] = (
        "https://github.com/albaNnaksqr/OcrParser.git"
    )
    inventory["all"]["vars"]["ocr_wheel_url"] = "https://artifacts.example.invalid/release.whl"
    inventory["all"]["vars"]["ocr_wheel_sha256"] = "a" * 64
    findings = validate(inventory, example_group_vars())
    codes = {f.code for f in findings}
    assert validate_inventory.RELEASE_COMMIT_INVALID in codes


def test_placeholder_value_is_detected():
    inventory = example_inventory()
    inventory["all"]["vars"]["ocr_service_user"] = "CHANGE_ME"
    findings = validate(inventory, example_group_vars())
    codes = {f.code for f in findings}
    assert validate_inventory.PLACEHOLDER_VALUE in codes


def test_duplicate_worker_server_id_is_reported():
    inventory = example_inventory()
    workers = inventory["all"]["children"]["ocr_workers"]["hosts"]
    first_host = next(iter(workers))
    workers[first_host]["ocr_worker_server_id"] = "duplicate-worker"
    workers["worker-4.example.invalid"] = {"ocr_worker_server_id": "duplicate-worker"}
    findings = validate(inventory, example_group_vars())
    codes = {f.code for f in findings}
    assert validate_inventory.WORKER_SERVER_ID_DUPLICATE in codes


def test_second_control_host_is_reported():
    inventory = example_inventory()
    control_hosts = inventory["all"]["children"]["ocr_control"]["hosts"]
    first = next(iter(control_hosts.values()))
    control_hosts["control-2.example.invalid"] = dict(first)
    findings = validate(inventory, example_group_vars())
    codes = {f.code for f in findings}
    assert validate_inventory.CONTROL_GROUP_NOT_SINGLE_HOST in codes


def test_rollout_serial_below_one_is_rejected():
    inventory = example_inventory()
    inventory["all"]["vars"]["ocr_rollout_serial"] = 0
    findings = validate(inventory, example_group_vars())
    codes = {f.code for f in findings}
    assert validate_inventory.ROLLOUT_SERIAL_INVALID in codes


def test_rollout_failure_percentage_out_of_range_is_rejected():
    inventory = example_inventory()
    inventory["all"]["vars"]["ocr_rollout_max_fail_percentage"] = 101
    findings = validate(inventory, example_group_vars())
    codes = {f.code for f in findings}
    assert validate_inventory.ROLLOUT_FAILURE_PERCENTAGE_INVALID in codes


def test_path_escaping_shared_root_is_rejected():
    inventory = example_inventory()
    inventory["all"]["vars"]["ocr_input_root"] = "/srv/other-tree/input"
    findings = validate(inventory, example_group_vars())
    codes = {f.code for f in findings}
    assert validate_inventory.PATH_ESCAPES_SHARED_ROOT in codes


def test_canary_contract_incomplete_when_a_canary_var_is_blank():
    inventory = example_inventory()
    inventory["all"]["vars"]["ocr_canary_enabled"] = True
    inventory["all"]["vars"]["ocr_canary_model_name"] = ""
    findings = validate(inventory, example_group_vars())
    codes = {f.code for f in findings}
    assert validate_inventory.CANARY_CONTRACT_INCOMPLETE in codes


def test_canary_fields_are_optional_while_canary_is_disabled():
    inventory = example_inventory()
    assert validate(inventory, example_group_vars()) == []


def test_compact_inventory_derives_safe_paths_and_worker_identity():
    inventory = example_inventory()
    provided = dict(example_group_vars())
    provided.update(inventory["all"]["vars"])
    effective = validate_inventory.effective_all_vars(provided)
    assert effective["ocr_install_root"] == "/opt/ocr-platform"
    assert effective["ocr_input_root"] == "/srv/ocr-shared/input"
    assert effective["ocr_output_root"] == "/srv/ocr-shared/output"
    assert effective["ocr_platform_root"] == "/srv/ocr-shared/ocr-platform"
    assert effective["ocr_postgres_port"] == 5432


def test_partial_explicit_release_identity_is_rejected():
    inventory = example_inventory()
    inventory["all"]["vars"]["ocr_release_commit"] = "a" * 40
    codes = {finding.code for finding in validate(inventory, example_group_vars())}
    assert validate_inventory.RELEASE_IDENTITY_PARTIAL in codes


def test_explicit_release_identity_requires_source_repository_too():
    inventory = example_inventory()
    inventory["all"]["vars"].update(
        {
            "ocr_release_commit": "a" * 40,
            "ocr_wheel_url": "https://artifacts.example.invalid/release.whl",
            "ocr_wheel_sha256": "b" * 64,
        }
    )
    codes = {finding.code for finding in validate(inventory, example_group_vars())}
    assert validate_inventory.RELEASE_IDENTITY_PARTIAL in codes


def test_complete_explicit_release_identity_remains_compatible():
    inventory = example_inventory()
    inventory["all"]["vars"].update(
        {
            "ocr_release_commit": "a" * 40,
            "ocr_source_repo_url": "https://github.com/albaNnaksqr/OcrParser.git",
            "ocr_wheel_url": "https://artifacts.example.invalid/release.whl",
            "ocr_wheel_sha256": "b" * 64,
        }
    )
    assert validate(inventory, example_group_vars()) == []


def test_manifest_url_cannot_be_mixed_with_explicit_identity():
    inventory = example_inventory()
    inventory["all"]["vars"].update(
        {
            "ocr_release_manifest_url": "https://artifacts.example.invalid/deployment-manifest.json",
            "ocr_release_commit": "a" * 40,
            "ocr_source_repo_url": "https://github.com/albaNnaksqr/OcrParser.git",
            "ocr_wheel_url": "https://artifacts.example.invalid/release.whl",
            "ocr_wheel_sha256": "b" * 64,
        }
    )
    codes = {finding.code for finding in validate(inventory, example_group_vars())}
    assert validate_inventory.RELEASE_IDENTITY_PARTIAL in codes


def test_invalid_derived_worker_alias_is_rejected():
    inventory = example_inventory()
    workers = inventory["all"]["children"]["ocr_workers"]["hosts"]
    workers["bad/worker"] = workers.pop(next(iter(workers)))
    codes = {finding.code for finding in validate(inventory, example_group_vars())}
    assert validate_inventory.WORKER_SERVER_ID_INVALID in codes


def test_yaml_parse_error_on_unreadable_document(tmp_path):
    bad = tmp_path / "inventory.yml"
    bad.write_text("all:\n  vars: [unterminated", encoding="utf-8")
    findings = validate_inventory.validate_paths(bad)
    assert len(findings) == 1
    assert findings[0].code == validate_inventory.YAML_PARSE_ERROR


# ---- secret hygiene: never leak values, only reject shapes -----------------


def test_literal_secret_field_is_rejected_and_the_value_never_leaks():
    inventory = example_inventory()
    inventory["all"]["vars"]["ocr_extra_api_key"] = "sk-super-secret-value"
    findings = validate(inventory, example_group_vars())
    codes = {f.code for f in findings}
    assert validate_inventory.LITERAL_SECRET_FIELD in codes
    rendered = json.dumps([f.to_dict() for f in findings])
    assert "sk-super-secret-value" not in rendered


def test_malformed_secret_reference_is_rejected_and_never_leaks_a_value():
    inventory = example_inventory()
    inventory["all"]["vars"]["ocr_api_token_secret_var"] = "not env-var shaped!!"
    findings = validate(inventory, example_group_vars())
    codes = {f.code for f in findings}
    assert validate_inventory.SECRET_REFERENCE_INVALID in codes
    rendered = json.dumps([f.to_dict() for f in findings])
    assert "not env-var shaped!!" not in rendered


def test_boolean_and_binary_policy_switches_named_like_secrets_are_allowed():
    inventory = example_inventory()
    inventory["all"]["vars"]["ocr_control_require_postgres_password"] = 1
    findings = validate(inventory, example_group_vars())
    codes = {f.code for f in findings}
    assert validate_inventory.LITERAL_SECRET_FIELD not in codes


# ---- CLI surface -------------------------------------------------------


def test_cli_json_output_reports_ok_true_for_the_shipped_examples(capsys):
    exit_code = validate_inventory.main(
        [
            str(BUNDLE_ROOT / "inventory.example.yml"),
            "--group-vars",
            str(BUNDLE_ROOT / "group_vars" / "all.example.yml"),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["findings"] == []


def test_cli_exits_non_zero_and_lists_findings_for_a_broken_inventory(tmp_path, capsys):
    inventory = example_inventory()
    del inventory["all"]["vars"]["ocr_release_tag"]
    broken = tmp_path / "inventory.yml"
    broken.write_text(yaml.safe_dump(inventory), encoding="utf-8")
    group_vars_path = tmp_path / "all.yml"
    group_vars_path.write_text(yaml.safe_dump(example_group_vars()), encoding="utf-8")

    exit_code = validate_inventory.main(
        [str(broken), "--group-vars", str(group_vars_path), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ok"] is False
    assert any(f["code"] == validate_inventory.MISSING_REQUIRED_VAR for f in payload["findings"])
