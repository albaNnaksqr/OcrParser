import sys
import types
from pathlib import Path

from tools import run_performance_baseline as baseline


class _FakeFitzDocument:
    page_count = 3

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_parse_variant_spec_supports_entrypoint():
    variant = baseline.parse_variant_spec("v20=/workspace/dotsocr:parser_async_v20.py")

    assert variant.name == "v20"
    assert variant.cwd == Path("/workspace/dotsocr")
    assert variant.entrypoint == "parser_async_v20.py"
    assert variant.kind == "legacy"


def test_build_command_uses_modular_engine_flags(tmp_path):
    variant = baseline.Variant(
        name="current",
        cwd=Path("/repo/current"),
        entrypoint="ocr_parser_cli.py",
        kind="modular",
    )

    command = baseline.build_command(
        variant=variant,
        python=Path("/venv/bin/python"),
        pdf=Path("/data/a.pdf"),
        output_dir=tmp_path / "out",
        engine="paddleocr-vl",
        engine_config=Path("/repo/engines.yaml"),
        ip="127.0.0.1",
        port=8000,
        model_name="model",
        page_concurrency=4,
        num_cpu_workers=2,
        md_gen_concurrency=1,
        timeout=60.0,
        max_retries=2,
        retry_delay=0.1,
        max_completion_tokens=4096,
        save_page_json=True,
        skip_blank_pages=True,
    )

    assert command[:2] == ["/venv/bin/python", "ocr_parser_cli.py"]
    assert "--engine" in command
    assert "paddleocr-vl" in command
    assert "--engine_config" in command
    assert "--skip_blank_pages" in command
    assert "--max_completion_tokens" in command
    assert "4096" in command
    assert "--save_page_json" in command


def test_build_directory_command_uses_file_concurrency(tmp_path):
    variant = baseline.Variant(
        name="current",
        cwd=Path("/repo/current"),
        entrypoint="ocr_parser_cli.py",
        kind="modular",
    )

    command = baseline.build_directory_command(
        variant=variant,
        python=Path("/venv/bin/python"),
        input_dir=Path("/data/pdfs"),
        output_dir=tmp_path / "out",
        engine="dotsocr",
        engine_config=None,
        ip="127.0.0.1",
        port=8000,
        model_name="model",
        page_concurrency=80,
        file_concurrency=3,
        num_cpu_workers=56,
        md_gen_concurrency=56,
        timeout=180.0,
        max_retries=1,
        retry_delay=0.2,
        max_completion_tokens=4096,
        save_page_json=True,
        skip_blank_pages=True,
    )

    assert "--input_dir" in command
    assert "/data/pdfs" in command
    assert "--input_file" not in command
    assert "--file_concurrency" in command
    assert "3" in command
    assert "--flatten_output" not in command


def test_build_command_can_inject_api_key_from_environment_without_secret(tmp_path):
    variant = baseline.Variant(
        name="v20",
        cwd=Path("/repo/v20"),
        entrypoint="parser_async_v20.py",
        kind="legacy",
    )

    command = baseline.build_command(
        variant=variant,
        python=Path("/venv/bin/python"),
        pdf=Path("/data/a.pdf"),
        output_dir=tmp_path / "out",
        engine="dotsocr",
        engine_config=None,
        ip="127.0.0.1",
        port=8000,
        model_name="DotsOCR",
        page_concurrency=8,
        num_cpu_workers=4,
        md_gen_concurrency=2,
        timeout=600.0,
        max_retries=1,
        retry_delay=0.0,
        max_completion_tokens=4096,
        save_page_json=False,
        skip_blank_pages=False,
        api_key_from_env=True,
    )

    assert command[:3] == ["/venv/bin/python", "-c", baseline.API_KEY_ARGV_WRAPPER]
    assert command[3] == "parser_async_v20.py"
    assert "secret" not in " ".join(command)


def test_build_command_omits_modular_only_flags_for_legacy_v20(tmp_path):
    variant = baseline.Variant(
        name="v20",
        cwd=Path("/repo/v20"),
        entrypoint="parser_async_v20.py",
        kind="legacy",
    )

    command = baseline.build_command(
        variant=variant,
        python=Path("/venv/bin/python"),
        pdf=Path("/data/a.pdf"),
        output_dir=tmp_path / "out",
        engine="dotsocr",
        engine_config=Path("/repo/engines.yaml"),
        ip="127.0.0.1",
        port=8000,
        model_name="DotsOCR",
        page_concurrency=8,
        num_cpu_workers=4,
        md_gen_concurrency=2,
        timeout=600.0,
        max_retries=1,
        retry_delay=0.0,
        max_completion_tokens=4096,
        save_page_json=False,
        skip_blank_pages=False,
    )

    assert "--engine" not in command
    assert "--engine_config" not in command
    assert "--input_file" in command
    assert "--disable_resume" in command
    assert "--force_reprocess" in command


def test_extract_log_metrics_parses_runtime_summary():
    text = """
    Total pages processed (incl. blank): 42
    Total processing time: 21.50s
    Total inference requests: 40
    Average inference time: 0.42s
    Overall throughput: 1.95 pages/sec
    [AUTOTUNE] api_concurrency 64 -> 80
    Concurrent retry triggered (4 lanes)
    """

    metrics = baseline.extract_log_metrics(text)

    assert metrics["log_total_pages"] == "42"
    assert metrics["log_total_time_s"] == "21.500"
    assert metrics["log_inference_requests"] == "40"
    assert metrics["log_avg_inference_s"] == "0.420"
    assert metrics["log_throughput_pages_per_sec"] == "1.950"
    assert metrics["autotune_events"] == "1"
    assert metrics["concurrent_retry_events"] == "1"


def test_load_local_config_supports_env_style_file(tmp_path):
    config_path = tmp_path / "dotsocr.env"
    config_path.write_text(
        """
        # local secret; do not print
        DOTSOCR_API_KEY="secret value"
        DOTSOCR_IP=127.0.0.1
        DOTSOCR_PORT=30080
        """,
        encoding="utf-8",
    )

    config = baseline.load_local_config(config_path)

    assert config["DOTSOCR_API_KEY"] == "secret value"
    assert config["DOTSOCR_IP"] == "127.0.0.1"
    assert config["DOTSOCR_PORT"] == "30080"


def test_apply_local_config_updates_benchmark_defaults():
    args = baseline.parse_args(
        [
            "--variant",
            "current=/repo/current",
        ]
    )

    baseline.apply_local_config(
        args,
        {
            "DOTSOCR_API_KEY": "secret",
            "DOTSOCR_IP": "127.0.0.1",
            "DOTSOCR_PORT": "30080",
            "DOTSOCR_MODEL_NAME": "DotsOCR",
            "BENCHMARK_PAGE_CONCURRENCY": "12",
            "BENCHMARK_FILE_CONCURRENCY": "3",
            "BENCHMARK_MAX_COMPLETION_TOKENS": "4096",
            "BENCHMARK_SAVE_PAGE_JSON": "true",
            "BENCHMARK_SKIP_BLANK_PAGES": "true",
        },
    )

    assert args.api_key == "secret"
    assert args.ip == "127.0.0.1"
    assert args.port == 30080
    assert args.model_name == "DotsOCR"
    assert args.page_concurrency == 12
    assert args.file_concurrency == 3
    assert args.max_completion_tokens == 4096
    assert args.save_page_json is True
    assert args.skip_blank_pages is True


def test_load_local_configs_overlays_later_files(tmp_path):
    secret_path = tmp_path / "secret.env"
    secret_path.write_text(
        """
        DOTSOCR_API_KEY=secret
        DOTSOCR_IP=old-host
        DOTSOCR_PORT=30080
        """,
        encoding="utf-8",
    )
    profile_path = tmp_path / "profile.env"
    profile_path.write_text(
        """
        DOTSOCR_IP=127.0.0.1
        DOTSOCR_PORT=13080
        BENCHMARK_PAGE_CONCURRENCY=80
        """,
        encoding="utf-8",
    )

    config = baseline.load_local_configs([secret_path, profile_path])

    assert config["DOTSOCR_API_KEY"] == "secret"
    assert config["DOTSOCR_IP"] == "127.0.0.1"
    assert config["DOTSOCR_PORT"] == "13080"
    assert config["BENCHMARK_PAGE_CONCURRENCY"] == "80"


def test_resolve_pdf_meta_counts_pages_without_manifest(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    fake_fitz = types.SimpleNamespace(open=lambda _path: _FakeFitzDocument())
    monkeypatch.setitem(sys.modules, "fitz", fake_fitz)

    meta = baseline.resolve_pdf_meta(pdf_path, {})

    assert meta["pages"] == 3
    assert meta["category"] == ""


def test_find_markdown_output_prefers_non_empty_files(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    empty = output_dir / "empty.md"
    empty.write_text("", encoding="utf-8")
    nested = output_dir / "doc" / "doc.md"
    nested.parent.mkdir()
    nested.write_text("content", encoding="utf-8")

    assert baseline.find_markdown_output(output_dir, "doc") == nested


def test_find_document_markdown_outputs_ignores_origin_markdown(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    doc = output_dir / "doc" / "doc.md"
    origin = output_dir / "doc" / "doc_origin.md"
    doc.parent.mkdir()
    doc.write_text("content", encoding="utf-8")
    origin.write_text("origin content", encoding="utf-8")

    assert baseline.find_document_markdown_outputs(output_dir) == [doc]


def test_classify_run_status_rejects_auth_failure_even_with_markdown(tmp_path):
    md_path = tmp_path / "doc.md"
    md_path.write_text("![page_1_bad.jpg](images/page_1_bad.jpg)", encoding="utf-8")
    log_text = """
    OpenAI API error after 0.40s (attempt 1/1): Error code: 401
    Page 1 failed all attempts. Last known error: AuthenticationError.
    Fallback succeeded. Screenshot for page 1 saved.
    """

    assert baseline.classify_run_status(0, md_path, log_text) == "failed"


def test_validate_args_rejects_invalid_run_mode_from_local_config():
    args = baseline.parse_args(["--variant", "current=/repo/current"])
    baseline.apply_local_config(args, {"BENCHMARK_RUN_MODE": "bad"})

    try:
        baseline.validate_args(args)
    except SystemExit as exc:
        assert "--run-mode must be one of" in str(exc)
    else:
        raise AssertionError("invalid run mode should fail validation")


def test_validate_args_rejects_legacy_variant_in_directory_mode():
    args = baseline.parse_args(
        [
            "--variant",
            "v20=/repo/v20:parser_async_v20.py",
            "--run-mode",
            "directory",
        ]
    )

    try:
        baseline.validate_args(args)
    except SystemExit as exc:
        assert "directory run mode only supports modular variants" in str(exc)
    else:
        raise AssertionError("legacy variant should fail directory validation")
