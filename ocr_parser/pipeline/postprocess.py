from __future__ import annotations

from typing import Any, Dict, List


def run_all_post_processing_worker(parser_init_kwargs: Dict[str, Any], layout_data: List[dict]) -> List[dict]:
    from ..parser import DotsOCRParser

    worker_kwargs = dict(parser_init_kwargs)
    worker_kwargs["init_process_pool"] = False
    worker_kwargs["init_md_semaphore"] = False
    parser = DotsOCRParser(**worker_kwargs)

    has_original_cells = False
    for result in layout_data:
        if "cells" in result and result.get("cells"):
            result["cells"] = parser._perform_intra_page_matching(result["cells"])
        if result.get("original_cells"):
            result["original_cells"] = parser._perform_intra_page_matching(result["original_cells"])
            has_original_cells = True

    parser._perform_cross_page_table_caption_matching(layout_data)
    parser._perform_cross_page_text_merging(layout_data)

    if has_original_cells:
        saved_cells = []
        for result in layout_data:
            saved_cells.append(result.get("cells"))
            if result.get("original_cells") is not None:
                result["cells"] = result["original_cells"]
            else:
                result["cells"] = []

        parser._perform_cross_page_table_caption_matching(layout_data)
        parser._perform_cross_page_text_merging(layout_data)

        for result, filtered_cells in zip(layout_data, saved_cells):
            if result.get("original_cells") is not None:
                result["original_cells"] = result.get("cells")
            result["cells"] = filtered_cells

    return layout_data
