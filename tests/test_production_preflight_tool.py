import importlib.util
import json
import subprocess
import sys
from pathlib import Path


TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "production_preflight.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("production_preflight", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_production_preflight_parser_exposes_read_only_deployment_checks():
    tool = load_tool()
    parser = tool.build_parser()

    args = parser.parse_args(
        [
            "--host",
            "control.example.internal",
            "--host",
            "worker-1.example.internal",
            "--user",
            "ocr_user",
            "--identity-file",
            "~/.ssh/ocr_prod_ed25519",
            "--shared-root",
            "/shared/ocr-data",
            "--platform-root",
            "/shared/ocr-data/ocr-platform",
            "--control-host",
            "control.example.internal",
            "--control-url",
            "http://control.example.internal:8080",
            "--expected-git-ref",
            "9e45c90",
            "--json",
        ]
    )

    assert args.hosts == ["control.example.internal", "worker-1.example.internal"]
    assert args.user == "ocr_user"
    assert args.identity_file == "~/.ssh/ocr_prod_ed25519"
    assert args.shared_root == "/shared/ocr-data"
    assert args.platform_root == "/shared/ocr-data/ocr-platform"
    assert args.control_host == "control.example.internal"
    assert args.control_url == "http://control.example.internal:8080"
    assert args.expected_git_ref == "9e45c90"
    assert args.json is True


def test_remote_probe_script_is_read_only_and_checks_shared_disk_permissions():
    tool = load_tool()

    script = tool.remote_probe_script()

    for forbidden in (
        " chown ",
        " chmod ",
        " mkdir ",
        " rm ",
        " cp ",
        " mv ",
        "systemctl start",
        "systemctl restart",
        "systemctl enable",
        "systemctl stop",
        "tee ",
    ):
        assert forbidden not in script
    assert "findmnt" in script
    assert "sudo -n -u ocr-agent test -w" in script
    assert "sudo -n -u ocr-platform test -w" in script
    assert "pg_isready" in script
    assert "/api/system/database" in script


def test_remote_probe_treats_legacy_control_database_endpoint_404_as_reachable_warning():
    tool = load_tool()

    script = tool.remote_probe_script()

    assert "401|403) emit_check control_api_reachable ok" in script
    assert "404) emit_check control_api_reachable warn" in script


def test_remote_probe_falls_back_to_agent_process_git_ref_when_repo_is_unreadable():
    tool = load_tool()

    script = tool.remote_probe_script()

    assert "agent_git_ref" in script
    assert "--git_ref" in script
    assert 'git_ref="$agent_git_ref"' in script


def test_parse_probe_output_summarizes_failed_mount_and_control_checks():
    tool = load_tool()

    report = tool.parse_probe_output(
        host="control.example.internal",
        returncode=0,
        stdout="\n".join(
            [
                "FACT\thostname\tocr_user",
                "FACT\tgit_ref\tebd07bb",
                "CHECK\tshared_root_mounted\tfail\t/shared/ocr-data resolves to /",
                "CHECK\tplatform_root_writable_by_ocr_agent\tok\twritable",
                "CHECK\tpostgres_ready\tfail\t127.0.0.1:5432 - no response",
                "CHECK\tcontrol_api_ready\tfail\tconnection refused",
            ]
        ),
        stderr="",
    )

    payload = report.to_dict()

    assert payload["host"] == "control.example.internal"
    assert payload["ok"] is False
    assert payload["facts"]["hostname"] == "ocr_user"
    assert payload["facts"]["git_ref"] == "ebd07bb"
    assert payload["checks"]["shared_root_mounted"]["status"] == "fail"
    assert payload["checks"]["postgres_ready"]["detail"] == "127.0.0.1:5432 - no response"


def test_run_host_probe_uses_batchmode_ssh_and_does_not_mutate_remote(monkeypatch):
    tool = load_tool()
    captured = {}

    def fake_run(command, *, input, text, capture_output, timeout, check):
        captured["command"] = command
        captured["input"] = input
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="FACT\thostname\tocr_user\nCHECK\tssh_probe\tok\tconnected\n",
            stderr="",
        )

    monkeypatch.setattr(tool.subprocess, "run", fake_run)

    report = tool.run_host_probe(
        host="worker-1.example.internal",
        user="ocr_user",
        identity_file="~/.ssh/ocr_prod_ed25519",
        shared_root="/shared/ocr-data",
        platform_root="/shared/ocr-data/ocr-platform",
        control_url="http://control.example.internal:8080",
        repo_dir="/opt/ocr-platform/ocrparser",
        expected_git_ref="9e45c90",
        control_host="control.example.internal",
        timeout=12,
        ssh_options=["StrictHostKeyChecking=accept-new"],
    )

    command = captured["command"]
    assert command[0] == "ssh"
    assert "BatchMode=yes" in command
    assert "ocr_user@worker-1.example.internal" in command
    assert "bash" in command
    assert "/shared/ocr-data" in command
    assert "/shared/ocr-data/ocr-platform" in command
    assert "sudo -n -u ocr-agent test -w" in captured["input"]
    assert captured["timeout"] == 12
    assert report.ok is True


def test_json_report_marks_overall_failed_when_any_host_fails():
    tool = load_tool()
    healthy = tool.HostReport(
        host="control.example.internal",
        returncode=0,
        facts={},
        checks={"ssh_probe": tool.CheckResult("ok", "connected")},
        stderr="",
    )
    broken = tool.HostReport(
        host="worker-1.example.internal",
        returncode=0,
        facts={},
        checks={"shared_root_mounted": tool.CheckResult("fail", "not mounted")},
        stderr="",
    )

    payload = tool.build_report([healthy, broken]).to_dict()

    assert payload["ok"] is False
    assert json.dumps(payload, sort_keys=True)


def test_warning_checks_do_not_fail_overall_report():
    tool = load_tool()
    host = tool.HostReport(
        host="control.example.internal",
        returncode=0,
        facts={},
        checks={"control_api_reachable": tool.CheckResult("warn", "legacy endpoint")},
        stderr="",
    )

    assert host.ok is True
    assert tool.build_report([host]).ok is True
