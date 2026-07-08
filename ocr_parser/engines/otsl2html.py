"""OTSL → HTML table converter.

Ported verbatim from mineru-vl-utils v0.2.6
(mineru_vl_utils/post_process/otsl2html.py).
"""
from __future__ import annotations

import html
import itertools
import re
from typing import Any, Dict, List

from pydantic import BaseModel, computed_field, model_validator

OTSL_NL = "<nl>"
OTSL_FCEL = "<fcel>"
OTSL_ECEL = "<ecel>"
OTSL_LCEL = "<lcel>"
OTSL_UCEL = "<ucel>"
OTSL_XCEL = "<xcel>"

_OTSL_TOKENS = [OTSL_NL, OTSL_FCEL, OTSL_ECEL, OTSL_LCEL, OTSL_UCEL, OTSL_XCEL]
_TOKEN_RE = re.compile(r"(" + "|".join(re.escape(t) for t in _OTSL_TOKENS) + r")")


class TableCell(BaseModel):
    row_span: int = 1
    col_span: int = 1
    start_row_offset_idx: int
    end_row_offset_idx: int
    start_col_offset_idx: int
    end_col_offset_idx: int
    text: str
    column_header: bool = False
    row_header: bool = False
    row_section: bool = False

    @model_validator(mode="before")
    @classmethod
    def from_dict_format(cls, data: Any) -> Any:
        if isinstance(data, Dict):
            if "text" in data:
                return data
            text = data["bbox"].get("token", "")
            if not len(text):
                text_cells = data.pop("text_cell_bboxes", None)
                if text_cells:
                    for el in text_cells:
                        text += el["token"] + " "
                text = text.strip()
            data["text"] = text
        return data


class TableData(BaseModel):
    table_cells: List[TableCell] = []
    num_rows: int = 0
    num_cols: int = 0

    @computed_field
    @property
    def grid(self) -> List[List[TableCell]]:
        table_data = [
            [
                TableCell(
                    text="",
                    start_row_offset_idx=i,
                    end_row_offset_idx=i + 1,
                    start_col_offset_idx=j,
                    end_col_offset_idx=j + 1,
                )
                for j in range(self.num_cols)
            ]
            for i in range(self.num_rows)
        ]
        for cell in self.table_cells:
            for i in range(
                min(cell.start_row_offset_idx, self.num_rows),
                min(cell.end_row_offset_idx, self.num_rows),
            ):
                for j in range(
                    min(cell.start_col_offset_idx, self.num_cols),
                    min(cell.end_col_offset_idx, self.num_cols),
                ):
                    table_data[i][j] = cell
        return table_data


def _otsl_extract_tokens_and_text(s: str):
    tokens = re.findall(_TOKEN_RE, s)
    text_parts = re.split(_TOKEN_RE, s)
    text_parts = [p for p in text_parts if p.strip()]
    return tokens, text_parts


def _otsl_parse_texts(texts, tokens):
    split_row_tokens = [list(y) for x, y in itertools.groupby(tokens, lambda z: z == OTSL_NL) if not x]
    table_cells = []
    r_idx = 0
    c_idx = 0

    if split_row_tokens:
        max_cols = max(len(row) for row in split_row_tokens)
        for row in split_row_tokens:
            while len(row) < max_cols:
                row.append(OTSL_ECEL)

        new_texts = []
        text_idx = 0
        for row in split_row_tokens:
            for token in row:
                new_texts.append(token)
                if text_idx < len(texts) and texts[text_idx] == token:
                    text_idx += 1
                    if text_idx < len(texts) and texts[text_idx] not in _OTSL_TOKENS:
                        new_texts.append(texts[text_idx])
                        text_idx += 1
            new_texts.append(OTSL_NL)
            if text_idx < len(texts) and texts[text_idx] == OTSL_NL:
                text_idx += 1
        texts = new_texts

    def count_right(tok_rows, c, r, which):
        span = 0
        ci = c
        while tok_rows[r][ci] in which:
            ci += 1
            span += 1
            if ci >= len(tok_rows[r]):
                return span
        return span

    def count_down(tok_rows, c, r, which):
        span = 0
        ri = r
        while tok_rows[ri][c] in which:
            ri += 1
            span += 1
            if ri >= len(tok_rows):
                return span
        return span

    for i, text in enumerate(texts):
        cell_text = ""
        if text in (OTSL_FCEL, OTSL_ECEL):
            row_span = 1
            col_span = 1
            right_offset = 1
            if text != OTSL_ECEL and (texts[i + 1] not in _OTSL_TOKENS):
                cell_text = texts[i + 1]
                right_offset = 2

            next_right = texts[i + right_offset] if i + right_offset < len(texts) else ""
            next_bottom = ""
            if r_idx + 1 < len(split_row_tokens) and c_idx < len(split_row_tokens[r_idx + 1]):
                next_bottom = split_row_tokens[r_idx + 1][c_idx]

            if next_right in (OTSL_LCEL, OTSL_XCEL):
                col_span += count_right(split_row_tokens, c_idx + 1, r_idx, [OTSL_LCEL, OTSL_XCEL])
            if next_bottom in (OTSL_UCEL, OTSL_XCEL):
                row_span += count_down(split_row_tokens, c_idx, r_idx + 1, [OTSL_UCEL, OTSL_XCEL])

            table_cells.append(
                TableCell(
                    text=cell_text.strip(),
                    row_span=row_span,
                    col_span=col_span,
                    start_row_offset_idx=r_idx,
                    end_row_offset_idx=r_idx + row_span,
                    start_col_offset_idx=c_idx,
                    end_col_offset_idx=c_idx + col_span,
                )
            )
        if text in (OTSL_FCEL, OTSL_ECEL, OTSL_LCEL, OTSL_UCEL, OTSL_XCEL):
            c_idx += 1
        if text == OTSL_NL:
            r_idx += 1
            c_idx = 0

    return table_cells, split_row_tokens


def _export_to_html(table_data: TableData) -> str:
    if not table_data.table_cells:
        return ""
    grid = table_data.grid
    parts = []
    for i in range(table_data.num_rows):
        parts.append("<tr>")
        for j in range(table_data.num_cols):
            cell = grid[i][j]
            if cell.start_row_offset_idx != i or cell.start_col_offset_idx != j:
                continue
            content = html.escape(cell.text.strip())
            tag = "th" if cell.column_header else "td"
            attrs = ""
            if cell.row_span > 1:
                attrs += f' rowspan="{cell.row_span}"'
            if cell.col_span > 1:
                attrs += f' colspan="{cell.col_span}"'
            parts.append(f"<{tag}{attrs}>{content}</{tag}>")
        parts.append("</tr>")
    return f"<table>{''.join(parts)}</table>"


def convert_otsl_to_html(otsl_content: str) -> str:
    """Convert OTSL table markup to an HTML <table> string."""
    if otsl_content.startswith("<table") and otsl_content.endswith("</table>"):
        return otsl_content

    tokens, mixed_texts = _otsl_extract_tokens_and_text(otsl_content)
    table_cells, split_row_tokens = _otsl_parse_texts(mixed_texts, tokens)
    table_data = TableData(
        num_rows=len(split_row_tokens),
        num_cols=(max(len(r) for r in split_row_tokens) if split_row_tokens else 0),
        table_cells=table_cells,
    )
    return _export_to_html(table_data)
