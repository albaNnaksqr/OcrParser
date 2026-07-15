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
    assert "tools/apply_control_migrations.py --database-url" in rendered
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
    assert rendered.index("apply_control_migrations.py") < rendered.index("ocr_platform.control")


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


def test_parser_builds_local_prod_config_with_optional_worker(tmp_path):
    parser = local_prod_env.build_parser()

    args = parser.parse_args(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "up",
            "--with-worker",
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
    assert config.with_mock_ocr is True
    assert config.mock_ocr_port == 19000
    assert config.mock_ocr_model == "local-mock"
    assert config.shared_roots == [str(tmp_path / "shared")]
    assert config.postgres_port == 55432
    assert config.control_port == 18080
    assert config.api_token == "dev-secret"
    assert config.database_url.endswith("@127.0.0.1:55432/ocr_platform")
