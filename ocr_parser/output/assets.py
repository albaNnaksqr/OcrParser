from __future__ import annotations

import base64
import contextlib
import os
import re
from io import BytesIO
from typing import Dict, Optional, Tuple

from PIL import Image

from ..domain.algorithms import extract_table_screenshot, generate_image_name


def _process_base64_images_with_custom_naming(
    self,
    md_content,
    images_dir,
    page_num,
    cells,
    origin_image=None,
    image_b64_to_filename=None,
):
    os.makedirs(images_dir, exist_ok=True)
    base64_pattern = r"!\[[^\]]*\]\s*\(\s*data:image/[^;]+;base64,([^)]+)\)"

    if image_b64_to_filename is None:
        image_b64_to_filename = {}

    table_cache: Dict[Tuple, Tuple[str, Optional[str]]] = image_b64_to_filename.setdefault("__TABLE_SCREENSHOT_CACHE__", {})

    def _build_table_cache_key(table_cell: dict, snippet: Optional[str], table_idx: int) -> tuple:
        bbox = tuple(int(round(float(coord))) for coord in (table_cell.get("bbox") or [])[:4])
        caption = (table_cell.get("caption_text") or "").strip()
        snippet_signature = (snippet or table_cell.get("html") or table_cell.get("text") or "").strip()[:500]
        return (page_num, table_idx, bbox, caption, snippet_signature)

    def _locate_snippet_in_md(target: Optional[str], *, content: Optional[str] = None):
        if not target:
            return None
        haystack = md_content if content is None else content
        idx = haystack.find(target)
        if idx != -1:
            end_idx = idx + len(target)
            return idx, end_idx, haystack[idx:end_idx]
        stripped = target.strip()
        if not stripped:
            return None
        tokens = [tok for tok in re.split(r"\s+", stripped) if tok]
        if not tokens:
            return None
        pattern = r"\s+".join(re.escape(tok) for tok in tokens)
        try:
            match = re.search(pattern, haystack, flags=re.DOTALL)
        except re.error:
            return None
        if match:
            start_idx, end_idx = match.start(), match.end()
            return start_idx, end_idx, haystack[start_idx:end_idx]
        return None

    pictures = [cell for cell in cells if cell["category"] == "Picture"]
    picture_counter = 0

    def replace_base64(match):
        nonlocal picture_counter
        try:
            base64_data = match.group(1)
            if base64_data in image_b64_to_filename:
                image_filename_with_ext, image_filename_base = image_b64_to_filename[base64_data]
                if image_filename_with_ext is None:
                    picture_counter += 1
                    return ""
                md_image_path = os.path.join("images", image_filename_with_ext).replace(os.sep, "/")
                picture_counter += 1
                return f"![{image_filename_base}]({md_image_path})"

            image_data = base64.b64decode(base64_data)
            image = Image.open(BytesIO(image_data))
            if self.filter_qr_barcodes and self._is_qr_or_barcode(image):
                picture_counter += 1
                image_b64_to_filename[base64_data] = (None, None)
                return ""

            current_pic_cell = pictures[picture_counter] if picture_counter < len(pictures) else None
            if self.skip_uncaptioned_images and (not current_pic_cell or not current_pic_cell.get("caption_text")):
                picture_counter += 1
                image_b64_to_filename[base64_data] = (None, None)
                return ""

            image_filename_base = generate_image_name(current_pic_cell, picture_counter, page_num)
            counter = 0
            image_filename = image_filename_base
            while os.path.exists(os.path.join(images_dir, f"{image_filename}.jpg")):
                counter += 1
                image_filename = f"{image_filename_base}_{counter}"

            image_filename_with_ext = f"{image_filename}.jpg"
            image_path = os.path.join(images_dir, image_filename_with_ext)
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGB")
            image.save(image_path, "JPEG", quality=95)
            image_b64_to_filename[base64_data] = (image_filename_with_ext, image_filename_base)
            picture_counter += 1
            md_image_path = os.path.join("images", image_filename_with_ext).replace(os.sep, "/")
            return f"![{image_filename_base}]({md_image_path})"
        except Exception as exc:
            self._console_write(f"Error processing base64 image: {exc}", level="error")
            return match.group(0)

    md_content = re.sub(base64_pattern, replace_base64, md_content)

    if self.enable_table_screenshot and origin_image and cells:
        table_cells = [cell for cell in cells if cell.get("category") == "Table"]
        cursor_in_original = 0
        insertions = []  # list of (original_pos, text) collected for single-pass assembly
        for table_index, table_cell in enumerate(table_cells):
            table_html_candidates = []
            html_field = table_cell.get("html")
            text_field = table_cell.get("text")
            if isinstance(html_field, str) and html_field.strip():
                table_html_candidates.extend([html_field, html_field.strip()])
            if isinstance(text_field, str) and text_field.strip():
                table_html_candidates.extend([text_field, text_field.strip()])

            seen_candidates = set()
            normalized_candidates = []
            for candidate in table_html_candidates:
                if candidate and candidate not in seen_candidates:
                    seen_candidates.add(candidate)
                    normalized_candidates.append(candidate)

            snippet_bounds = None
            search_content = md_content[cursor_in_original:]
            for candidate in normalized_candidates:
                match_bounds = _locate_snippet_in_md(candidate, content=search_content)
                if match_bounds:
                    rel_start, rel_end, snippet_fragment = match_bounds
                    snippet_bounds = (cursor_in_original + rel_start, cursor_in_original + rel_end, snippet_fragment)
                    break

            fallback_strategy = None
            if not snippet_bounds:
                lower_search = search_content.lower()
                table_tag_start = lower_search.find("<table")
                if table_tag_start != -1:
                    table_tag_end = lower_search.find("</table>", table_tag_start)
                    table_tag_end = table_tag_end + len("</table>") if table_tag_end != -1 else table_tag_start
                    snippet_bounds = (
                        cursor_in_original + table_tag_end,
                        cursor_in_original + table_tag_end,
                        search_content[table_tag_start:table_tag_end] if table_tag_end > table_tag_start else "",
                    )
                    fallback_strategy = "html-tag"

            if not snippet_bounds:
                caption_text = (table_cell.get("caption_text") or "").strip()
                if caption_text:
                    caption_variants = [caption_text]
                    plain_caption = re.sub(r"\*+", "", caption_text).strip()
                    if plain_caption and plain_caption not in caption_variants:
                        caption_variants.append(plain_caption)
                    normalized_caption = re.sub(r"\s+", " ", plain_caption or caption_text).strip()
                    if normalized_caption and normalized_caption not in caption_variants:
                        caption_variants.append(normalized_caption)
                    for cap_variant in caption_variants:
                        cap_bounds = _locate_snippet_in_md(cap_variant, content=search_content)
                        if cap_bounds:
                            _, cap_end_rel, snippet_fragment = cap_bounds
                            insert_pos = cursor_in_original + cap_end_rel
                            snippet_bounds = (insert_pos, insert_pos, snippet_fragment)
                            fallback_strategy = "caption"
                            break

            if not snippet_bounds:
                insertion_point = len(md_content)
                snippet_bounds = (insertion_point, insertion_point, "")
                fallback_strategy = "append-end"

            if fallback_strategy == "html-tag":
                self._console_write(
                    f"Warning on page {page_num}: table #{table_index + 1} HTML signature not matched exactly; using nearest <table> block for screenshot placement.",
                    level="warning",
                )
            elif fallback_strategy == "caption":
                self._console_write(
                    f"Warning on page {page_num}: table #{table_index + 1} HTML snippet missing; placing screenshot after caption text.",
                    level="warning",
                )
            elif fallback_strategy == "append-end":
                self._console_write(
                    f"Warning on page {page_num}: table #{table_index + 1} could not be aligned with markdown content; appending screenshot at document end.",
                    level="warning",
                )

            _, snippet_end, html_snippet_in_md = snippet_bounds
            table_key = _build_table_cache_key(table_cell, html_snippet_in_md, table_index)
            table_filename_with_ext = None
            table_filename_base = None
            cached_entry = table_cache.get(table_key)
            if cached_entry:
                cached_filename, cached_base = cached_entry
                cached_path = os.path.join(images_dir, cached_filename)
                if os.path.exists(cached_path):
                    table_filename_with_ext = cached_filename
                    table_filename_base = cached_base
                else:
                    table_cache.pop(table_key, None)

            if not table_filename_with_ext:
                table_image, table_filename_base = extract_table_screenshot(origin_image, table_cell, page_num, table_index)
                if not table_image or not table_filename_base:
                    continue
                counter = 0
                table_filename = table_filename_base
                while os.path.exists(os.path.join(images_dir, f"{table_filename}.jpg")):
                    counter += 1
                    table_filename = f"{table_filename_base}_{counter}"
                table_filename_with_ext = f"{table_filename}.jpg"
                table_path = os.path.join(images_dir, table_filename_with_ext)
                if table_image.mode in ("RGBA", "LA", "P"):
                    table_image = table_image.convert("RGB")
                table_image.save(table_path, "JPEG", quality=95)
                with contextlib.suppress(Exception):
                    table_image.close()
                table_cache[table_key] = (table_filename_with_ext, table_filename_base)
            else:
                table_filename_base = table_filename_base or (
                    table_filename_with_ext[:-4] if table_filename_with_ext.lower().endswith(".jpg") else table_filename_with_ext
                )

            md_image_path = os.path.join("images", table_filename_with_ext).replace(os.sep, "/")
            alt_text = table_filename_base or table_filename_with_ext
            md_image_ref = f"\n\n![{alt_text}]({md_image_path})"
            insertions.append((snippet_end, md_image_ref))
            cursor_in_original = snippet_end

        if insertions:
            parts = []
            prev = 0
            for pos, text in insertions:
                parts.append(md_content[prev:pos])
                parts.append(text)
                prev = pos
            parts.append(md_content[prev:])
            md_content = "".join(parts)

    return {"md_content": md_content, "page_num": page_num}
