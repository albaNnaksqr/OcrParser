from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_backup_restore_runbook_covers_pg_manifest_and_validation():
    runbook = ROOT / "docs" / "ocr-platform-backup-restore.zh-CN.md"

    assert runbook.exists()
    text = runbook.read_text(encoding="utf-8")
    for required in (
        "pg_dump",
        "pg_restore",
        "manifest_root",
        "rsync",
        "/api/jobs/{job_id}/manifest/integrity",
        "/api/jobs/{job_id}/manifest/freeze-report",
        "schema_migrations",
        "OCR_PLATFORM_DATABASE_URL",
        "OCR_JOB_FILE_DETAIL_LIMIT",
        "OCR_JOB_EVENT_DETAIL_LIMIT",
    ):
        assert required in text
    assert "JSONL manifest" in text
    assert "shard" in text
    assert "可重建" in text


def test_english_backup_restore_runbook_covers_pg_manifest_and_validation():
    runbook = ROOT / "docs" / "ocr-platform-backup-restore.md"

    assert runbook.exists()
    text = runbook.read_text(encoding="utf-8")
    for required in (
        "pg_dump",
        "pg_restore",
        "manifest_root",
        "rsync",
        "/api/jobs/{job_id}/manifest/integrity",
        "/api/jobs/{job_id}/manifest/freeze-report",
        "schema_migrations",
        "OCR_PLATFORM_DATABASE_URL",
        "OCR_JOB_FILE_DETAIL_LIMIT",
        "OCR_JOB_EVENT_DETAIL_LIMIT",
    ):
        assert required in text
    assert "JSONL manifest" in text
    assert "shard" in text
    assert "rebuildable" in text


def test_deployment_docs_link_backup_restore_runbook():
    en_doc = (ROOT / "docs" / "ocr-platform-deployment.md").read_text(encoding="utf-8")
    zh_doc = (ROOT / "docs" / "ocr-platform-deployment.zh-CN.md").read_text(encoding="utf-8")
    codex_doc = (ROOT / "docs" / "codex-production-deployment-guide.zh-CN.md").read_text(encoding="utf-8")

    assert "ocr-platform-backup-restore.md" in en_doc
    assert "ocr-platform-backup-restore.zh-CN.md" in zh_doc
    assert "ocr-platform-backup-restore.zh-CN.md" in codex_doc


def test_deployment_docs_require_current_postgres_migrations_guard():
    en_doc = (ROOT / "docs" / "ocr-platform-deployment.md").read_text(encoding="utf-8")
    zh_doc = (ROOT / "docs" / "ocr-platform-deployment.zh-CN.md").read_text(encoding="utf-8")

    assert "OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS=1" in en_doc
    assert "database migrations are current" in en_doc
    assert "OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS=1" in zh_doc
    assert "SQL migration" in zh_doc


def test_deployment_docs_stress_scan_unit_completion_shard_indexes():
    en_doc = (ROOT / "docs" / "ocr-platform-deployment.md").read_text(encoding="utf-8")
    zh_doc = (ROOT / "docs" / "ocr-platform-deployment.zh-CN.md").read_text(encoding="utf-8")
    codex_doc = (ROOT / "docs" / "codex-production-deployment-guide.zh-CN.md").read_text(
        encoding="utf-8"
    )

    for text in (en_doc, zh_doc, codex_doc):
        assert "--scan-unit-shards" in text
        assert "scan_unit_completion_shards" in text


def test_deployment_docs_explain_generated_and_executable_shard_counts():
    en_doc = (ROOT / "docs" / "ocr-platform-deployment.md").read_text(encoding="utf-8")
    zh_doc = (ROOT / "docs" / "ocr-platform-deployment.zh-CN.md").read_text(encoding="utf-8")

    for text in (en_doc, zh_doc):
        assert "shards_created" in text
        assert "executable_shards" in text


def test_deployment_docs_define_runtime_group_shared_disk_permissions():
    en_doc = (ROOT / "docs" / "ocr-platform-deployment.md").read_text(encoding="utf-8")
    zh_doc = (ROOT / "docs" / "ocr-platform-deployment.zh-CN.md").read_text(encoding="utf-8")
    agent_doc = (ROOT / "docs" / "ocr-agent-worker-service.md").read_text(
        encoding="utf-8"
    )

    for text in (en_doc, zh_doc, agent_doc):
        assert "ocr-runtime" in text
        assert "chmod 2775" in text
        assert "sudo -u ocr-agent test -w" in text
        assert "sudo -u ocr-platform test -w" in text
        assert "/shared/ocr-data/ocr-platform" in text
        assert "OCR_AGENT_SHARED_ROOTS=/shared/ocr-data" in text
        assert "input_dir=/shared/ocr-data/project-a/pdfs" in text
        assert "output_dir=/shared/ocr-data/project-a/output" in text
        assert "manifest_root=/shared/ocr-data/ocr-platform/manifests" in text
        assert "UID/GID" in text


def test_deployment_docs_include_read_only_production_preflight_tool():
    en_doc = (ROOT / "docs" / "ocr-platform-deployment.md").read_text(encoding="utf-8")
    zh_doc = (ROOT / "docs" / "ocr-platform-deployment.zh-CN.md").read_text(encoding="utf-8")
    codex_doc = (ROOT / "docs" / "codex-production-deployment-guide.zh-CN.md").read_text(
        encoding="utf-8"
    )

    for text in (en_doc, zh_doc, codex_doc):
        assert "tools/production_preflight.py" in text
        assert "--shared-root /shared/ocr-data" in text
        assert "--platform-root /shared/ocr-data/ocr-platform" in text
        assert "--control-url http://control.example.internal:8080" in text
        assert "read-only" in text or "只读" in text
