import json

from ocr_parser.infra.status_sidecar import write_status_sidecar


class ParserWithSecretConfig:
    engine = "dotsocr"
    model_name = "dotsocr-test"
    page_concurrency = 4
    engine_config = {
        "temperature": 0,
        "api_key": "secret-value",
        "nested": {"api_key_env_var": "DOTS_API_KEY", "api_key": "nested-secret"},
    }


class ParserWithSuffixSecretConfig:
    engine = "dotsocr"
    engine_config = {
        "bearer_token": "token-secret",
        "client_secret": "client-secret",
        "db_password": "password-secret",
        "safe_tokenizer": "keep-me",
    }


class ParserWithHeaderSecretConfig:
    engine = "dotsocr"
    engine_config = {
        "headers": {
            "Authorization": "Bearer header-secret",
            "x-api-key": "header-api-key",
            "Accept": "application/json",
        }
    }


def test_status_sidecar_redacts_api_keys_from_model_config(tmp_path):
    input_file = tmp_path / "input.pdf"
    input_file.write_bytes(b"%PDF-1.4\n")
    save_dir = tmp_path / "out" / "input"

    write_status_sidecar(
        parser=ParserWithSecretConfig(),
        save_dir=str(save_dir),
        input_path=str(input_file),
        filename="input",
        status="success",
        error=None,
        result=[],
        duration_seconds=1.25,
    )

    payload = json.loads((save_dir / ".ocr_status.json").read_text(encoding="utf-8"))

    assert payload["model_config"]["engine"] == "dotsocr"
    assert payload["model_config"]["engine_config"]["temperature"] == 0
    assert payload["model_config"]["engine_config"]["api_key"] == "[redacted]"
    assert payload["model_config"]["engine_config"]["nested"]["api_key"] == "[redacted]"
    assert payload["model_config"]["engine_config"]["nested"]["api_key_env_var"] == "DOTS_API_KEY"
    assert "secret-value" not in json.dumps(payload)
    assert "nested-secret" not in json.dumps(payload)


def test_status_sidecar_redacts_token_secret_and_password_suffixes(tmp_path):
    input_file = tmp_path / "input.pdf"
    input_file.write_bytes(b"%PDF-1.4\n")
    save_dir = tmp_path / "out" / "input"

    write_status_sidecar(
        parser=ParserWithSuffixSecretConfig(),
        save_dir=str(save_dir),
        input_path=str(input_file),
        filename="input",
        status="success",
        error=None,
        result=[],
        duration_seconds=1.25,
    )

    payload = json.loads((save_dir / ".ocr_status.json").read_text(encoding="utf-8"))
    engine_config = payload["model_config"]["engine_config"]

    assert engine_config["bearer_token"] == "[redacted]"
    assert engine_config["client_secret"] == "[redacted]"
    assert engine_config["db_password"] == "[redacted]"
    assert engine_config["safe_tokenizer"] == "keep-me"
    rendered = json.dumps(payload)
    assert "token-secret" not in rendered
    assert "client-secret" not in rendered
    assert "password-secret" not in rendered


def test_status_sidecar_redacts_auth_headers_from_model_config(tmp_path):
    input_file = tmp_path / "input.pdf"
    input_file.write_bytes(b"%PDF-1.4\n")
    save_dir = tmp_path / "out" / "input"

    write_status_sidecar(
        parser=ParserWithHeaderSecretConfig(),
        save_dir=str(save_dir),
        input_path=str(input_file),
        filename="input",
        status="success",
        error=None,
        result=[],
        duration_seconds=1.25,
    )

    payload = json.loads((save_dir / ".ocr_status.json").read_text(encoding="utf-8"))
    headers = payload["model_config"]["engine_config"]["headers"]

    assert headers["Authorization"] == "[redacted]"
    assert headers["x-api-key"] == "[redacted]"
    assert headers["Accept"] == "application/json"
    rendered = json.dumps(payload)
    assert "header-secret" not in rendered
    assert "header-api-key" not in rendered


def test_status_sidecar_records_page_status_summary(tmp_path):
    input_file = tmp_path / "input.pdf"
    input_file.write_bytes(b"%PDF-1.4\n")
    save_dir = tmp_path / "out" / "input"

    write_status_sidecar(
        parser=ParserWithSecretConfig(),
        save_dir=str(save_dir),
        input_path=str(input_file),
        filename="input",
        status="success",
        error=None,
        result=[
            {"page_no": 1, "status": "success"},
            {"page_no": 2, "status": "success_fallback_image"},
            {"page_no": 3, "status": "skipped_blank"},
            {"page_no": 4, "status": "error", "error_type": "TimeoutError"},
        ],
        duration_seconds=1.25,
    )

    payload = json.loads((save_dir / ".ocr_status.json").read_text(encoding="utf-8"))

    assert payload["pages"] == 4
    assert payload["total_pages"] == 4
    assert payload["completed_pages"] == 2
    assert payload["failed_pages"] == 1
    assert payload["skipped_pages"] == 1
    assert payload["page_status_counts"] == {
        "error": 1,
        "skipped_blank": 1,
        "success": 1,
        "success_fallback_image": 1,
    }


def test_status_sidecar_records_manifest_relative_path(tmp_path):
    input_file = tmp_path / "input" / "source.pdf"
    input_file.parent.mkdir()
    input_file.write_bytes(b"%PDF-1.4\n")
    save_dir = tmp_path / "out" / "nested" / "canonical"

    write_status_sidecar(
        parser=ParserWithSecretConfig(),
        save_dir=str(save_dir),
        input_path=str(input_file),
        filename="canonical",
        status="success",
        error=None,
        result=[],
        duration_seconds=1.25,
        manifest_relative_path="nested/canonical.pdf",
    )

    payload = json.loads((save_dir / ".ocr_status.json").read_text(encoding="utf-8"))

    assert payload["manifest_relative_path"] == "nested/canonical.pdf"


def test_status_sidecar_records_execution_trace_on_document_and_artifact(tmp_path):
    input_file = tmp_path / "input.pdf"
    input_file.write_bytes(b"%PDF-1.4\n")
    artifact = tmp_path / "out" / "input" / "native" / "page.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("ok", encoding="utf-8")

    result = [
        {
            "page_no": 1,
            "status": "success_fallback_text",
            "stages": [
                {"stage": "layout", "status": "failed", "failure_category": "model_unreachable"},
                {"stage": "single_stage_ocr", "status": "success"},
            ],
            "fallback": {
                "used": True,
                "reason": "layout_unavailable",
                "source_stage": "layout",
            },
            "native_artifacts": [
                {"kind": "markdown", "path": str(artifact), "engine": "paddleocr-vl"}
            ],
        }
    ]

    write_status_sidecar(
        parser=ParserWithSecretConfig(),
        save_dir=str(tmp_path / "out" / "input"),
        input_path=str(input_file),
        filename="input",
        status="success",
        error=None,
        result=result,
        duration_seconds=1.25,
    )

    payload = json.loads((tmp_path / "out" / "input" / ".ocr_status.json").read_text())
    assert payload["fallback"] == {
        "used": True,
        "reason": "layout_unavailable",
        "source_stage": "layout",
    }
    assert payload["stages"][0]["page_no"] == 1
    assert payload["artifacts"][0]["fallback"] == payload["fallback"]
