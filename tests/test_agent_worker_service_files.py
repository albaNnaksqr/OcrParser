from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agent_worker_script_has_standard_lifecycle_commands():
    script = (ROOT / "scripts" / "ocr_agent_worker.sh").read_text(encoding="utf-8")

    assert "start|stop|restart|status|logs|doctor|run" in script
    assert "python -m ocr_platform.agent" in script
    assert "--server_id" in script
    assert "--control_url" in script
    assert "--shared_root" in script
    assert "--control_retry_initial_seconds" in script
    assert "--control_retry_max_seconds" in script
    assert "--process_termination_timeout_seconds" in script
    assert "--stop_poll_interval_seconds" in script
    assert "OCR_AGENT_SHARED_ROOTS" in script
    assert "OCR_AGENT_GIT_REF" in script


def test_agent_worker_env_example_documents_required_fields():
    env_file = (ROOT / "configs" / "ocr-agent-worker.env.example").read_text(
        encoding="utf-8"
    )

    assert "OCR_AGENT_SERVER_ID=" in env_file
    assert "OCR_CONTROL_URL=" in env_file
    assert "OCR_REPO_DIR=" in env_file
    assert "OCR_AGENT_WORK_DIR=" in env_file
    assert "OCR_AGENT_PYTHON=" in env_file
    assert "OCR_AGENT_SHARED_ROOTS=" in env_file
    assert "OCR_AGENT_CONTROL_RETRY_INITIAL=" in env_file
    assert "OCR_AGENT_CONTROL_RETRY_MAX=" in env_file
    assert "OCR_AGENT_EVENT_SPOOL_MAX_MB=" in env_file
    assert "OCR_AGENT_TERMINATION_TIMEOUT=" in env_file
    assert "OCR_AGENT_STOP_POLL_INTERVAL=" in env_file
    assert "OCR_CONTROL_API_TOKEN=" in env_file


def test_control_env_example_documents_api_token():
    env_file = (ROOT / "configs" / "ocr-platform-control.env.example").read_text(
        encoding="utf-8"
    )

    assert "OCR_PLATFORM_API_TOKEN=" in env_file


def test_control_env_example_disables_saved_model_profile_keys_for_production():
    env_file = (ROOT / "configs" / "ocr-platform-control.env.example").read_text(
        encoding="utf-8"
    )

    assert "OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS=1" in env_file
    assert "model profile or per-job API keys" in env_file


def test_control_env_example_requires_current_migrations_for_production():
    env_file = (ROOT / "configs" / "ocr-platform-control.env.example").read_text(
        encoding="utf-8"
    )

    assert "OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS=1" in env_file
    assert "Refuse to start if PostgreSQL SQL migrations are not current" in env_file


def test_control_env_example_documents_recent_error_sample_limit():
    env_file = (ROOT / "configs" / "ocr-platform-control.env.example").read_text(
        encoding="utf-8"
    )

    assert "OCR_JOB_RECENT_ERROR_SAMPLE_LIMIT=100" in env_file
    assert "job-level error" in env_file


def test_agent_worker_systemd_unit_points_to_standard_script():
    service = (ROOT / "services" / "ocr-agent-worker.service.example").read_text(
        encoding="utf-8"
    )

    assert "EnvironmentFile=" in service
    assert "SupplementaryGroups=ocr-runtime" in service
    assert "ocr_agent_worker.sh run" in service
    assert "ocr_agent_worker.sh stop" in service
    assert "Restart=always" in service


def test_agent_worker_systemd_template_supports_multiple_instances():
    service = (ROOT / "services" / "ocr-agent-worker@.service.example").read_text(
        encoding="utf-8"
    )

    assert "Description=OCR Platform Agent Worker %i" in service
    assert "SupplementaryGroups=ocr-runtime" in service
    assert "EnvironmentFile=/etc/ocr-agent/%i.env" in service
    assert "ocr_agent_worker.sh run /etc/ocr-agent/%i.env" in service
    assert "ocr_agent_worker.sh stop /etc/ocr-agent/%i.env" in service
    assert "Restart=always" in service


def test_control_systemd_unit_joins_runtime_group_for_shared_manifests():
    service = (ROOT / "services" / "ocr-platform-control.service.example").read_text(
        encoding="utf-8"
    )

    assert "User=ocr-platform" in service
    assert "SupplementaryGroups=ocr-runtime" in service


def test_agent_worker_logrotate_template_covers_worker_logs():
    logrotate = (ROOT / "services" / "ocr-agent-worker.logrotate.example").read_text(
        encoding="utf-8"
    )

    assert "/var/log/ocr-agent/*/*.log" in logrotate
    assert "rotate 14" in logrotate
    assert "compress" in logrotate
    assert "missingok" in logrotate
    assert "copytruncate" in logrotate

