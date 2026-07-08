import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_parser import DotsOCRParser


@pytest.mark.parametrize(
    ("kwargs", "field_name"),
    [
        ({"page_concurrency": 0}, "page_concurrency"),
        ({"page_concurrency": -1}, "page_concurrency"),
        ({"dpi": 0}, "dpi"),
        ({"port": 65536}, "port"),
        ({"block_concurrency": -1}, "block_concurrency"),
    ],
)
def test_parser_rejects_invalid_sdk_numeric_config(kwargs, field_name):
    with pytest.raises(ValueError, match=field_name):
        DotsOCRParser(
            **kwargs,
            init_process_pool=False,
            init_md_semaphore=False,
        )


def test_parser_exposes_paddleocr_vl_engine_settings():
    parser = DotsOCRParser(
        engine="paddleocr-vl",
        layout_detection_url="http://layout.local:30002",
        block_concurrency=3,
        init_process_pool=False,
        init_md_semaphore=False,
    )

    assert parser.layout_detection_url == "http://layout.local:30002"
    assert parser.block_concurrency == 3
    assert parser.ocr_engine._layout_url == "http://layout.local:30002"
    assert parser.ocr_engine._block_concurrency == 3


def test_parser_exposes_resource_lane_settings():
    parser = DotsOCRParser(
        api_concurrency=8,
        render_concurrency=4,
        encode_concurrency=3,
        postprocess_concurrency=2,
        init_process_pool=False,
        init_md_semaphore=False,
    )

    assert parser.api_concurrency == 8
    assert parser.render_concurrency == 4
    assert parser.encode_concurrency == 3
    assert parser.postprocess_concurrency == 2
    assert parser.api_limiter.limit == 8
    assert parser.api_semaphore is parser.api_limiter
    assert parser.render_semaphore._value == 4
    assert parser.encode_semaphore._value == 3
    assert parser.postprocess_semaphore._value == 2


def test_parser_uses_api_key_from_environment_when_cli_value_is_none(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret-from-env")

    parser = DotsOCRParser(
        api_key=None,
        init_process_pool=False,
        init_md_semaphore=False,
    )

    assert parser.api_key == "secret-from-env"
