from __future__ import annotations

import math
import re
from typing import List


def calculate_center_distance(bbox1, bbox2):
    x1_center = (bbox1[0] + bbox1[2]) / 2
    y1_center = (bbox1[1] + bbox1[3]) / 2
    x2_center = (bbox2[0] + bbox2[2]) / 2
    y2_center = (bbox2[1] + bbox2[3]) / 2
    return math.sqrt((x1_center - x2_center) ** 2 + (y1_center - y2_center) ** 2)


def calculate_reading_order_score(bbox1, bbox2, index1, index2):
    return abs(index1 - index2) * 85 + (0 if bbox2[1] > bbox1[3] else 30)


def is_spatially_close(bbox1, bbox2, max_distance_ratio=1.8):
    center_distance = calculate_center_distance(bbox1, bbox2)

    width1 = bbox1[2] - bbox1[0]
    height1 = bbox1[3] - bbox1[1]
    width2 = bbox2[2] - bbox2[0]
    height2 = bbox2[3] - bbox2[1]
    avg_dimension = (width1 + width2 + height1 + height2) / 4

    if center_distance <= avg_dimension * max_distance_ratio:
        return True

    x_overlap = max(0, min(bbox1[2], bbox2[2]) - max(bbox1[0], bbox2[0]))
    overlap_ratio = x_overlap / min(width1, width2) if min(width1, width2) > 0 else 0
    if overlap_ratio > 0.3:
        vertical_distance = max(0, max(bbox1[1], bbox2[1]) - min(bbox1[3], bbox2[3]))
        if vertical_distance < max(height1, height2) * 1.0:
            return True

    y_overlap = max(0, min(bbox1[3], bbox2[3]) - max(bbox1[1], bbox2[1]))
    y_overlap_ratio = y_overlap / min(height1, height2) if min(height1, height2) > 0 else 0
    if y_overlap_ratio > 0.3:
        horizontal_distance = max(0, max(bbox1[0], bbox2[0]) - min(bbox1[2], bbox2[2]))
        if horizontal_distance < max(width1, width2) * 0.5:
            return True
    return False


def get_relative_position_score(bbox1, bbox2):
    y1_center = (bbox1[1] + bbox1[3]) / 2
    y2_center = (bbox2[1] + bbox2[3]) / 2
    vertical_distance = abs(y1_center - y2_center)
    x_overlap = max(0, min(bbox1[2], bbox2[2]) - max(bbox1[0], bbox2[0]))
    return vertical_distance * 0.5 if x_overlap > 0 else vertical_distance * 1.5


def match_elements_with_captions(cells, element_category):
    elements = [{"index": i, "cell": cell} for i, cell in enumerate(cells) if cell["category"] == element_category]
    captions = [{"index": i, "cell": cell} for i, cell in enumerate(cells) if cell["category"] == "Caption"]
    matches = []
    used_captions = set()
    for element in elements:
        candidate_captions = []
        for caption in captions:
            if caption["index"] in used_captions or not is_spatially_close(element["cell"]["bbox"], caption["cell"]["bbox"]):
                continue
            reading_score = calculate_reading_order_score(
                element["cell"]["bbox"], caption["cell"]["bbox"], element["index"], caption["index"]
            )
            center_distance = calculate_center_distance(element["cell"]["bbox"], caption["cell"]["bbox"])
            position_score = get_relative_position_score(element["cell"]["bbox"], caption["cell"]["bbox"])
            score = reading_score + position_score + center_distance * 0.1
            candidate_captions.append({"caption": caption, "score": score})
        if candidate_captions:
            best_match = min(candidate_captions, key=lambda item: item["score"])
            matches.append(
                {"element": element, "caption_text": best_match["caption"]["cell"].get("text", "").strip()}
            )
            used_captions.add(best_match["caption"]["index"])
        else:
            matches.append({"element": element, "caption_text": None})
    return matches


def generate_image_name(picture_cell, picture_index, page_num):
    if picture_cell and picture_cell.get("caption_text"):
        caption_text = picture_cell["caption_text"]
        caption_text = re.sub(r"(\d+)\.(\d+)", r"\1-\2", caption_text)
        safe_caption = re.sub(r"[^\w\s-]", "", caption_text).strip()
        safe_caption = re.sub(r"[-\s]+", "_", safe_caption)
        if safe_caption:
            return f"page_{page_num}_{safe_caption[:50]}"
    return f"page_{page_num}_img_{picture_index + 1}"


def extract_table_screenshot(image, table_cell, page_num, table_index):
    try:
        table_bbox = table_cell["bbox"]
        img_width, img_height = image.size
        x1 = max(0, int(table_bbox[0]))
        y1 = max(0, int(table_bbox[1]))
        x2 = min(img_width, int(table_bbox[2]))
        y2 = min(img_height, int(table_bbox[3]))
        table_image = image.crop((x1, y1, x2, y2))

        filename_base = f"page_{page_num}_table_{table_index + 1}"
        if table_cell.get("caption_text"):
            caption_text = table_cell["caption_text"]
            caption_text = re.sub(r"(\d+)\.(\d+)", r"\1-\2", caption_text)
            safe_caption = re.sub(r"[^\w\s-]", "", caption_text).strip()
            safe_caption = re.sub(r"[-\s]+", "_", safe_caption)
            if safe_caption:
                filename_base = f"page_{page_num}_table_{safe_caption[:50]}"

        return table_image, filename_base
    except Exception:
        return None, None


def _perform_intra_page_matching(self, cells: List[dict]) -> List[dict]:
    picture_matches = match_elements_with_captions(cells, "Picture")
    for match in picture_matches:
        pic_cell = match["element"]["cell"]
        pic_cell["is_matched"] = True
        if match["caption_text"]:
            pic_cell["caption_text"] = match["caption_text"]
            for cell in cells:
                if cell.get("text", "").strip() == match["caption_text"] and cell["category"] == "Caption":
                    cell["is_matched"] = True
                    cell["locked_for_picture"] = True
                    break

    tables = [cell for cell in cells if cell["category"] == "Table" and not cell.get("is_matched")]
    captions = [cell for cell in cells if cell["category"] == "Caption" and not cell.get("is_matched")]
    captions.sort(key=lambda cell: cell["bbox"][1])

    for cap_cell in captions:
        if cap_cell.get("is_matched"):
            continue

        best_table_candidate = None
        min_distance = float("inf")
        cap_bbox = cap_cell["bbox"]
        cap_width = cap_bbox[2] - cap_bbox[0]
        y_center_cap = (cap_bbox[1] + cap_bbox[3]) / 2

        for tbl_cell in tables:
            if tbl_cell.get("is_matched"):
                continue
            tbl_bbox = tbl_cell["bbox"]
            y_center_tbl = (tbl_bbox[1] + tbl_bbox[3]) / 2
            if y_center_tbl < y_center_cap:
                continue

            vertical_distance = tbl_bbox[1] - cap_bbox[3]
            if vertical_distance > 300:
                continue

            tbl_width = tbl_bbox[2] - tbl_bbox[0]
            x_overlap = max(0, min(tbl_bbox[2], cap_bbox[2]) - max(tbl_bbox[0], cap_bbox[0]))
            overlap_ratio = x_overlap / min(tbl_width, cap_width) if min(tbl_width, cap_width) > 0 else 0
            if overlap_ratio < 0.2:
                continue

            if vertical_distance < min_distance:
                min_distance = vertical_distance
                best_table_candidate = tbl_cell

        if best_table_candidate:
            best_table_candidate["is_matched"] = True
            best_table_candidate["caption_text"] = cap_cell.get("text", "").strip()
            cap_cell["is_matched"] = True

    tables.sort(key=lambda cell: cell["bbox"][1])
    for tbl_cell in tables:
        if tbl_cell.get("is_matched"):
            continue
        best_caption_candidate = None
        min_distance = float("inf")
        tbl_bbox = tbl_cell["bbox"]
        tbl_width = tbl_bbox[2] - tbl_bbox[0]

        for cap_cell in captions:
            if cap_cell.get("is_matched"):
                continue
            cap_bbox = cap_cell["bbox"]
            if cap_bbox[1] < tbl_bbox[3]:
                continue
            vertical_distance = cap_bbox[1] - tbl_bbox[3]
            if vertical_distance > 300:
                continue
            cap_width = cap_bbox[2] - cap_bbox[0]
            x_overlap = max(0, min(tbl_bbox[2], cap_bbox[2]) - max(tbl_bbox[0], cap_bbox[0]))
            overlap_ratio = x_overlap / min(tbl_width, cap_width) if min(tbl_width, cap_width) > 0 else 0
            if overlap_ratio < 0.2:
                continue
            if vertical_distance < min_distance:
                min_distance = vertical_distance
                best_caption_candidate = cap_cell

        if best_caption_candidate:
            tbl_cell["is_matched"] = True
            tbl_cell["caption_text"] = best_caption_candidate.get("text", "").strip()
            best_caption_candidate["is_matched"] = True

    return self._merge_adjacent_text_blocks_in_same_page(cells)


def _merge_adjacent_text_blocks_in_same_page(self, cells: List[dict]) -> List[dict]:
    if not cells:
        return cells
    idx = 0
    total_cells = len(cells)
    while idx < total_cells - 1:
        current_cell = cells[idx]
        if current_cell.get("category") != "Text":
            idx += 1
            continue
        next_cell = cells[idx + 1]
        if next_cell.get("category") != "Text":
            idx += 1
            continue
        if not self._should_merge_same_page_text_blocks(current_cell, next_cell):
            idx += 1
            continue
        self._merge_two_text_cells(current_cell, next_cell)
        cells.pop(idx + 1)
        total_cells -= 1
    return cells


def _should_merge_same_page_text_blocks(self, first_cell: dict, second_cell: dict) -> bool:
    text1 = first_cell.get("text") or ""
    text2 = second_cell.get("text") or ""
    if not text1.strip() or not text2.strip():
        return False
    if self.ends_with_digit_pattern.search(text1.rstrip()):
        return False
    if self._is_english_to_chinese_transition(text1, text2):
        return False
    if self._is_sentence_end(text1) or self._is_toc_entry(text1):
        return False
    if self._starts_new_paragraph(text2) or self._is_toc_entry(text2):
        return False
    if text2.lstrip().startswith(("-", "*", "•")):
        return False
    if text1.rstrip().endswith("\n\n"):
        return False
    return True


def _merge_two_text_cells(self, base_cell: dict, next_cell: dict) -> None:
    text1 = base_cell.get("text") or ""
    text2 = next_cell.get("text") or ""
    stripped_text1 = text1.rstrip()
    merge_type = "Standard"
    if stripped_text1.endswith("-") and len(stripped_text1) > 1 and self.english_letter_pattern.match(text2.lstrip()):
        base_cell["text"] = stripped_text1[:-1] + text2.lstrip()
        merge_type = "Hyphenated"
    else:
        t1_stripped = text1.rstrip()
        t2_stripped = text2.lstrip()
        final_text = t1_stripped + t2_stripped
        if t1_stripped and t2_stripped and re.search(r"[a-zA-Z0-9]$", t1_stripped) and re.search(r"^[a-zA-Z0-9]", t2_stripped):
            final_text = t1_stripped + " " + t2_stripped
        base_cell["text"] = final_text
    if self.debug_matching:
        self._console_write(
            f"[DEBUG] Merged text ({merge_type}) within page between blocks at {base_cell.get('bbox')} and {next_cell.get('bbox')}."
        )


def _is_path_clean_between(self, elem1_info, elem2_info, all_pages_data, allowed_categories):
    elements = sorted([elem1_info, elem2_info], key=lambda item: (item["page_num"], item["cell"]["bbox"][1]))
    start_info, end_info = elements[0], elements[1]
    start_page_idx = start_info["page_num"] - 1
    end_page_idx = end_info["page_num"] - 1
    for p_idx in range(start_page_idx, end_page_idx + 1):
        page_cells = all_pages_data[p_idx].get("cells", [])
        if not page_cells:
            continue
        y_start_check = 0
        y_end_check = float("inf")
        if p_idx == start_page_idx:
            y_start_check = start_info["cell"]["bbox"][3]
        if p_idx == end_page_idx:
            y_end_check = end_info["cell"]["bbox"][1]
        for cell in page_cells:
            if cell is start_info["cell"] or cell is end_info["cell"]:
                continue
            cell_y_center = (cell["bbox"][1] + cell["bbox"][3]) / 2
            if y_start_check <= cell_y_center <= y_end_check and cell["category"] not in allowed_categories:
                if self.debug_matching:
                    self._console_write(
                        f"[DEBUG] Path blocked on page {p_idx + 1} between elements on pg {start_info['page_num']} and pg {end_info['page_num']} by a '{cell['category']}' element."
                    )
                return False
    return True


def _is_valid_continuation_page(self, page_data, allowed_categories):
    if not page_data.get("cells"):
        return False
    for cell in page_data["cells"]:
        if cell["category"] not in allowed_categories:
            return False
    return True


def _is_contiguous(self, elem1_info: dict, elem2_info: dict, all_pages_data: List[dict]) -> bool:
    if (elem1_info["page_num"], elem1_info["cell"]["bbox"][1]) > (elem2_info["page_num"], elem2_info["cell"]["bbox"][1]):
        elem1_info, elem2_info = elem2_info, elem1_info
    if elem1_info["page_num"] == elem2_info["page_num"]:
        page_idx = elem1_info["page_num"] - 1
        y_start = elem1_info["cell"]["bbox"][3]
        y_end = elem2_info["cell"]["bbox"][1]
        for cell in all_pages_data[page_idx].get("cells", []):
            if cell is elem1_info["cell"] or cell is elem2_info["cell"]:
                continue
            cell_y_center = (cell["bbox"][1] + cell["bbox"][3]) / 2
            if y_start < cell_y_center < y_end and cell["category"] != "Table":
                return False
    return self._is_path_clean_between(
        elem1_info, elem2_info, all_pages_data, allowed_categories={"Table", "Page-header", "Page-footer", "Footnote"}
    )


def _is_toc_entry(self, text: str, min_dots: int = 4) -> bool:
    if not text:
        return False
    dot_like_chars = {".", "…"}
    consecutive = 0
    for char in text:
        if char in dot_like_chars:
            consecutive += 1
            if consecutive >= min_dots:
                return True
        else:
            consecutive = 0
    return False


def _perform_cross_page_table_caption_matching(self, all_pages_data: List[dict]):
    all_tables = []
    for page_data in all_pages_data:
        for cell in page_data.get("cells", []):
            if cell["category"] == "Table":
                all_tables.append({"page_num": page_data["original_page_num"], "cell": cell})
    all_tables.sort(key=lambda item: (item["page_num"], item["cell"]["bbox"][1]))
    if not all_tables:
        return

    table_groups = []
    visited_table_ids = set()
    for i, tbl_info in enumerate(all_tables):
        if id(tbl_info["cell"]) in visited_table_ids:
            continue
        current_group = [tbl_info]
        visited_table_ids.add(id(tbl_info["cell"]))
        last_member_in_group = tbl_info
        for j in range(i + 1, len(all_tables)):
            next_tbl_info = all_tables[j]
            if id(next_tbl_info["cell"]) in visited_table_ids:
                continue
            if self._is_contiguous(last_member_in_group, next_tbl_info, all_pages_data):
                current_group.append(next_tbl_info)
                visited_table_ids.add(id(next_tbl_info["cell"]))
                last_member_in_group = next_tbl_info
        table_groups.append(current_group)

    caption_text_to_cell_map = {}
    for page_data in all_pages_data:
        for cell in page_data.get("cells", []):
            if cell["category"] == "Caption":
                caption_text_to_cell_map[cell.get("text", "").strip()] = cell

    allowed_categories_in_path = {"Table", "Page-header", "Page-footer", "Footnote"}
    search_window = 12

    for group in table_groups:
        group_caption_text = None
        first_table_in_group = group[0]
        if first_table_in_group["cell"].get("is_matched") and first_table_in_group["cell"].get("caption_text"):
            group_caption_text = first_table_in_group["cell"]["caption_text"]
            if self.debug_matching:
                self._console_write(
                    f"[DEBUG] Group starting on page {group[0]['page_num']} inherits caption '{group_caption_text[:30]}...' from its first element."
                )

        if group_caption_text:
            for tbl_info in group:
                old_caption = tbl_info["cell"].get("caption_text")
                if old_caption and old_caption != group_caption_text:
                    caption_cell_to_release = caption_text_to_cell_map.get(old_caption)
                    if caption_cell_to_release:
                        caption_cell_to_release["is_matched"] = False

        if not group_caption_text:
            best_candidate = {"caption_info": None, "score": float("inf")}
            group_start_info = group[0]
            group_end_info = group[-1]
            unmatched_captions = []
            for page_data in all_pages_data:
                for cell in page_data.get("cells", []):
                    if cell["category"] == "Caption" and not cell.get("is_matched") and not cell.get("locked_for_picture"):
                        unmatched_captions.append({"page_num": page_data["original_page_num"], "cell": cell})

            for cap_info in unmatched_captions:
                page_diff_head = group_start_info["page_num"] - cap_info["page_num"]
                if 0 <= page_diff_head <= search_window:
                    if self._is_path_clean_between(cap_info, group_start_info, all_pages_data, allowed_categories_in_path):
                        distance = calculate_center_distance(cap_info["cell"]["bbox"], group_start_info["cell"]["bbox"])
                        score = distance + page_diff_head * 500
                        if page_diff_head == 0 and group_start_info["cell"]["bbox"][1] < cap_info["cell"]["bbox"][3]:
                            continue
                        if score < best_candidate["score"]:
                            best_candidate = {"caption_info": cap_info, "score": score}

                page_diff_tail = cap_info["page_num"] - group_end_info["page_num"]
                if 0 <= page_diff_tail <= search_window:
                    if self._is_path_clean_between(group_end_info, cap_info, all_pages_data, allowed_categories_in_path):
                        distance = calculate_center_distance(group_end_info["cell"]["bbox"], cap_info["cell"]["bbox"])
                        score = distance + page_diff_tail * 500
                        if page_diff_tail == 0 and cap_info["cell"]["bbox"][1] < group_end_info["cell"]["bbox"][3]:
                            continue
                        if score < best_candidate["score"]:
                            best_candidate = {"caption_info": cap_info, "score": score}

            if best_candidate["caption_info"]:
                source_caption_info = best_candidate["caption_info"]
                group_caption_text = source_caption_info["cell"].get("text", "").strip()
                source_caption_info["cell"]["is_matched"] = True

        if group_caption_text:
            authoritative_caption_cell = caption_text_to_cell_map.get(group_caption_text)
            if authoritative_caption_cell:
                authoritative_caption_cell["is_matched"] = True
            for tbl_info in group:
                tbl_info["cell"]["is_matched"] = True
                tbl_info["cell"]["caption_text"] = group_caption_text


def _is_sentence_end(self, text: str) -> bool:
    if not text:
        return False
    sentence_end_chars = {"。", "？", "！", ".", "?", "!"}
    closing_punctuation = {'"', "'", "”", "’", "』", ")", "）", "]"}
    stripped_text = text.rstrip()
    while stripped_text and stripped_text[-1] in closing_punctuation:
        stripped_text = stripped_text[:-1].rstrip()
    if not stripped_text:
        return True
    return stripped_text[-1] in sentence_end_chars


def _starts_new_paragraph(self, text: str) -> bool:
    if not text:
        return False
    return bool(self.NEW_PARAGRAPH_PATTERN.match(text))


def _perform_cross_page_text_merging(self, all_pages_layout_data: List[dict]):
    for i in range(len(all_pages_layout_data) - 1):
        current_page_data = all_pages_layout_data[i]
        next_page_data = all_pages_layout_data[i + 1]
        if not current_page_data.get("cells") or not next_page_data.get("cells"):
            continue

        last_text_block_curr_page = None
        for cell in reversed(current_page_data["cells"]):
            if cell.get("category") == "Text":
                last_text_block_curr_page = cell
                break
        if not last_text_block_curr_page:
            continue

        first_text_block_next_page = None
        first_text_block_index = -1
        for idx, cell in enumerate(next_page_data["cells"]):
            if cell.get("category") == "Text":
                first_text_block_next_page = cell
                first_text_block_index = idx
                break
        if not first_text_block_next_page:
            continue

        if self._is_path_clean_between_texts(
            last_text_block_curr_page, first_text_block_next_page, current_page_data, next_page_data
        ):
            text1 = last_text_block_curr_page.get("text", "")
            text2 = first_text_block_next_page.get("text", "")
            if self.ends_with_digit_pattern.search(text1.rstrip()):
                continue
            if self._is_english_to_chinese_transition(text1, text2):
                continue
            if self._is_sentence_end(text1) or self._is_toc_entry(text1):
                continue
            if first_text_block_index > 0:
                element_before = next_page_data["cells"][first_text_block_index - 1]
                if element_before.get("category") == "Section-header":
                    header_text = element_before.get("text", "").strip()
                    if self._starts_new_paragraph(header_text):
                        continue
            if self._starts_new_paragraph(text2) or self._is_toc_entry(text2):
                continue

            stripped_text1 = text1.rstrip()
            if stripped_text1.endswith("-") and len(stripped_text1) > 1 and self.english_letter_pattern.match(text2.lstrip()):
                last_text_block_curr_page["text"] = stripped_text1[:-1] + text2.lstrip()
            else:
                t1_stripped = text1.rstrip()
                t2_stripped = text2.lstrip()
                final_text = t1_stripped + t2_stripped
                if t1_stripped and t2_stripped and re.search(r"[a-zA-Z0-9]$", t1_stripped) and re.search(
                    r"^[a-zA-Z0-9]", t2_stripped
                ):
                    final_text = t1_stripped + " " + t2_stripped
                last_text_block_curr_page["text"] = final_text
            first_text_block_next_page["to_be_deleted"] = True

    for page_data in all_pages_layout_data:
        if page_data.get("cells"):
            page_data["cells"] = [cell for cell in page_data["cells"] if not cell.get("to_be_deleted")]


def _is_path_clean_between_texts(self, last_text_cell_page1, first_text_cell_page2, page1_data, page2_data) -> bool:
    allowed_categories = {"Picture", "Page-header", "Page-footer", "Section-header", "Footnote"}
    found_last_text = False
    for cell in page1_data.get("cells", []):
        if cell is last_text_cell_page1:
            found_last_text = True
            continue
        if found_last_text and cell["category"] not in allowed_categories:
            return False

    for cell in page2_data.get("cells", []):
        if cell is first_text_cell_page2:
            break
        if cell["category"] not in allowed_categories:
            return False
    return True
