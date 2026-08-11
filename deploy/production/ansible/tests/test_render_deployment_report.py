"""Deterministic tests for scripts/render_deployment_report.py.

No SSH, systemd, or network: this only feeds synthetic structured-fact
dictionaries (matching the schema roles/verify/tasks/main.yml writes) through
the renderer's pure-Python entry points and its CLI.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = BUNDLE_ROOT / "scripts"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


render = _load_module(
    "ocrparser_bundle_render_deployment_report", SCRIPTS_DIR / "render_deployment_report.py"
)


def healthy_verify_payload(**overrides) -> dict:
    payload = {
        "release_tag": "v0.4.0",
        "release_commit": "4b7c1e0a9d3f26815c4a0be7f2d59836a10c4d7b",
        "wheel_sha256": "8a1c4f0b26d97e35c081ba47f92d6e30c5a7b418d29f60e3b71c8d425af096e",
        "control": {
            "healthy": True,
            "ready": True,
            "source_revision_matches": True,
        },
        "migrations": {"verified": True},
        "workers": {
            "total": 3,
            "online": 3,
            "unique_server_ids": 3,
            "shared_root_ok": 3,
            "spool_quarantine": 0,
            "version_consistent": True,
        },
    }
    payload.update(overrides)
    return payload


# ---- build_report: status codes --------------------------------------------


def test_fully_healthy_verify_report_is_ok_with_no_failure_codes():
    report = render.build_report(healthy_verify_payload())
    assert report["ok"] is True
    assert render.CONTROL_HEALTHY in report["codes"]
    assert render.WORKERS_ALL_ONLINE in report["codes"]
    assert render.MIGRATIONS_CURRENT in report["codes"]
    assert render.CANARY_DISABLED in report["codes"]


def test_control_unhealthy_flips_ok_false_and_emits_the_matching_code():
    payload = healthy_verify_payload()
    payload["control"]["healthy"] = False
    report = render.build_report(payload)
    assert report["ok"] is False
    assert render.CONTROL_UNHEALTHY in report["codes"]


def test_incomplete_release_identity_is_flagged():
    payload = healthy_verify_payload()
    payload["release_commit"] = ""
    report = render.build_report(payload)
    assert render.RELEASE_IDENTITY_INCOMPLETE in report["codes"]
    assert report["ok"] is False


def test_worker_offline_and_duplicate_and_shared_root_codes():
    payload = healthy_verify_payload()
    payload["workers"].update(
        {"online": 2, "unique_server_ids": 2, "shared_root_ok": 2, "spool_quarantine": 1}
    )
    report = render.build_report(payload)
    codes = set(report["codes"])
    assert render.WORKERS_OFFLINE in codes
    assert render.WORKER_SERVER_ID_DUPLICATE in codes
    assert render.WORKER_SHARED_ROOT_UNAVAILABLE in codes
    assert render.WORKER_SPOOL_QUARANTINE_PRESENT in codes
    assert report["ok"] is False


def test_canary_succeeded_with_artifacts_present_is_ok():
    report = render.build_report(
        healthy_verify_payload(),
        {"enabled": True, "status": "succeeded", "artifacts_present": True},
    )
    assert render.CANARY_SUCCEEDED in report["codes"]
    assert report["ok"] is True


def test_canary_failed_flips_ok_false():
    report = render.build_report(
        healthy_verify_payload(),
        {"enabled": True, "status": "failed", "artifacts_present": False},
    )
    assert render.CANARY_FAILED in report["codes"]
    assert report["ok"] is False


def test_every_emitted_code_is_a_member_of_the_fixed_report_codes():
    report = render.build_report(healthy_verify_payload())
    assert set(report["codes"]) <= set(render.REPORT_CODES)


# ---- redaction: no address, path, secret, or free text ever survives -------


def test_redact_masks_url_and_hostname_shaped_strings():
    node = {"host": "worker-1.internal.example", "control_url": "https://ctrl.internal:8080"}
    redacted = render.redact(node)
    assert redacted["host"] == render.REDACTED_ADDRESS
    assert redacted["control_url"] == render.REDACTED_ADDRESS


def test_redact_masks_absolute_paths_under_path_shaped_keys():
    node = {"shared_root": "/srv/ocr-shared/input"}
    redacted = render.redact(node)
    assert redacted["shared_root"] == render.REDACTED_PATH


def test_redact_masks_secret_shaped_keys_regardless_of_value():
    node = {"database_url": "postgresql://user:pw@host/db", "api_token": "tok-abc"}
    redacted = render.redact(node)
    assert redacted["database_url"] == render.REDACTED_SECRET
    assert redacted["api_token"] == render.REDACTED_SECRET


def test_redact_masks_long_free_text_even_under_an_unsuspicious_key():
    node = {"note": "x" * 200}
    redacted = render.redact(node)
    assert redacted["note"] == render.REDACTED_TEXT


def test_redact_leaves_booleans_counts_and_short_identifiers_alone():
    node = {"total": 3, "verified": True, "tag": "v0.4.0"}
    redacted = render.redact(node)
    assert redacted == node


def test_built_report_never_contains_a_raw_hostname_path_or_token():
    payload = healthy_verify_payload()
    payload["control"]["worker_host"] = "worker-1.internal.example"
    report = render.build_report(payload)
    rendered = json.dumps(report)
    assert "worker-1.internal.example" not in rendered
    assert "/srv/" not in rendered


# ---- markdown rendering -----------------------------------------------------


def test_render_markdown_includes_release_and_code_sections():
    report = render.build_report(healthy_verify_payload())
    markdown = render.render_markdown(report)
    assert "# OcrParser production deployment report" in markdown
    assert report["release"]["commit"] in markdown
    assert "workers_all_online" in markdown


# ---- CLI: --verify / --output-dir, and raw-report/renderer-input parity ----


def test_cli_writes_report_json_and_report_md_matching_build_report(tmp_path):
    verify_path = tmp_path / "verify-abc.json"
    verify_path.write_text(json.dumps(healthy_verify_payload()), encoding="utf-8")
    output_dir = tmp_path / "reports"

    exit_code = render.main(
        [
            "--verify",
            str(verify_path),
            "--output-dir",
            str(output_dir),
            "--generated-at",
            "2026-01-01T00:00:00Z",
        ]
    )

    assert exit_code == 0
    on_disk = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    expected = render.build_report(healthy_verify_payload(), generated_at="2026-01-01T00:00:00Z")
    assert on_disk == expected
    assert (output_dir / "report.md").exists()


def test_cli_exit_code_is_nonzero_when_the_report_is_not_ok(tmp_path):
    payload = healthy_verify_payload()
    payload["control"]["healthy"] = False
    verify_path = tmp_path / "verify-abc.json"
    verify_path.write_text(json.dumps(payload), encoding="utf-8")
    output_dir = tmp_path / "reports"

    exit_code = render.main(["--verify", str(verify_path), "--output-dir", str(output_dir)])

    assert exit_code == 1
    on_disk = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    assert on_disk["ok"] is False


def test_cli_rejects_a_verify_document_that_is_not_a_json_object(tmp_path, capsys):
    verify_path = tmp_path / "verify.json"
    verify_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    exit_code = render.main(
        ["--verify", str(verify_path), "--output-dir", str(tmp_path / "reports")]
    )

    assert exit_code == 2
    assert "error:" in capsys.readouterr().err
