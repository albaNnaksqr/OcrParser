import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_parser.cli import _apply_profile_defaults, build_parser
from ocr_parser.config import DEFAULT_MAX_COMPLETION_TOKENS, DEFAULT_MODEL_DIR, ParserConfig


def test_parser_accepts_engine_flags():
    args = build_parser().parse_args(
        [
            "--input_file",
            "sample.pdf",
            "--engine",
            "mineru",
            "--engine_config",
            "engines.yaml",
            "--model_dir",
            DEFAULT_MODEL_DIR,
            "--gpu_memory_limit_gb",
            "60",
        ]
    )
    assert args.engine == "mineru"
    assert args.engine_config == "engines.yaml"
    assert args.model_dir == DEFAULT_MODEL_DIR
    assert args.gpu_memory_limit_gb == 60.0


def test_parser_profile_local_applies_conservative_defaults():
    args = build_parser().parse_args(["--input_file", "sample.pdf", "--profile", "local"])
    parser_kwargs = vars(args).copy()

    _apply_profile_defaults(args, parser_kwargs)

    assert parser_kwargs["page_concurrency"] == 4
    assert parser_kwargs["api_concurrency_start"] == 4
    assert parser_kwargs["api_concurrency_max"] == 4
    assert parser_kwargs["num_cpu_workers"] == 8


def test_parser_profile_keeps_explicit_cli_values():
    args = build_parser().parse_args(
        ["--input_file", "sample.pdf", "--profile", "local", "--page_concurrency", "9"]
    )
    parser_kwargs = vars(args).copy()

    _apply_profile_defaults(args, parser_kwargs)

    assert parser_kwargs["page_concurrency"] == 9
    assert parser_kwargs["api_concurrency_start"] == 4


def test_parser_uses_safe_default_completion_token_budget():
    args = build_parser().parse_args(["--input_file", "sample.pdf"])

    assert DEFAULT_MAX_COMPLETION_TOKENS == 4096
    assert ParserConfig().max_completion_tokens == DEFAULT_MAX_COMPLETION_TOKENS
    assert args.max_completion_tokens == DEFAULT_MAX_COMPLETION_TOKENS


def test_parser_accepts_flatten_output_flag():
    args = build_parser().parse_args(["--input_dir", "pdfs", "--flatten_output"])

    assert args.flatten_output is True


def test_parser_accepts_resource_lane_flags():
    args = build_parser().parse_args(
        [
            "--input_file",
            "sample.pdf",
            "--api_concurrency",
            "8",
            "--api_concurrency_start",
            "4",
            "--api_concurrency_max",
            "16",
            "--enable_api_autotune",
            "--api_autotune_interval",
            "3",
            "--render_concurrency",
            "4",
            "--encode_concurrency",
            "3",
            "--postprocess_concurrency",
            "2",
        ]
    )

    assert args.api_concurrency == 8
    assert args.api_concurrency_start == 4
    assert args.api_concurrency_max == 16
    assert args.enable_api_autotune is True
    assert args.api_autotune_interval == 3
    assert args.render_concurrency == 4
    assert args.encode_concurrency == 3
    assert args.postprocess_concurrency == 2


def test_parser_accepts_file_concurrency_flag():
    args = build_parser().parse_args(
        [
            "--input_dir",
            "pdfs",
            "--file_concurrency",
            "3",
        ]
    )

    assert args.file_concurrency == 3


def test_parser_accepts_mineru_stage_scheduler_flags():
    args = build_parser().parse_args(
        [
            "--input_file",
            "sample.pdf",
            "--engine",
            "mineru",
            "--mineru_layout_reserved_api_slots",
            "2",
            "--mineru_recognition_api_concurrency",
            "6",
            "--mineru_min_block_area_ratio",
            "0.001",
            "--mineru_max_blocks_per_page",
            "80",
            "--mineru_skip_visual_block_recognition",
        ]
    )

    assert args.mineru_layout_reserved_api_slots == 2
    assert args.mineru_recognition_api_concurrency == 6
    assert args.mineru_min_block_area_ratio == 0.001
    assert args.mineru_max_blocks_per_page == 80
    assert args.mineru_skip_visual_block_recognition is True


def test_parser_accepts_paddleocr_vl_tuning_flags():
    args = build_parser().parse_args(
        [
            "--input_file",
            "sample.pdf",
            "--engine",
            "paddleocr-vl",
            "--paddle_layout_concurrency",
            "2",
            "--paddle_block_backpressure_high_watermark",
            "128",
            "--paddle_block_backpressure_low_watermark",
            "64",
        ]
    )

    assert args.paddle_layout_concurrency == 2
    assert args.paddle_block_backpressure_high_watermark == 128
    assert args.paddle_block_backpressure_low_watermark == 64


def test_parser_rejects_unknown_engine():
    parser = build_parser()
    try:
        parser.parse_args(["--input_file", "sample.pdf", "--engine", "unknown"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("unknown engine should fail argparse validation")


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--page_concurrency", "0"),
        ("--page_concurrency", "-1"),
        ("--file_concurrency", "0"),
        ("--file_concurrency", "-1"),
        ("--dpi", "0"),
        ("--port", "0"),
        ("--port", "65536"),
    ],
)
def test_parser_rejects_invalid_numeric_bounds(flag, value):
    parser = build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--input_file", "sample.pdf", flag, value])

    assert exc.value.code == 2


def test_parser_rejects_negative_block_concurrency():
    parser = build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--input_file", "sample.pdf", "--block_concurrency", "-1"])

    assert exc.value.code == 2


def test_parser_rejects_negative_paddle_layout_concurrency():
    parser = build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--input_file", "sample.pdf", "--paddle_layout_concurrency", "-1"])

    assert exc.value.code == 2


@pytest.mark.parametrize(
    "flag",
    [
        "--paddle_block_backpressure_high_watermark",
        "--paddle_block_backpressure_low_watermark",
    ],
)
def test_parser_rejects_negative_paddle_backpressure_watermark(flag):
    parser = build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--input_file", "sample.pdf", flag, "-1"])

    assert exc.value.code == 2


def test_parser_rejects_rename_with_input_dir():
    parser = build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--input_dir", "pdfs", "--rename", "renamed"])

    assert exc.value.code == 2


def test_config_preserves_engine_defaults_and_overrides():
    default = ParserConfig.from_kwargs()
    assert default.engine == "dotsocr"
    assert default.model_dir == DEFAULT_MODEL_DIR
    config = ParserConfig.from_kwargs(engine="paddleocr-vl", gpu_memory_limit_gb=60)
    assert config.engine == "paddleocr-vl"
    assert config.gpu_memory_limit_gb == 60


def test_config_preserves_resource_lane_overrides():
    config = ParserConfig.from_kwargs(
        api_concurrency=8,
        api_concurrency_start=4,
        api_concurrency_max=16,
        enable_api_autotune=True,
        api_autotune_interval=3,
        render_concurrency=4,
        encode_concurrency=3,
        postprocess_concurrency=2,
    )

    assert config.api_concurrency == 8
    assert config.api_concurrency_start == 4
    assert config.api_concurrency_max == 16
    assert config.enable_api_autotune is True
    assert config.api_autotune_interval == 3
    assert config.render_concurrency == 4
    assert config.encode_concurrency == 3
    assert config.postprocess_concurrency == 2


def test_config_preserves_file_concurrency_override():
    config = ParserConfig.from_kwargs(file_concurrency=3)

    assert config.file_concurrency == 3


def test_config_preserves_mineru_stage_scheduler_overrides():
    config = ParserConfig.from_kwargs(
        mineru_layout_reserved_api_slots=2,
        mineru_recognition_api_concurrency=6,
        mineru_min_block_area_ratio=0.001,
        mineru_max_blocks_per_page=80,
        mineru_skip_visual_block_recognition=True,
    )

    assert config.mineru_layout_reserved_api_slots == 2
    assert config.mineru_recognition_api_concurrency == 6
    assert config.mineru_min_block_area_ratio == 0.001
    assert config.mineru_max_blocks_per_page == 80
    assert config.mineru_skip_visual_block_recognition is True


def test_config_preserves_paddleocr_vl_tuning_overrides():
    config = ParserConfig.from_kwargs(
        paddle_layout_concurrency=2,
        paddle_block_backpressure_high_watermark=128,
        paddle_block_backpressure_low_watermark=64,
    )

    assert config.paddle_layout_concurrency == 2
    assert config.paddle_block_backpressure_high_watermark == 128
    assert config.paddle_block_backpressure_low_watermark == 64


def test_parser_config_validates_known_option_dict():
    normalized = ParserConfig.validate_option_dict(
        {
            "file_concurrency": "4",
            "api_concurrency_start": "8",
            "enable_api_autotune": "true",
            "mineru_min_block_area_ratio": "0.001",
            "api_key_env_var": "OCR_API_KEY",
        },
        context="test options",
    )

    assert normalized["file_concurrency"] == 4
    assert normalized["api_concurrency_start"] == 8
    assert normalized["enable_api_autotune"] is True
    assert normalized["mineru_min_block_area_ratio"] == 0.001
    assert normalized["api_key_env_var"] == "OCR_API_KEY"


def test_parser_config_rejects_unknown_option_dict_key():
    with pytest.raises(ValueError, match="unknown test options key"):
        ParserConfig.validate_option_dict({"not_a_parser_option": True}, context="test options")


def test_parser_config_rejects_invalid_option_dict_value():
    with pytest.raises(ValueError, match="file_concurrency"):
        ParserConfig.validate_option_dict({"file_concurrency": "many"}, context="test options")
