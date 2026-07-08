import asyncio

from ocr_parser.output.rendering import _generate_md_for_one_page


class RenderingStub:
    generate_origin_md = False
    keep_page_header = False
    keep_page_footer = False
    skip_footnote = False
    normalize_superscript = False
    filter_author_blocks = False

    def __init__(self):
        self.md_gen_semaphore = asyncio.Semaphore(1)

    def _process_base64_images_with_custom_naming(
        self,
        md_content,
        images_dir,
        page_num,
        filtered_cells_for_images,
        origin_image,
        *,
        image_b64_to_filename,
    ):
        return {"md_content": md_content}

    def _console_write(self, message, level="info"):
        pass


def test_generate_md_for_fallback_image_without_cells(tmp_path):
    page_md = asyncio.run(
        _generate_md_for_one_page(
            RenderingStub(),
            0,
            [
                {
                    "status": "success_fallback_image",
                    "md_content": "![page](images/page_1_bad.jpg)",
                }
            ],
            str(tmp_path),
        )
    )

    assert page_md == {
        "page_num": 1,
        "md_content": "![page](images/page_1_bad.jpg)",
    }
