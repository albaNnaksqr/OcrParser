from pathlib import Path

from ocr_parser.cli import _apply_engine_config, _apply_profile_defaults, _load_engine_config, build_parser


def test_load_engine_config_reads_yaml_defaults_and_engine_section(tmp_path):
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        """
defaults:
  timeout: 123
  file_concurrency: 2
engines:
  mineru:
    ip: 10.0.0.2
    port: 30000
    model_name: MinerU2.5
""".strip(),
        encoding="utf-8",
    )

    config = _load_engine_config(config_path, "mineru")

    assert config == {
        "timeout": 123,
        "file_concurrency": 2,
        "ip": "10.0.0.2",
        "port": 30000,
        "model_name": "MinerU2.5",
    }


def test_load_engine_config_normalizes_known_value_types(tmp_path):
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        """
engines:
  dotsocr:
    file_concurrency: "2"
    enable_api_autotune: "true"
    retry_delay: "1.5"
""".strip(),
        encoding="utf-8",
    )

    config = _load_engine_config(config_path, "dotsocr")

    assert config["file_concurrency"] == 2
    assert config["enable_api_autotune"] is True
    assert config["retry_delay"] == 1.5


def test_apply_engine_config_keeps_explicit_cli_values(tmp_path):
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        """
engines:
  mineru:
    ip: 10.0.0.2
    port: 30000
""".strip(),
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "--input_file",
            "sample.pdf",
            "--engine",
            "mineru",
            "--engine_config",
            str(config_path),
            "--ip",
            "cli-host",
        ]
    )
    parser_kwargs = vars(args).copy()

    _apply_engine_config(args, parser_kwargs)

    assert parser_kwargs["ip"] == "cli-host"
    assert parser_kwargs["port"] == 30000


def test_engine_config_overrides_profile_but_not_explicit_cli_values(tmp_path):
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        """
engines:
  dotsocr:
    page_concurrency: 12
    api_concurrency_start: 10
    api_concurrency_max: 10
""".strip(),
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "--input_file",
            "sample.pdf",
            "--profile",
            "local",
            "--engine_config",
            str(config_path),
            "--api_concurrency_max",
            "6",
        ]
    )
    parser_kwargs = vars(args).copy()

    _apply_profile_defaults(args, parser_kwargs)
    _apply_engine_config(args, parser_kwargs)

    assert parser_kwargs["page_concurrency"] == 12
    assert parser_kwargs["api_concurrency_start"] == 10
    assert parser_kwargs["api_concurrency_max"] == 6


def test_load_engine_config_rejects_unknown_keys(tmp_path):
    config_path = tmp_path / "engines.json"
    config_path.write_text('{"engines": {"mineru": {"unknown": true}}}', encoding="utf-8")

    try:
        _load_engine_config(Path(config_path), "mineru")
    except ValueError as exc:
        assert "unknown engine_config key" in str(exc)
    else:
        raise AssertionError("unknown engine_config keys should fail")
