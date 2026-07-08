from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_readme_agent_env_path_matches_worker_templates():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    env_template = (ROOT / "configs" / "ocr-agent-worker.env.example").read_text(encoding="utf-8")
    service_template = (ROOT / "services" / "ocr-agent-worker.service.example").read_text(encoding="utf-8")

    assert "/etc/ocr-agent/worker.env" in readme
    assert "/etc/ocr-agent/worker.env" in env_template
    assert "/etc/ocr-agent/worker.env" in service_template
    assert "/etc/ocr-platform/agent.env" not in readme


def test_readme_points_ui_users_to_deployment_doctor():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Deployment Doctor" in readme
    assert "/healthz" in readme
    assert "/readyz" in readme
    assert "/api/system/diagnostics" in readme


def test_docs_define_three_startup_modes_and_local_prod_entrypoint():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    deployment = (ROOT / "docs" / "ocr-platform-deployment.md").read_text(encoding="utf-8")
    deployment_zh = (ROOT / "docs" / "ocr-platform-deployment.zh-CN.md").read_text(
        encoding="utf-8"
    )

    for text in (readme, deployment):
        assert "local dev" in text
        assert "single-machine production-like" in text
        assert "real production" in text
        assert "tools/local_prod_env.py up" in text
        assert "--with-mock-ocr" in text
        assert "mock-ocr" in text
        assert "tools/local_prod_env.py down" in text

    assert "本地开发" in deployment_zh
    assert "单机生产近似" in deployment_zh
    assert "真实生产" in deployment_zh
    assert "tools/local_prod_env.py up" in deployment_zh
    assert "--with-mock-ocr" in deployment_zh


def test_startup_mode_docs_spell_out_db_ports_env_logs_and_stop():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    deployment = (ROOT / "docs" / "ocr-platform-deployment.md").read_text(encoding="utf-8")
    deployment_zh = (ROOT / "docs" / "ocr-platform-deployment.zh-CN.md").read_text(
        encoding="utf-8"
    )

    for text in (readme, deployment):
        assert "| Mode | DB | Ports | Env | Logs | Stop |" in text
        assert "`local dev`" in text
        assert "`single-machine production-like`" in text
        assert "`real production`" in text
        assert "`sqlite:///./ocr_platform.db`" in text
        assert "`postgresql+psycopg://...@127.0.0.1:15432/ocr_platform`" in text
        assert "`.local/production/control.env`" in text
        assert "`.local/production/logs/control.out.log`" in text
        assert "`/etc/ocr-platform/control.env`" in text
        assert "`journalctl -u ocr-platform-control`" in text
        assert "`systemctl stop ocr-platform-control`" in text

    assert "| 模式 | DB | 端口 | Env | 日志 | 停止 |" in deployment_zh
    assert "`sqlite:///./ocr_platform.db`" in deployment_zh
    assert "`postgresql+psycopg://...@127.0.0.1:15432/ocr_platform`" in deployment_zh
    assert "`.local/production/control.env`" in deployment_zh
    assert "`/etc/ocr-platform/control.env`" in deployment_zh
