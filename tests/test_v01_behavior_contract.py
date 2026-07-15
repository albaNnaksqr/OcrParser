from __future__ import annotations

import asyncio
import json
from pathlib import Path

import fitz

from ocr_parser.cli import _collect_input_records, build_parser
from ocr_parser.contracts import EnginePageResult, ManifestItem
from ocr_parser.output.markdown_writer import write_document_outputs
from ocr_platform.control.app import create_app


ROOT = Path(__file__).parents[1]
CONTRACT = json.loads(
    (ROOT / "tests/fixtures/contracts/v01_behavior_contract.json").read_text(encoding="utf-8")
)


class _OutputParser:
    md_gen_concurrency = 1
    generate_origin_md = False
    filter_duplicates = False
    save_page_layout = False
    add_page_tag = False

    def _get_unique_md_path(self, save_dir, base_filename):
        return str(Path(save_dir) / f"{base_filename}.md")

    async def _generate_md_for_one_page(self, page_idx, all_pages_layout_data, images_dir):
        return {"page_num": page_idx + 1, "md_content": all_pages_layout_data[page_idx]["md_content"]}

    async def _flush_document_page_json(self, save_dir):
        return None

    def _console_write(self, message, level="info"):
        return None


def test_cli_defaults_match_v01_contract() -> None:
    args = build_parser().parse_args(["--input_file", "sample.pdf"])
    observed = {key: getattr(args, key) for key in CONTRACT["cli_defaults"]}
    assert observed == CONTRACT["cli_defaults"]


def test_openapi_route_surface_matches_v01_contract() -> None:
    schema = create_app().openapi()
    observed = {
        path: sorted(method for method in operations if method in {"get", "post", "put", "patch", "delete"})
        for path, operations in sorted(schema["paths"].items())
    }
    assert observed == CONTRACT["openapi_paths"]


def test_manifest_and_engine_result_wire_formats_match_v01_contract() -> None:
    item = ManifestItem(
        input_path="/public/input.pdf",
        relative_path="nested/input.pdf",
        size_bytes=123,
        mtime_ns=456,
    )
    assert item.to_json_line() == CONTRACT["manifest_jsonl"]

    result = EnginePageResult(
        page_no=1,
        original_page_num=1,
        status="success",
        cells=[{"category": "Text", "text": "hello"}],
        page_json_path="page_1.json",
    )
    assert result.to_layout_result() == CONTRACT["engine_page_result"]


def test_markdown_output_snapshot_is_stable(tmp_path) -> None:
    artifacts = asyncio.run(
        write_document_outputs(
            _OutputParser(),
            filename="document",
            save_dir=str(tmp_path),
            all_pages_layout_data=[
                {"status": "success", "md_content": "# Title\n\nPage one."},
                {"status": "success_fallback_text", "md_content": "Page two."},
            ],
            total_pages_expected=2,
        )
    )
    assert Path(artifacts.combined_md_path).read_text(encoding="utf-8") == (
        "# Title\n\nPage one.\n\nPage two.\n\n"
    )


def test_public_pdf_golden_fixtures_are_small_and_readable() -> None:
    fixtures = ROOT / "tests/fixtures/public_pdfs"
    expected = {"simple_text_1p.pdf": 1, "receipt_narrow_1p.pdf": 1}
    for filename, pages in expected.items():
        path = fixtures / filename
        assert path.stat().st_size < 32 * 1024
        with fitz.open(path) as document:
            assert document.page_count == pages


def test_single_directory_and_manifest_input_mode_snapshots(tmp_path) -> None:
    root = tmp_path / "input"
    nested = root / "nested"
    nested.mkdir(parents=True)
    first = root / "first.pdf"
    second = nested / "second.pdf"
    first.write_bytes(b"%PDF-1.4\n")
    second.write_bytes(b"%PDF-1.4\n")

    single = _collect_input_records(build_parser().parse_args(["--input_file", str(first)]))
    directory = _collect_input_records(build_parser().parse_args(["--input_dir", str(root)]))

    stat = second.stat()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        ManifestItem(
            input_path=str(second.resolve()),
            relative_path="renamed/document.pdf",
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        ).to_json_line()
        + "\n",
        encoding="utf-8",
    )
    manifest_records = _collect_input_records(
        build_parser().parse_args(["--input_manifest", str(manifest)])
    )

    assert [(item.path.name, item.rel_parent.as_posix(), item.output_stem) for item in single] == [
        ("first.pdf", ".", None)
    ]
    assert [(item.path.name, item.rel_parent.as_posix(), item.output_stem) for item in directory] == [
        ("first.pdf", ".", None),
        ("second.pdf", "nested", None),
    ]
    assert [
        (item.path.name, item.rel_parent.as_posix(), item.output_stem)
        for item in manifest_records
    ] == [("second.pdf", "renamed", "document")]
