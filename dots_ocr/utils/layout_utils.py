from PIL import Image
Image.MAX_IMAGE_PIXELS = 200000000
from typing import Dict, List, Optional, Tuple

import fitz
from io import BytesIO
import json
import re
from json_repair import loads as json_repair_loads

from dots_ocr.utils.image_utils import smart_resize
from dots_ocr.utils.consts import MIN_PIXELS, MAX_PIXELS
from dots_ocr.utils.output_cleaner import OutputCleaner


# Define a color map (using RGB format)
dict_layout_type_to_color = {
    "Text": (0, 128, 0),  # #008000
    "Picture": (255, 0, 255),  # #ff00ff
    "Caption": (255, 165, 0),  # #ffa500
    "Section-header": (0, 255, 255),  # #00ffff
    "Footnote": (109, 244, 109),  # #6df46d
    "Formula": (128, 128, 128),  # #808080
    "Table": (255, 192, 203),  # #ffc0cb
    "Title": (61, 0, 230),  # #3d00e6
    "List-item": (0, 0, 255),  # #0000ff
    "Page-header": (215, 246, 62),  # #d7f63e
    "Page-footer":  (128, 0, 128),  # #800080
    "Other": (165, 42, 42),  # Brown (Fallback)
    "Unknown": (0, 0, 0),  # Black (Fallback)
}


def _resolve_resized_dims(
    original_width: int,
    original_height: int,
    resized_height: int = None,
    resized_width: int = None,
    min_pixels: int = None,
    max_pixels: int = None,
):
    if resized_height and resized_width:
        return resized_height, resized_width
    if original_width <= 0 or original_height <= 0:
        return None, None
    min_pixels = min_pixels or MIN_PIXELS
    max_pixels = max_pixels or MAX_PIXELS
    try:
        resized_height, resized_width = smart_resize(
            original_height,
            original_width,
            min_pixels=min_pixels,
            max_pixels=max_pixels,
        )
    except Exception:
        return None, None
    return resized_height, resized_width


def _transform_cells_to_original(
    cells: List[Dict],
    original_size: Tuple[int, int],
    resized_height: int = None,
    resized_width: int = None,
    min_pixels: int = None,
    max_pixels: int = None,
) -> List[Dict]:
    if not cells:
        return cells

    original_width, original_height = original_size
    resized_height, resized_width = _resolve_resized_dims(
        original_width,
        original_height,
        resized_height=resized_height,
        resized_width=resized_width,
        min_pixels=min_pixels,
        max_pixels=max_pixels,
    )
    if not resized_height or not resized_width:
        return cells
    if original_width == 0 or original_height == 0:
        return cells

    scale_x = resized_width / original_width
    scale_y = resized_height / original_height
    if abs(scale_x - 1.0) < 1e-3 and abs(scale_y - 1.0) < 1e-3:
        return cells

    transformed: List[Dict] = []
    for cell in cells:
        bbox = cell.get("bbox")
        if not bbox or len(bbox) != 4:
            transformed.append(cell)
            continue
        try:
            x0, y0, x1, y1 = map(float, bbox)
        except (TypeError, ValueError):
            transformed.append(cell)
            continue
        scaled_bbox = [
            int(round(x0 / scale_x)),
            int(round(y0 / scale_y)),
            int(round(x1 / scale_x)),
            int(round(y1 / scale_y)),
        ]
        cell_copy = cell.copy()
        cell_copy["bbox"] = scaled_bbox
        transformed.append(cell_copy)

    tolerance_x = max(4, int(original_width * 0.02))
    tolerance_y = max(4, int(original_height * 0.02))
    out_of_bounds = 0
    for cell in transformed:
        bbox = cell.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = bbox
        if (
            x1 > original_width + tolerance_x
            or y1 > original_height + tolerance_y
            or x0 < -tolerance_x
            or y0 < -tolerance_y
        ):
            out_of_bounds += 1

    if out_of_bounds > len(transformed) * 0.4:
        return cells

    clipped: List[Dict] = []
    for cell in transformed:
        bbox = cell.get("bbox")
        if not bbox or len(bbox) != 4:
            clipped.append(cell)
            continue
        x0, y0, x1, y1 = bbox
        clipped_bbox = [
            max(0, min(original_width, x0)),
            max(0, min(original_height, y0)),
            max(0, min(original_width, x1)),
            max(0, min(original_height, y1)),
        ]
        cell_copy = cell.copy()
        cell_copy["bbox"] = clipped_bbox
        clipped.append(cell_copy)

    return clipped


def _cells_fit_resized_space(
    cells: List[Dict],
    resized_width: int,
    resized_height: int,
    original_width: int,
    original_height: int,
) -> bool:
    if not cells:
        return False
    if resized_width <= 0 or resized_height <= 0:
        return False
    if original_width <= 0 or original_height <= 0:
        return False

    tol_x = max(4, int(resized_width * 0.02))
    tol_y = max(4, int(resized_height * 0.02))
    valid = 0
    in_bounds = 0
    max_x = None
    max_y = None

    for cell in cells:
        bbox = cell.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        try:
            x0, y0, x1, y1 = map(float, bbox)
        except (TypeError, ValueError):
            continue
        if x1 <= x0 or y1 <= y0:
            continue
        valid += 1
        if (
            x0 >= -tol_x
            and y0 >= -tol_y
            and x1 <= resized_width + tol_x
            and y1 <= resized_height + tol_y
        ):
            in_bounds += 1
        max_x = x1 if max_x is None else max(max_x, x1)
        max_y = y1 if max_y is None else max(max_y, y1)

    if valid == 0 or max_x is None or max_y is None:
        return False
    if in_bounds / valid < 0.7:
        return False

    scale_x = resized_width / original_width
    scale_y = resized_height / original_height
    if scale_x <= 0 or scale_y <= 0:
        return False

    max_x_ratio = max_x / original_width
    max_y_ratio = max_y / original_height
    dist_to_resized = abs(max_x_ratio - scale_x) + abs(max_y_ratio - scale_y)
    dist_to_original = abs(max_x_ratio - 1.0) + abs(max_y_ratio - 1.0)
    return dist_to_resized <= dist_to_original


def draw_layout_on_image(image, cells, resized_height=None, resized_width=None, fill_bbox=True, draw_bbox=True):
    """
    Draw transparent boxes on an image.
    
    Args:
        image: The source PIL Image.
        cells: A list of cells containing bounding box information.
        resized_height/width: If provided, the model's resized canvas size; we auto-rescale
            bboxes from that space back to the original image space when needed.
        fill_bbox: Whether to fill the bounding box.
        draw_bbox: Whether to draw the bounding box.
        
    Returns:
        PIL.Image: The image with drawings.
    """
    original_width, original_height = image.size

    def _score_and_clip(candidate_cells):
        """
        Score a set of cells against the original image size.
        Returns clipped cells plus a score tuple for comparison.
        """
        if not candidate_cells:
            return [], (float("inf"), 0.0, 0)

        tol_x = max(4, int(original_width * 0.02))
        tol_y = max(4, int(original_height * 0.02))
        img_area = max(1, original_width * original_height)

        clipped_cells = []
        out_of_bounds = 0
        total_area = 0
        valid = 0

        for cell in candidate_cells:
            bbox = cell.get('bbox')
            if not bbox or len(bbox) != 4:
                clipped_cells.append(cell)
                continue
            try:
                x0, y0, x1, y1 = map(float, bbox)
            except (TypeError, ValueError):
                clipped_cells.append(cell)
                continue
            if x1 <= x0 or y1 <= y0:
                clipped_cells.append(cell)
                continue

            if (
                x0 < -tol_x or y0 < -tol_y
                or x1 > original_width + tol_x
                or y1 > original_height + tol_y
            ):
                out_of_bounds += 1

            cx0 = max(0, min(original_width, x0))
            cy0 = max(0, min(original_height, y0))
            cx1 = max(0, min(original_width, x1))
            cy1 = max(0, min(original_height, y1))
            area = max(0, cx1 - cx0) * max(0, cy1 - cy0)
            total_area += area
            valid += 1

            cell_copy = cell.copy()
            cell_copy['bbox'] = [int(cx0), int(cy0), int(cx1), int(cy1)]
            clipped_cells.append(cell_copy)

        coverage = total_area / img_area
        # score: fewer out-of-bounds first, then higher coverage, then more valids
        return clipped_cells, (out_of_bounds, -coverage, -valid)

    def _rescale_if_needed():
        """
        Generate two candidates (raw and rescaled) when resized dims are provided,
        then pick the healthier one based on bounds/coverage.
        """
        raw_cells = cells if isinstance(cells, list) else []
        raw_clipped, raw_score = _score_and_clip(raw_cells)

        if original_width == 0 or original_height == 0:
            return raw_clipped

        target_resized_height, target_resized_width = _resolve_resized_dims(
            original_width,
            original_height,
            resized_height=resized_height,
            resized_width=resized_width,
            min_pixels=MIN_PIXELS,
            max_pixels=MAX_PIXELS,
        )
        if not target_resized_height or not target_resized_width:
            return raw_clipped

        scale_x = target_resized_width / original_width
        scale_y = target_resized_height / original_height
        if scale_x <= 0 or scale_y <= 0:
            return raw_clipped
        if abs(scale_x - 1.0) < 1e-3 and abs(scale_y - 1.0) < 1e-3:
            return raw_clipped

        should_rescale = _cells_fit_resized_space(
            raw_cells,
            target_resized_width,
            target_resized_height,
            original_width,
            original_height,
        )
        if not should_rescale and raw_score[0] > max(1, int(len(raw_cells) * 0.2)):
            should_rescale = True

        if not should_rescale:
            return raw_clipped

        rescaled_cells = _transform_cells_to_original(
            raw_cells,
            (original_width, original_height),
            resized_height=target_resized_height,
            resized_width=target_resized_width,
            min_pixels=MIN_PIXELS,
            max_pixels=MAX_PIXELS,
        )
        rescaled_clipped, rescaled_score = _score_and_clip(rescaled_cells)
        if rescaled_score[0] > raw_score[0] and raw_score[0] <= 1:
            return raw_clipped
        return rescaled_clipped

    cells_to_draw = _rescale_if_needed()

    # Create a new PDF document
    doc = fitz.open()
    
    # Get image information
    img_bytes = BytesIO()
    image.save(img_bytes, format='PNG')
    pix = fitz.Pixmap(img_bytes)
    
    # Create a page
    page = doc.new_page(width=pix.width, height=pix.height)
    page.insert_image(
        fitz.Rect(0, 0, pix.width, pix.height), 
        pixmap=pix
        )

    for i, cell in enumerate(cells_to_draw):
        bbox = cell['bbox']
        layout_type = cell.get('category', 'Unknown')
        order = i
        
        top_left = (bbox[0], bbox[1])
        down_right = (bbox[2], bbox[3])
            
        color = dict_layout_type_to_color.get(layout_type, (0, 128, 0))
        color = [col/255 for col in color[:3]]

        x0, y0, x1, y1 = top_left[0], top_left[1], down_right[0], down_right[1]
        rect_coords = fitz.Rect(x0, y0, x1, y1)
        if draw_bbox:
            if fill_bbox:
                page.draw_rect(
                    rect_coords,
                    color=None,
                    fill=color,
                    fill_opacity=0.3,
                    width=0.5,
                    overlay=True,
                )  # Draw the rectangle
            else:
                page.draw_rect(
                    rect_coords,
                    color=color,
                    fill=None,
                    fill_opacity=1,
                    width=0.5,
                    overlay=True,
                )  # Draw the rectangle
        order_cate = f"{order}_{layout_type}"
        page.insert_text(
            (x1, y0 + 20), order_cate, fontsize=20, color=color
        )  # Insert the index in the top left corner of the rectangle

    # Convert to a Pixmap (maintaining original dimensions)
    mat = fitz.Matrix(1.0, 1.0)
    pix = page.get_pixmap(matrix=mat)

    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def pre_process_bboxes(
    origin_image,
    bboxes,
    input_width,
    input_height,
    factor: int = 28,
    min_pixels: int = 3136, 
    max_pixels: int = 11289600
):
    assert isinstance(bboxes, list) and len(bboxes) > 0 and isinstance(bboxes[0], list)
    min_pixels = min_pixels or MIN_PIXELS
    max_pixels = max_pixels or MAX_PIXELS
    original_width, original_height = origin_image.size

    input_height, input_width = smart_resize(input_height, input_width, min_pixels=min_pixels, max_pixels=max_pixels)
    
    scale_x = original_width / input_width
    scale_y = original_height / input_height

    bboxes_out = []
    for bbox in bboxes:
        bbox_resized = [
            int(float(bbox[0]) / scale_x), 
            int(float(bbox[1]) / scale_y),
            int(float(bbox[2]) / scale_x), 
            int(float(bbox[3]) / scale_y)
        ]
        bboxes_out.append(bbox_resized)
    
    return bboxes_out

def post_process_cells(
    origin_image: Image.Image, 
    cells: List[Dict], 
    input_width,  # server input width, also has smart_resize in server
    input_height,
    factor: int = 28,
    min_pixels: int = 3136, 
    max_pixels: int = 11289600
) -> List[Dict]:
    """
    Post-processes cell bounding boxes, converting coordinates from the resized dimensions back to the original dimensions.
    
    Args:
        origin_image: The original PIL Image.
        cells: A list of cells containing bounding box information.
        input_width: The width of the input image sent to the server.
        input_height: The height of the input image sent to the server.
        factor: Resizing factor.
        min_pixels: Minimum number of pixels.
        max_pixels: Maximum number of pixels.
        
    Returns:
        A list of post-processed cells.
    """
    assert isinstance(cells, list) and len(cells) > 0 and isinstance(cells[0], dict)
    min_pixels = min_pixels or MIN_PIXELS
    configured_max_pixels = max_pixels or MAX_PIXELS
    try:
        configured_max_pixels = float(configured_max_pixels)
    except (TypeError, ValueError):
        configured_max_pixels = float(MAX_PIXELS)
    if configured_max_pixels <= 0:
        configured_max_pixels = float(MAX_PIXELS)

    original_width, original_height = origin_image.size
    if original_width <= 0 or original_height <= 0:
        return cells

    def _score_candidate(candidate_cells: List[Dict]) -> Tuple[int, float, int]:
        tol_x = max(4, int(original_width * 0.02))
        tol_y = max(4, int(original_height * 0.02))
        img_area = max(1, original_width * original_height)
        out_of_bounds = 0
        total_area = 0.0
        valid = 0

        for cell in candidate_cells:
            if not isinstance(cell, dict):
                continue
            bbox = cell.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            try:
                x0, y0, x1, y1 = map(float, bbox)
            except (TypeError, ValueError):
                continue
            if x1 <= x0 or y1 <= y0:
                continue

            if (
                x0 < -tol_x or y0 < -tol_y
                or x1 > original_width + tol_x
                or y1 > original_height + tol_y
            ):
                out_of_bounds += 1

            cx0 = max(0, min(original_width, x0))
            cy0 = max(0, min(original_height, y0))
            cx1 = max(0, min(original_width, x1))
            cy1 = max(0, min(original_height, y1))
            total_area += max(0.0, cx1 - cx0) * max(0.0, cy1 - cy0)
            valid += 1

        coverage = total_area / img_area
        # lower is better: fewer out-of-bounds, then larger coverage, then more valid cells
        return (out_of_bounds, -coverage, -valid)

    def _build_candidate(candidate_max_pixels: float) -> Optional[Dict[str, object]]:
        try:
            resized_h, resized_w = smart_resize(
                int(input_height),
                int(input_width),
                min_pixels=int(min_pixels),
                max_pixels=int(candidate_max_pixels),
            )
        except Exception:
            return None

        transformed = _transform_cells_to_original(
            cells,
            (original_width, original_height),
            resized_height=resized_h,
            resized_width=resized_w,
            min_pixels=int(min_pixels),
            max_pixels=int(candidate_max_pixels),
        )
        fit_resized = _cells_fit_resized_space(
            cells,
            resized_w,
            resized_h,
            original_width,
            original_height,
        )
        return {
            "cells": transformed,
            "fit_resized": fit_resized,
            "score": _score_candidate(transformed),
        }

    candidate_max_values: List[float] = [configured_max_pixels]
    # Many servers clamp layout canvas around MAX_PIXELS even if client allows larger images.
    # Keep a canonical fallback candidate to avoid systematic top-left-shrunk boxes.
    if configured_max_pixels > MAX_PIXELS:
        candidate_max_values.append(float(MAX_PIXELS))

    candidates = [c for c in (_build_candidate(v) for v in candidate_max_values) if c is not None]
    if not candidates:
        return cells

    best = candidates[0]
    for cand in candidates[1:]:
        if cand["fit_resized"] != best["fit_resized"]:
            if cand["fit_resized"]:
                best = cand
            continue
        if cand["score"] < best["score"]:
            best = cand

    return best["cells"]  # type: ignore[return-value]

def is_legal_bbox(cells):
    for cell in cells:
        bbox = cell['bbox']
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return False
    return True


_REPEAT_TRIGGER = 10
_REPEAT_LIMIT = 5
_MAX_SUBSTRING_UNITS = 24
_MAX_SEQUENCE_TOKENS = 4


def _collapse_repeated_substrings(value: str, trigger: int, limit: int) -> str:
    if not value or trigger <= limit:
        return value
    max_unit = min(_MAX_SUBSTRING_UNITS, max(0, len(value) // trigger))
    if max_unit <= 0:
        return value
    for unit_len in range(1, max_unit + 1):
        pattern = re.compile(r'((?:.{{{}}}))\1{{{},}}'.format(unit_len, trigger - 1), re.DOTALL)
        while True:
            match = pattern.search(value)
            if not match:
                break
            unit = match.group(1)
            value = value[:match.start()] + unit * limit + value[match.end():]
    return value


def _tokenize_with_prefix(text: str) -> Tuple[List[Dict[str, str]], str]:
    entries: List[Dict[str, str]] = []
    pos = 0
    for match in re.finditer(r'\S+', text):
        leading_ws = text[pos:match.start()]
        token = match.group(0)
        entries.append({'leading_ws': leading_ws, 'token': token})
        pos = match.end()
    trailing_ws = text[pos:]
    return entries, trailing_ws


def _limit_repeated_token_sequences(entries: List[Dict[str, str]], trigger: int, limit: int) -> None:
    if trigger <= limit or len(entries) < trigger:
        return
    max_seq = min(_MAX_SEQUENCE_TOKENS, len(entries))
    for seq_len in range(max_seq, 0, -1):
        i = 0
        while i + seq_len <= len(entries):
            seq = tuple(entry['token'] for entry in entries[i:i + seq_len])
            run_len = 1
            j = i + seq_len
            while j + seq_len <= len(entries):
                next_seq = tuple(entry['token'] for entry in entries[j:j + seq_len])
                if next_seq != seq:
                    break
                run_len += 1
                j += seq_len
            if run_len >= trigger:
                keep_sequences = min(limit, run_len)
                remove_start = i + keep_sequences * seq_len
                remove_end = i + run_len * seq_len
                del entries[remove_start:remove_end]
                continue
            i += 1


def sanitize_repeated_tokens_in_text(text: str, trigger: int = _REPEAT_TRIGGER, limit: int = _REPEAT_LIMIT) -> str:
    if not text:
        return text
    cleaned = _collapse_repeated_substrings(text, trigger, limit)
    entries, trailing_ws = _tokenize_with_prefix(cleaned)
    if not entries:
        return cleaned
    for entry in entries:
        entry['token'] = _collapse_repeated_substrings(entry['token'], trigger, limit)
    if len(entries) >= trigger:
        _limit_repeated_token_sequences(entries, trigger, limit)
    return ''.join(entry['leading_ws'] + entry['token'] for entry in entries) + trailing_ws


def sanitize_cells_repeated_tokens(cells: List[Dict], trigger: int = _REPEAT_TRIGGER, limit: int = _REPEAT_LIMIT) -> List[Dict]:
    if not cells or trigger <= limit:
        return cells
    for cell in cells:
        text = cell.get('text')
        if isinstance(text, str) and text:
            new_text = sanitize_repeated_tokens_in_text(text, trigger, limit)
            if new_text != text:
                cell['text'] = new_text
    return cells

# 增加json_reapir的修复
def post_process_output(response, prompt_mode, origin_image, input_image, min_pixels=None, max_pixels=None):
    if prompt_mode in ["prompt_ocr", "prompt_table_html", "prompt_table_latex", "prompt_formula_latex"]:
        return response, False # 注意：为了保持返回签名一致，这里也返回一个元组

    cells = response
    try:
        # 使用 json_repair.loads 直接进行解析，它更强大
        cells = json_repair_loads(cells)
        
        # 确保修复后的结果是列表，否则视为失败
        if not isinstance(cells, list):
            raise TypeError(f"Repaired JSON is not a list, but {type(cells)}")

        cells = post_process_cells(
            origin_image, 
            cells,
            input_image.width,
            input_image.height,
            min_pixels=min_pixels,
            max_pixels=max_pixels
        )
        sanitize_cells_repeated_tokens(cells)
        return cells, False
    except Exception as e:
        # 如果连 json_repair 都失败了，说明输出格式问题很严重
        print(f"CRITICAL: JSON repair failed. Error: {e}, when using {prompt_mode}")
        
        # 触发旧的降级清理逻辑
        cleaner = OutputCleaner()
        response_clean = cleaner.clean_model_output(response) # 使用原始 response
        if isinstance(response_clean, list):
            response_clean = "\n\n".join([cell['text'] for cell in response_clean if 'text' in cell])
        return response_clean, True
