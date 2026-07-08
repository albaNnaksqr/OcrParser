import asyncio
from pathlib import Path

from ocr_parser.output.markdown_writer import write_document_outputs


class ParserStub:
    md_gen_concurrency = 1
    generate_origin_md = False
    filter_duplicates = False
    save_page_layout = False
    add_page_tag = False

    def _get_unique_md_path(self, save_dir, base_filename):
        return str(Path(save_dir) / f"{base_filename}.md")

    async def _generate_md_for_one_page(self, page_idx, all_pages_layout_data, images_dir):
        return {
            "page_num": page_idx + 1,
            "md_content": all_pages_layout_data[page_idx]["md_content"],
        }

    async def _flush_document_page_json(self, save_dir):
        return None

    def _console_write(self, message, level="info"):
        pass


def test_write_document_outputs_streams_fallback_markdown(tmp_path):
    artifacts = asyncio.run(
        write_document_outputs(
            ParserStub(),
            filename="doc",
            save_dir=str(tmp_path),
            all_pages_layout_data=[
                {"status": "success_fallback_text", "md_content": "hello"}
            ],
            total_pages_expected=1,
        )
    )

    assert Path(artifacts.combined_md_path).read_text(encoding="utf-8") == "hello\n\n"
