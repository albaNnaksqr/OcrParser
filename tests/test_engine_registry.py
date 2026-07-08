import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_parser.engines.registry import create_engine


class ParserStub:
    engine = "dotsocr"


def test_create_known_engines():
    parser = ParserStub()
    assert create_engine(parser, "dotsocr").name == "dotsocr"
    assert create_engine(parser, "mineru").name == "mineru"
    assert create_engine(parser, "paddleocr-vl").name == "paddleocr-vl"


def test_create_unknown_engine_fails():
    parser = ParserStub()
    try:
        create_engine(parser, "other")
    except ValueError as exc:
        assert "Unsupported OCR engine" in str(exc)
    else:
        raise AssertionError("unsupported engine should raise ValueError")
