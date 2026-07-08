import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_parser.output.native_writer import (
    NativeArtifact,
    native_engine_dir,
    write_native_json,
    write_native_text,
)


def test_native_engine_dir_is_stable(tmp_path):
    path = native_engine_dir(str(tmp_path), "paddleocr-vl")
    assert path == tmp_path / "native" / "paddleocr-vl"
    assert path.exists()


def test_write_native_json_and_text(tmp_path):
    json_artifact = write_native_json(str(tmp_path), "mineru", "page_0001_raw.json", {"ok": True})
    text_artifact = write_native_text(str(tmp_path), "mineru", "page_0001.md", "# Page")
    assert isinstance(json_artifact, NativeArtifact)
    assert json.loads(Path(json_artifact.path).read_text()) == {"ok": True}
    assert Path(text_artifact.path).read_text() == "# Page"
