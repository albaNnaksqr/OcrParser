from pathlib import Path

from tools import local_prod_env


def make_config(tmp_path: Path, **overrides) -> local_prod_env.LocalProdConfig:
    values = {
        "root": tmp_path,
        "state_dir": tmp_path / ".local" / "production",
    }
    values.update(overrides)
    return local_prod_env.LocalProdConfig(**values)


def test_default_config_uses_postgres_and_production_guards(tmp_path):
    config = make_config(tmp_path)

    assert config.database_url == (
        "postgresql+psycopg://ocr_platform:ocr_platform_local@127.0.0.1:15432/ocr_platform"
    )

    env = local_prod_env.build_control_env(config)

    assert env["OCR_PLATFORM_DATABASE_URL"] == config.database_url
    assert env["OCR_PLATFORM_REQUIRE_POSTGRES"] == "1"
    assert env["OCR_PLATFORM_AUTO_MIGRATE"] == "0"
    assert env["OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS"] == "1"
    assert env["OCR_PLATFORM_API_TOKEN"] == "local-dev-token"
    assert env["OCR_PLATFORM_REQUIRE_API_TOKEN"] == "1"
    assert env["OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS"] == "0"
    assert env["OCR_PLATFORM_ENABLE_REMOTE_ADMIN"] == "0"
    assert env["OCR_PLATFORM_PORT"] == "38080"


def test_compose_yaml_starts_only_postgres_with_healthcheck(tmp_path):
    config = make_config(tmp_path)

    text = local_prod_env.build_compose_yaml(config)

    assert "postgres:16-alpine" in text
    assert "ocr-platform-local-postgres" in text
    assert "127.0.0.1:15432:5432" in text
    assert "POSTGRES_DB: ocr_platform" in text
    assert "pg_isready -U ocr_platform -d ocr_platform" in text
    assert "ocr_platform.control" not in text


def test_up_plan_applies_migrations_then_starts_control_and_optional_worker(tmp_path):
    config = make_config(
        tmp_path,
        with_worker=True,
        with_mock_ocr=True,
        mock_ocr_port=18080,
        mock_ocr_model="local-mock",
        shared_roots=[str(tmp_path / "shared")],
    )

    plan = local_prod_env.build_up_plan(config, python_executable="/venv/bin/python")
    rendered = "\n".join(step.render() for step in plan)

    assert "docker compose -f" in rendered
    assert "up -d postgres" in rendered
    for action in ("plan", "apply", "verify"):
        assert f"-m ocr_platform.control.migrate_cli {action} --database-url" in rendered
    assert config.database_url in rendered
    assert f"PYTHONPATH={tmp_path}" in rendered
    assert "OCR_PLATFORM_REQUIRE_POSTGRES=1" in rendered
    assert "-m ocr_platform.control" in rendered
    assert "-m ocr_platform.agent" in rendered
    assert "tools/mock_ocr_service.py" in rendered
    assert "--port 18080" in rendered
    assert "--model-name local-mock" in rendered
    assert "--server_id local-worker-01" in rendered
    assert f"--shared_root {tmp_path / 'shared'}" in rendered
    assert rendered.index("migrate_cli plan") < rendered.index("migrate_cli apply")
    assert rendered.index("migrate_cli apply") < rendered.index("migrate_cli verify")
    assert rendered.index("migrate_cli verify") < rendered.index("ocr_platform.control\n")


def test_up_plan_can_start_two_isolated_workers(tmp_path):
    config = make_config(tmp_path, with_worker=True, worker_count=2)

    plan = local_prod_env.build_up_plan(config, python_executable="/venv/bin/python")
    workers = [step for step in plan if step.label.startswith("start local worker")]

    assert len(workers) == 2
    assert config.worker_id_for(0) == "local-worker-01"
    assert config.worker_id_for(1) == "local-worker-02"
    assert workers[0].env["OCR_AGENT_WORK_DIR"] != workers[1].env["OCR_AGENT_WORK_DIR"]
    assert workers[0].env["OCR_AGENT_EVENT_SPOOL_DIR"] != workers[1].env["OCR_AGENT_EVENT_SPOOL_DIR"]
    assert workers[0].argv[workers[0].argv.index("--server_id") + 1] == "local-worker-01"
    assert workers[1].argv[workers[1].argv.index("--server_id") + 1] == "local-worker-02"


def test_worker_runtime_files_preserve_first_worker_compatibility(tmp_path):
    config = make_config(tmp_path, with_worker=True, worker_count=2)

    assert config.worker_pid_file_for(0) == config.state_dir / "worker.pid"
    assert config.worker_env_file_for(0) == config.state_dir / "worker.env"
    assert config.worker_pid_file_for(1) == config.state_dir / "worker-02.pid"
    assert config.worker_env_file_for(1) == config.state_dir / "worker-02.env"


def test_runtime_summary_names_db_ports_env_logs_and_stop_command(tmp_path):
    config = make_config(
        tmp_path,
        with_worker=True,
        with_mock_ocr=True,
        mock_ocr_port=18080,
        mock_ocr_model="local-mock",
        shared_roots=[str(tmp_path / "shared")],
    )

    summary = "\n".join(local_prod_env.build_runtime_summary(config))

    assert "Database URL: postgresql+psycopg://ocr_platform:***@127.0.0.1:15432/ocr_platform" in summary
    assert f"PostgreSQL data: {tmp_path / '.local' / 'production' / 'postgres-data'}" in summary
    assert "Control URL: http://127.0.0.1:38080/ui/" in summary
    assert f"Control env: {tmp_path / '.local' / 'production' / 'control.env'}" in summary
    assert "Control logs:" in summary
    assert "control.out.log" in summary
    assert "control.err.log" in summary
    assert f"Worker env: {tmp_path / '.local' / 'production' / 'worker.env'}" in summary
    assert "Worker logs:" in summary
    assert "worker.out.log" in summary
    assert "worker.err.log" in summary
    assert "Mock OCR API: http://127.0.0.1:18080/v1 (model=local-mock)" in summary
    assert "Mock OCR logs:" in summary
    assert "mock-ocr.out.log" in summary
    assert "mock-ocr.err.log" in summary
    assert "Stop: python3 tools/local_prod_env.py down" in summary


def test_env_file_text_is_shell_compatible(tmp_path):
    config = make_config(tmp_path)

    text = local_prod_env.render_env_file(local_prod_env.build_control_env(config))

    assert "OCR_PLATFORM_DATABASE_URL=postgresql+psycopg://ocr_platform:ocr_platform_local@127.0.0.1:15432/ocr_platform" in text
    assert "OCR_PLATFORM_REQUIRE_POSTGRES=1" in text
    assert "OCR_PLATFORM_AUTO_MIGRATE=0" in text
    assert text.endswith("\n")
    assert "[object Object]" not in text


def test_down_plan_stops_local_services_without_deleting_pg_data_by_default(tmp_path):
    config = make_config(tmp_path, with_worker=True)

    rendered = "\n".join(step.render() for step in local_prod_env.build_down_plan(config))

    assert "stop local worker" in rendered
    assert "stop mock OCR service" in rendered
    assert "stop local control" in rendered
    assert "docker compose -f" in rendered
    assert " down" in rendered
    assert " -v" not in rendered

    rendered_with_volumes = "\n".join(
        step.render() for step in local_prod_env.build_down_plan(config, volumes=True)
    )
    assert " down -v" in rendered_with_volumes


def test_ensure_shared_roots_creates_root_and_job_subdirectories(tmp_path):
    shared = tmp_path / "shared"
    config = make_config(tmp_path, shared_roots=[str(shared)])

    created = local_prod_env.ensure_shared_roots(config)

    assert shared.is_dir()
    for name in ("input", "output", "manifests"):
        assert (shared / name).is_dir()
    assert set(created) == {shared, *(shared / name for name in ("input", "output", "manifests"))}


def test_ensure_shared_roots_preserves_existing_content_and_is_idempotent(tmp_path):
    shared = tmp_path / "shared"
    existing = shared / "input" / "keep.pdf"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing-content")
    config = make_config(tmp_path, shared_roots=[str(shared)])

    first = local_prod_env.ensure_shared_roots(config)
    second = local_prod_env.ensure_shared_roots(config)

    assert existing.read_bytes() == b"existing-content"
    assert shared / "input" not in first
    assert second == []
    assert (shared / "output").is_dir()
    assert (shared / "manifests").is_dir()


def test_ensure_shared_roots_rejects_a_shared_root_that_is_a_file(tmp_path):
    shared = tmp_path / "shared"
    shared.write_text("not a directory", encoding="utf-8")
    config = make_config(tmp_path, shared_roots=[str(shared)])

    try:
        local_prod_env.ensure_shared_roots(config)
    except RuntimeError as exc:
        assert "not a directory" in str(exc)
    else:  # pragma: no cover - guards against a silently weakened check
        raise AssertionError("expected RuntimeError for a non-directory shared root")


def test_up_dry_run_prints_the_plan_without_creating_shared_directories(tmp_path, capsys):
    shared = tmp_path / "shared"
    args = local_prod_env.build_parser().parse_args(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "up",
            "--with-worker",
            "--with-mock-ocr",
            "--shared-root",
            str(shared),
            "--dry-run",
        ]
    )

    assert local_prod_env.command_up(args) == 0

    output = capsys.readouterr().out
    assert "create shared root directories" in output
    assert str(shared / "input") in output
    assert "upsert mock OCR model profile" in output
    assert not shared.exists()
    assert not (tmp_path / "state").exists()


def test_mock_model_profile_is_low_concurrency_keyless_and_not_default(tmp_path):
    config = make_config(tmp_path, with_mock_ocr=True, mock_ocr_port=19000, mock_ocr_model="local-mock")

    request = local_prod_env.build_mock_model_profile_request(config)

    assert local_prod_env.MOCK_MODEL_PROFILE_ID == "mock_ocr_local"
    assert request["engine"] == "dotsocr"
    assert request["ip"] == "127.0.0.1"
    assert request["port"] == 19000
    assert request["model_name"] == "local-mock"
    assert request["page_concurrency"] == 1
    assert request["requires_api_key"] is False
    assert request["is_default"] is False
    assert request["extra_args"]["file_concurrency"] == 1
    assert request["extra_args"]["api_concurrency_max"] == 1
    assert "api_key" not in request
    assert "api_key" not in request["extra_args"]


def test_mock_model_profile_extra_args_are_accepted_by_the_parser_contract(tmp_path):
    from ocr_parser.config import ParserConfig

    config = make_config(tmp_path, with_mock_ocr=True)
    request = local_prod_env.build_mock_model_profile_request(config)

    normalized = ParserConfig.validate_option_dict(
        request["extra_args"], context="model profile extra_args"
    )

    assert normalized["file_concurrency"] == 1


def test_mock_model_profile_is_not_part_of_the_default_bootstrap_profiles():
    from ocr_platform.control.domains.common import DEFAULT_MODEL_PROFILES

    assert local_prod_env.MOCK_MODEL_PROFILE_ID not in DEFAULT_MODEL_PROFILES
    assert DEFAULT_MODEL_PROFILES["dotsocr_15"]["is_default"] is True
    assert DEFAULT_MODEL_PROFILES["dotsocr_15"]["requires_api_key"] is True


def test_up_plan_omits_shared_root_and_mock_profile_steps_when_not_requested(tmp_path):
    config = make_config(tmp_path, with_worker=True)

    rendered = "\n".join(step.render() for step in local_prod_env.build_up_plan(config))

    assert "create shared root directories" not in rendered
    assert "upsert mock OCR model profile" not in rendered


def test_parser_builds_local_prod_config_with_optional_worker(tmp_path):
    parser = local_prod_env.build_parser()

    args = parser.parse_args(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "up",
            "--with-worker",
            "--worker-count",
            "2",
            "--with-mock-ocr",
            "--shared-root",
            str(tmp_path / "shared"),
            "--postgres-port",
            "55432",
            "--control-port",
            "18080",
            "--mock-ocr-port",
            "19000",
            "--mock-ocr-model",
            "local-mock",
            "--api-token",
            "dev-secret",
            "--dry-run",
        ]
    )
    config = local_prod_env.config_from_args(args, root=tmp_path)

    assert args.command == "up"
    assert config.with_worker is True
    assert config.worker_count == 2
    assert config.with_mock_ocr is True
    assert config.mock_ocr_port == 19000
    assert config.mock_ocr_model == "local-mock"
    assert config.shared_roots == [str(tmp_path / "shared")]
    assert config.postgres_port == 55432
    assert config.control_port == 18080
    assert config.api_token == "dev-secret"
    assert config.database_url.endswith("@127.0.0.1:55432/ocr_platform")
