import stat
from pathlib import Path

import pytest

from tools import install_production as installer


def test_control_parser_defaults_disable_api_auth():
    args = installer.build_parser().parse_args(
        [
            "control",
            "--service-user",
            "ocr_user",
            "--service-group",
            "ocr-runtime",
            "--database-url",
            "postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform",
        ]
    )

    config = installer.config_from_args(args)

    assert isinstance(config, installer.ControlInstallConfig)
    assert config.role == "control"
    assert config.service_user == "ocr_user"
    assert config.service_group == "ocr-runtime"
    assert config.require_api_token is False
    assert config.api_token is None
    assert config.host == "127.0.0.1"
    assert config.port == 8080


def test_worker_parser_accepts_shared_roots_without_api_token():
    args = installer.build_parser().parse_args(
        [
            "worker",
            "--service-user",
            "ocr_user",
            "--service-group",
            "ocr-runtime",
            "--server-id",
            "ocr-worker-03",
            "--control-url",
            "http://control.example.internal:8080",
            "--shared-root",
            "/shared/ocr-data",
        ]
    )

    config = installer.config_from_args(args)

    assert isinstance(config, installer.WorkerInstallConfig)
    assert config.role == "worker"
    assert config.server_id == "ocr-worker-03"
    assert config.control_url == "http://control.example.internal:8080"
    assert config.shared_roots == ["/shared/ocr-data"]
    assert config.control_api_token is None


def test_non_interactive_worker_defaults_server_id_to_primary_ip(monkeypatch):
    monkeypatch.setattr(installer, "detect_primary_ip", lambda control_url="": "worker-1.example.internal")
    args = installer.build_parser().parse_args(
        [
            "worker",
            "--service-user",
            "ocr_user",
            "--service-group",
            "ocr-runtime",
            "--control-url",
            "http://control.example.internal:8080",
            "--shared-root",
            "/shared/ocr-data",
            "--non-interactive",
        ]
    )

    config = installer.config_from_args(args)

    assert config.server_id == "worker-1.example.internal"
    assert config.work_dir == Path("/var/lib/ocr-agent/worker-1.example.internal")
    assert config.log_dir == Path("/var/log/ocr-agent/worker-1.example.internal")


def test_interactive_worker_prompts_with_primary_ip_default(monkeypatch):
    prompts = []
    answers = iter(["http://control.example.internal:8080", "", "/shared/ocr-data"])

    monkeypatch.setattr(installer, "detect_primary_ip", lambda control_url="": "worker-1.example.internal")

    def fake_prompt(label, default=None, secret=False):
        prompts.append((label, default))
        answer = next(answers)
        return answer or (default or "")

    monkeypatch.setattr(
        installer,
        "_prompt",
        fake_prompt,
    )

    args = installer.build_parser().parse_args(
        [
            "worker",
            "--service-user",
            "ocr_user",
            "--service-group",
            "ocr-runtime",
        ]
    )

    config = installer.config_from_args(args)

    assert prompts[0] == ("Control URL", None)
    assert prompts[1] == ("Worker server id", "worker-1.example.internal")
    assert config.server_id == "worker-1.example.internal"


def test_config_file_supplies_control_values(tmp_path):
    config_path = tmp_path / "control-install.json"
    config_path.write_text(
        """{
          "service_user": "ocr_user",
          "service_group": "ocr-runtime",
          "database_url": "postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform",
          "host": "127.0.0.1",
          "port": 18080
        }""",
        encoding="utf-8",
    )

    args = installer.build_parser().parse_args(["--config", str(config_path), "control"])
    config = installer.config_from_args(args)

    assert config.service_user == "ocr_user"
    assert config.service_group == "ocr-runtime"
    assert config.database_url.startswith("postgresql+psycopg://")
    assert config.host == "127.0.0.1"
    assert config.port == 18080


def test_non_interactive_worker_requires_shared_root():
    args = installer.build_parser().parse_args(
        [
            "worker",
            "--service-user",
            "ocr_user",
            "--service-group",
            "ocr-runtime",
            "--server-id",
            "ocr-worker-03",
            "--control-url",
            "http://control.example.internal:8080",
            "--non-interactive",
        ]
    )

    with pytest.raises(installer.InstallConfigError, match="--shared-root"):
        installer.config_from_args(args)


def test_validate_service_identity_requires_existing_user(monkeypatch):
    monkeypatch.setattr(installer.pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))

    errors = installer.validate_service_identity("missing-user", "ocr-runtime")

    assert errors == [
        installer.ValidationError(
            code="service_user_missing",
            message="service user missing-user does not exist",
            fix="Create the user or pass an existing --service-user.",
        )
    ]


def test_validate_service_identity_requires_group_membership(monkeypatch):
    class Pw:
        pw_gid = 100

    class PrimaryGroup:
        gr_name = "users"

    class RuntimeGroup:
        gr_mem = []

    monkeypatch.setattr(installer.pwd, "getpwnam", lambda name: Pw())
    monkeypatch.setattr(
        installer.grp,
        "getgrnam",
        lambda name: RuntimeGroup() if name == "ocr-runtime" else PrimaryGroup(),
    )
    monkeypatch.setattr(installer.grp, "getgrgid", lambda gid: PrimaryGroup())

    errors = installer.validate_service_identity("ocr_user", "ocr-runtime")

    assert errors[0].code == "service_user_not_in_group"
    assert "sudo usermod -aG ocr-runtime ocr_user" in errors[0].fix


def test_validate_worker_paths_uses_service_user_runner(tmp_path):
    checked = []

    def fake_runner(user, path, mode):
        checked.append((user, Path(path), mode))
        return path.name != "blocked"

    config = installer.WorkerInstallConfig(
        role="worker",
        service_user="ocr_user",
        service_group="ocr-runtime",
        server_id="ocr-worker-03",
        control_url="http://control:8080",
        work_dir=tmp_path / "work",
        log_dir=tmp_path / "logs",
        event_spool_dir=tmp_path / "work" / "event-spool",
        shared_roots=[str(tmp_path / "shared"), str(tmp_path / "blocked")],
    )

    errors = installer.validate_worker_paths(config, can_access_as_user=fake_runner)

    assert checked == [
        ("ocr_user", tmp_path / "work", "rw"),
        ("ocr_user", tmp_path / "logs", "rw"),
        ("ocr_user", tmp_path / "work" / "event-spool", "rw"),
        ("ocr_user", tmp_path / "shared", "rw"),
        ("ocr_user", tmp_path / "blocked", "rw"),
    ]
    assert errors[0].code == "shared_root_not_writable"


def test_build_control_plan_can_include_dependency_install():
    config = installer.ControlInstallConfig(
        role="control",
        service_user="ocr_user",
        service_group="ocr-runtime",
        database_url="postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform",
        install_dependencies=True,
    )

    plan = installer.build_install_plan(config, validation_errors=[])

    assert any(action.description == "create Python virtual environment" for action in plan.actions)
    assert any(action.description == "install Python dependencies" for action in plan.actions)


def test_check_control_connectivity_uses_token_when_present(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request.full_url, dict(request.header_items()), timeout))

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        return Response()

    monkeypatch.setattr(installer.urllib.request, "urlopen", fake_urlopen)

    result = installer.check_control_connectivity(
        "http://control:8080",
        api_token="secret-token-123456",
    )

    assert result is None
    assert requests[0][0] == "http://control:8080/api/servers"
    headers = {key.lower(): value for key, value in requests[0][1].items()}
    assert headers["x-ocr-platform-token"] == "secret-token-123456"


def test_render_control_env_disables_api_auth_by_default():
    config = installer.ControlInstallConfig(
        role="control",
        service_user="ocr_user",
        service_group="ocr-runtime",
        database_url="postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform",
    )

    text = installer.render_control_env(config)

    assert "OCR_PLATFORM_REQUIRE_API_TOKEN=0" in text
    assert "OCR_PLATFORM_API_TOKEN=" not in text
    assert "OCR_PLATFORM_DATABASE_URL=postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform" in text


def test_render_control_env_can_enable_api_auth():
    config = installer.ControlInstallConfig(
        role="control",
        service_user="ocr_user",
        service_group="ocr-runtime",
        database_url="postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform",
        require_api_token=True,
        api_token="secret-token-123456",
    )

    text = installer.render_control_env(config)

    assert "OCR_PLATFORM_REQUIRE_API_TOKEN=1" in text
    assert "OCR_PLATFORM_API_TOKEN=secret-token-123456" in text


def test_render_worker_env_omits_token_when_not_set():
    config = installer.WorkerInstallConfig(
        role="worker",
        service_user="ocr_user",
        service_group="ocr-runtime",
        server_id="ocr-worker-03",
        control_url="http://control:8080",
        shared_roots=["/shared/ocr-data"],
    )

    text = installer.render_worker_env(config)

    assert "OCR_AGENT_SERVER_ID=ocr-worker-03" in text
    assert "OCR_CONTROL_URL=http://control:8080" in text
    assert "OCR_CONTROL_API_TOKEN=" not in text
    assert "OCR_AGENT_SHARED_ROOTS=/shared/ocr-data" in text


def test_redact_token_keeps_suffix_only():
    assert installer.redact_secret("secret-token-123456") == "************3456"


def test_render_control_service_uses_service_identity():
    config = installer.ControlInstallConfig(
        role="control",
        service_user="ocr_user",
        service_group="ocr-runtime",
        database_url="postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform",
    )

    text = installer.render_control_service(config)

    assert "User=ocr_user" in text
    assert "Group=ocr-runtime" in text
    assert "EnvironmentFile=/etc/ocr-platform/control.env" in text
    assert "ExecStart=/opt/ocr-platform/ocrparser/.venv/bin/python -u -m ocr_platform.control" in text


def test_build_control_plan_warns_when_auth_disabled_on_public_bind():
    config = installer.ControlInstallConfig(
        role="control",
        service_user="ocr_user",
        service_group="ocr-runtime",
        database_url="postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform",
        host="0.0.0.0",
        require_api_token=False,
    )

    plan = installer.build_install_plan(config, validation_errors=[])

    assert plan.role == "control"
    assert "Control API auth is disabled" in "\n".join(plan.warnings)
    assert any(action.description == "write control env file" for action in plan.actions)


def test_production_roles_install_platform_extra():
    config = installer.ControlInstallConfig(
        role="control",
        service_user="ocr_user",
        service_group="ocr-runtime",
        database_url="postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform",
        install_dependencies=True,
    )

    plan = installer.build_install_plan(config, validation_errors=[])
    action = next(item for item in plan.actions if item.description == "install Python dependencies")

    assert action.command[-2:] == ["-e", "/opt/ocr-platform/ocrparser[platform]"]


def test_non_loopback_control_install_requires_enabled_token_auth():
    config = installer.ControlInstallConfig(
        role="control",
        service_user="ocr_user",
        service_group="ocr-runtime",
        database_url="postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform",
        host="0.0.0.0",
        require_api_token=False,
    )

    errors = installer.collect_validation_errors(config)

    assert any(error.code == "non_loopback_control_requires_token" for error in errors)


def test_build_worker_plan_contains_shared_roots():
    config = installer.WorkerInstallConfig(
        role="worker",
        service_user="ocr_user",
        service_group="ocr-runtime",
        server_id="ocr-worker-03",
        control_url="http://control:8080",
        shared_roots=["/shared/ocr-data"],
    )

    plan = installer.build_install_plan(config, validation_errors=[])

    assert plan.role == "worker"
    assert "/shared/ocr-data" in installer.format_plan(plan)


def test_main_dry_run_prints_plan_without_applying(capsys, monkeypatch):
    applied = []
    monkeypatch.setattr(installer, "validate_service_identity", lambda user, group: [])
    monkeypatch.setattr(installer, "apply_plan", lambda plan: applied.append(plan))

    exit_code = installer.main(
        [
            "control",
            "--service-user",
            "ocr_user",
            "--service-group",
            "ocr-runtime",
            "--database-url",
            "postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert applied == []
    assert "Install plan: control" in capsys.readouterr().out


def test_main_non_interactive_requires_yes(monkeypatch):
    monkeypatch.setattr(installer, "validate_service_identity", lambda user, group: [])

    exit_code = installer.main(
        [
            "control",
            "--service-user",
            "ocr_user",
            "--service-group",
            "ocr-runtime",
            "--database-url",
            "postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform",
            "--non-interactive",
        ]
    )

    assert exit_code == 2


def test_format_plan_redacts_api_token():
    config = installer.ControlInstallConfig(
        role="control",
        service_user="ocr_user",
        service_group="ocr-runtime",
        database_url="postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform",
        require_api_token=True,
        api_token="secret-token-123456",
    )
    plan = installer.build_install_plan(config, validation_errors=[])

    text = installer.format_plan(plan)

    assert "secret-token-123456" not in text
    assert "************3456" in text


def test_apply_plan_writes_files_and_runs_commands(tmp_path, monkeypatch):
    commands = []
    target = tmp_path / "etc" / "control.env"
    plan = installer.InstallPlan(
        role="control",
        summary=[],
        warnings=[],
        validation_errors=[],
        actions=[
            installer.PlanAction("write env", str(target), content="KEY=value\n"),
            installer.PlanAction("reload", "systemd", command=["systemctl", "daemon-reload"]),
        ],
    )
    monkeypatch.setattr(installer.subprocess, "run", lambda command, check: commands.append((command, check)))

    installer.apply_plan(plan)

    assert target.read_text(encoding="utf-8") == "KEY=value\n"
    assert commands == [(["systemctl", "daemon-reload"], True)]


def test_apply_plan_never_creates_secret_env_with_group_or_world_permissions(tmp_path, monkeypatch):
    observed_modes_before_chmod = []
    real_chmod = installer.os.chmod
    old_umask = installer.os.umask(0o022)

    def chmod_spy(path, mode):
        observed_modes_before_chmod.append(stat.S_IMODE(Path(path).stat().st_mode))
        return real_chmod(path, mode)

    monkeypatch.setattr(installer.os, "chmod", chmod_spy)
    target = tmp_path / "control.env"
    plan = installer.InstallPlan(
        role="control",
        summary=[],
        warnings=[],
        validation_errors=[],
        actions=[
            installer.PlanAction("write env", str(target), content="TOKEN=secret\n"),
        ],
    )

    try:
        installer.apply_plan(plan)
    finally:
        installer.os.umask(old_umask)

    assert all(mode & 0o077 == 0 for mode in observed_modes_before_chmod)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_interactive_control_prompts_for_missing_values(monkeypatch):
    answers = iter(
        [
            "ocr_user",
            "ocr-runtime",
            "postgresql+psycopg://ocr_platform:secret@db:5432/ocr_platform",
        ]
    )
    monkeypatch.setattr(installer, "_prompt", lambda label, default=None, secret=False: next(answers))

    args = installer.build_parser().parse_args(["control"])
    config = installer.config_from_args(args)

    assert config.service_user == "ocr_user"
    assert config.service_group == "ocr-runtime"
    assert config.database_url.startswith("postgresql+psycopg://")


def test_non_interactive_does_not_prompt(monkeypatch):
    monkeypatch.setattr(
        installer,
        "_prompt",
        lambda label, default=None, secret=False: (_ for _ in ()).throw(AssertionError("prompted")),
    )
    args = installer.build_parser().parse_args(["control", "--non-interactive"])

    with pytest.raises(installer.InstallConfigError, match="--service-user"):
        installer.config_from_args(args)


def test_readme_references_production_installer():
    root = Path(__file__).resolve().parents[1]

    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "tools/install_production.py control" in readme
    assert "tools/install_production.py worker" in readme
