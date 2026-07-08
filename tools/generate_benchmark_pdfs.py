#!/usr/bin/env python3
"""Generate synthetic OCR benchmark PDFs.

The generated files are local fixtures for latency and throughput checks. They
avoid customer data while covering common document layouts that stress OCR
engines differently.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import fitz


A4 = fitz.paper_rect("a4")
LETTER = fitz.paper_rect("letter")
RECEIPT = fitz.Rect(0, 0, 260, 640)


@dataclass(frozen=True)
class Fixture:
    filename: str
    pages: int
    category: str
    purpose: str
    expected_stress: str


def insert_textbox(
    page: fitz.Page,
    rect: fitz.Rect,
    text: str,
    *,
    fontsize: float = 10,
    fontname: str = "helv",
    color: tuple[float, float, float] = (0, 0, 0),
    align: int = fitz.TEXT_ALIGN_LEFT,
) -> None:
    page.insert_textbox(
        rect,
        text,
        fontsize=fontsize,
        fontname=fontname,
        color=color,
        align=align,
    )


def draw_header(page: fitz.Page, title: str, page_no: int | None = None) -> None:
    page.draw_line((50, 58), (page.rect.width - 50, 58), color=(0.2, 0.2, 0.2), width=0.7)
    insert_textbox(page, fitz.Rect(50, 28, page.rect.width - 50, 52), title, fontsize=13)
    if page_no is not None:
        insert_textbox(
            page,
            fitz.Rect(page.rect.width - 120, page.rect.height - 38, page.rect.width - 50, page.rect.height - 20),
            f"Page {page_no}",
            fontsize=8,
            color=(0.35, 0.35, 0.35),
            align=fitz.TEXT_ALIGN_RIGHT,
        )


def draw_table(
    page: fitz.Page,
    x: float,
    y: float,
    col_widths: list[float],
    row_height: float,
    rows: list[list[str]],
    *,
    header_rows: int = 1,
    fontsize: float = 8,
    fontname: str = "helv",
) -> float:
    width = sum(col_widths)
    height = row_height * len(rows)
    page.draw_rect(fitz.Rect(x, y, x + width, y + height), color=(0, 0, 0), width=0.6)
    cursor_x = x
    for col_width in col_widths[:-1]:
        cursor_x += col_width
        page.draw_line((cursor_x, y), (cursor_x, y + height), color=(0, 0, 0), width=0.4)
    for idx in range(1, len(rows)):
        yy = y + idx * row_height
        page.draw_line((x, yy), (x + width, yy), color=(0, 0, 0), width=0.35)

    for row_idx, row in enumerate(rows):
        cursor_x = x
        if row_idx < header_rows:
            page.draw_rect(
                fitz.Rect(x, y + row_idx * row_height, x + width, y + (row_idx + 1) * row_height),
                color=None,
                fill=(0.9, 0.92, 0.95),
            )
        for col_idx, value in enumerate(row):
            cell = fitz.Rect(
                cursor_x + 3,
                y + row_idx * row_height + 3,
                cursor_x + col_widths[col_idx] - 3,
                y + (row_idx + 1) * row_height - 2,
            )
            insert_textbox(page, cell, value, fontsize=fontsize, fontname=fontname)
            cursor_x += col_widths[col_idx]
    return y + height


def save(doc: fitz.Document, path: Path) -> int:
    doc.save(path, garbage=4, deflate=True)
    page_count = doc.page_count
    doc.close()
    return page_count


def create_simple_text(path: Path) -> Fixture:
    doc = fitz.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    draw_header(page, "OCR Benchmark Memo")
    body = """Executive Summary
This one-page document is designed to measure plain text latency. It contains
short paragraphs, a compact list, dates, identifiers, and a small footer.

Scope
The parser should preserve headings, paragraph order, and simple bullet-like
lines without needing layout reconstruction.

Checklist
- Intake ID: OCR-BENCH-001
- Review date: 2026-05-19
- Owner: Synthetic Benchmark Team
- Expected output: readable Markdown with stable paragraph order

Notes
Short documents are useful for measuring fixed request overhead. They also help
separate service startup latency from page-level processing time."""
    insert_textbox(page, fitz.Rect(58, 86, A4.width - 58, A4.height - 90), body, fontsize=11)
    insert_textbox(page, fitz.Rect(58, A4.height - 58, A4.width - 58, A4.height - 40), "Confidentiality: synthetic fixture only", fontsize=8, color=(0.4, 0.4, 0.4))
    pages = save(doc, path)
    return Fixture(path.name, pages, "text", "Plain-text latency baseline", "Fixed overhead and paragraph ordering")


def create_invoice_table(path: Path) -> Fixture:
    doc = fitz.open()
    items = [
        ["SKU", "Description", "Qty", "Unit", "Amount"],
        ["OCR-101", "Document intake and routing", "3", "125.00", "375.00"],
        ["OCR-204", "Table extraction review", "8", "42.50", "340.00"],
        ["OCR-330", "Layout validation batch", "2", "310.00", "620.00"],
        ["OCR-412", "Markdown normalization", "5", "57.00", "285.00"],
        ["OCR-515", "Retry and timeout audit", "1", "190.00", "190.00"],
        ["", "", "", "Subtotal", "1,810.00"],
        ["", "", "", "Tax", "162.90"],
        ["", "", "", "Total", "1,972.90"],
    ]
    for i in range(2):
        page = doc.new_page(width=A4.width, height=A4.height)
        draw_header(page, f"Synthetic Services Invoice {1000 + i}", i + 1)
        insert_textbox(page, fitz.Rect(58, 82, 280, 130), "Bill To:\nExample Operations\n42 Parser Lane\nBenchmark City", fontsize=9)
        insert_textbox(page, fitz.Rect(350, 82, 540, 130), f"Invoice: INV-{1000 + i}\nDate: 2026-05-{19 + i:02d}\nTerms: Net 30", fontsize=9)
        y = draw_table(page, 58, 160, [70, 230, 50, 70, 80], 28, items, fontsize=7.6)
        insert_textbox(page, fitz.Rect(58, y + 24, 540, y + 100), "Payment memo: This table intentionally uses narrow columns and numeric values to test cell boundary detection.", fontsize=9)
    pages = save(doc, path)
    return Fixture(path.name, pages, "table", "Invoice-style tables with numeric cells", "Table structure, numeric alignment, repeated headers")


def create_two_column_report(path: Path) -> Fixture:
    doc = fitz.open()
    left = """The benchmark report uses two columns to exercise reading order. A
well-behaved parser should keep the left column before the right column on each
page and avoid mixing sentence fragments across columns.

The content includes recurring section labels, compact paragraphs, and a footer.
This helps reveal layout detectors that over-segment text blocks."""
    right = """The right column contains independent observations and short labels.
Latency should stay close to the plain-text baseline unless the engine performs
expensive layout analysis for every block.

Metric A: 94.2
Metric B: 18.7
Metric C: stable"""
    for page_no in range(1, 4):
        page = doc.new_page(width=LETTER.width, height=LETTER.height)
        draw_header(page, "Two Column Operational Report", page_no)
        insert_textbox(page, fitz.Rect(54, 88, 290, 700), f"Section {page_no}.1\n\n{left}\n\nRepeated note {page_no}: column order matters.", fontsize=9.2)
        page.draw_line((306, 84), (306, 704), color=(0.65, 0.65, 0.65), width=0.4)
        insert_textbox(page, fitz.Rect(326, 88, 558, 700), f"Section {page_no}.2\n\n{right}\n\nObservation {page_no}: preserve labels and values.", fontsize=9.2)
    pages = save(doc, path)
    return Fixture(path.name, pages, "layout", "Two-column reading-order benchmark", "Column ordering and block segmentation")


def create_mixed_forms(path: Path) -> Fixture:
    doc = fitz.open()
    for page_no in range(1, 6):
        page = doc.new_page(width=A4.width, height=A4.height)
        draw_header(page, "Mixed Layout Intake Form", page_no)
        insert_textbox(page, fitz.Rect(58, 82, 540, 116), f"Case ID: MIX-{page_no:03d}    Priority: Normal    Submitted: 2026-05-{18 + page_no:02d}", fontsize=9)
        y = 135
        fields = [
            ("Applicant", f"Benchmark User {page_no}"),
            ("Department", "Document AI Evaluation"),
            ("Review Window", "09:00-17:00"),
            ("Escalation", "No"),
        ]
        for label, value in fields:
            page.draw_rect(fitz.Rect(58, y, 220, y + 24), color=(0.1, 0.1, 0.1), width=0.4)
            page.draw_rect(fitz.Rect(220, y, 540, y + 24), color=(0.1, 0.1, 0.1), width=0.4)
            insert_textbox(page, fitz.Rect(64, y + 5, 214, y + 20), label, fontsize=8)
            insert_textbox(page, fitz.Rect(226, y + 5, 534, y + 20), value, fontsize=8)
            y += 24
        rows = [["Item", "Status", "Reviewer"], ["Identity check", "Complete", "A. Liu"], ["Document quality", "Pending", "B. Chen"], ["Table review", "Complete", "C. Smith"]]
        y = draw_table(page, 58, y + 28, [170, 150, 150], 27, rows, fontsize=8)
        page.draw_rect(fitz.Rect(58, y + 30, 248, y + 160), color=(0.2, 0.2, 0.2), width=0.7)
        insert_textbox(page, fitz.Rect(78, y + 78, 228, y + 112), "FIGURE AREA\nsynthetic chart placeholder", fontsize=9, align=fitz.TEXT_ALIGN_CENTER)
        page.draw_rect(fitz.Rect(285, y + 38, 299, y + 52), color=(0, 0, 0), width=0.8)
        page.draw_line((287, y + 45), (292, y + 50), color=(0, 0, 0), width=1.1)
        page.draw_line((292, y + 50), (300, y + 39), color=(0, 0, 0), width=1.1)
        insert_textbox(page, fitz.Rect(310, y + 34, 540, y + 56), "Verified against synthetic source", fontsize=8)
        insert_textbox(page, fitz.Rect(285, y + 76, 540, y + 150), "Free text notes: mixed pages combine key-value regions, tables, checkbox marks, captions, and body text.", fontsize=8)
    pages = save(doc, path)
    return Fixture(path.name, pages, "mixed", "Mixed form, table, checkbox, and figure regions", "Layout detector calls and heterogeneous blocks")


def create_scanned_like(path: Path) -> Fixture:
    source = fitz.open()
    for page_no in range(1, 3):
        page = source.new_page(width=A4.width, height=A4.height)
        draw_header(page, "Rasterized Scan Source", page_no)
        text = f"""Rasterized Page {page_no}
This page is rendered to an image and re-embedded into a PDF. OCR engines should
treat it like a scanned document instead of extracting native PDF text.

Fields:
Name: Synthetic Scan User {page_no}
Batch: SCAN-{page_no:03d}
Quality: medium

The page includes straight lines and light gray regions to simulate office scans."""
        insert_textbox(page, fitz.Rect(70, 95, 520, 420), text, fontsize=11)
        page.draw_rect(fitz.Rect(70, 450, 520, 575), color=(0.2, 0.2, 0.2), fill=(0.93, 0.93, 0.9), width=0.7)
        insert_textbox(page, fitz.Rect(90, 500, 500, 535), "Low contrast annotation band", fontsize=12, color=(0.25, 0.25, 0.25), align=fitz.TEXT_ALIGN_CENTER)

    scanned = fitz.open()
    for source_page in source:
        pix = source_page.get_pixmap(dpi=150, colorspace=fitz.csGRAY)
        page = scanned.new_page(width=A4.width, height=A4.height)
        page.insert_image(page.rect, pixmap=pix)
    source.close()
    pages = save(scanned, path)
    return Fixture(path.name, pages, "scan", "Raster-image PDF that mimics scanned pages", "Image OCR path and render cost")


def create_long_document(path: Path) -> Fixture:
    doc = fitz.open()
    para = """This synthetic long document is intended for concurrency and resume
testing. Each page has similar structure so throughput measurements are easier
to compare across engines and runs. The content is deliberately ordinary: short
paragraphs, compact metrics, and a small table."""
    rows = [["Metric", "Value", "Status"], ["Pages", "20", "Expected"], ["Retry", "0", "Target"], ["Output", "Markdown", "Target"]]
    for page_no in range(1, 21):
        page = doc.new_page(width=A4.width, height=A4.height)
        draw_header(page, "Long Document Throughput Fixture", page_no)
        insert_textbox(page, fitz.Rect(58, 88, 540, 300), f"Chapter {page_no}\n\n{para}\n\nRun marker: LONG-{page_no:02d}", fontsize=10)
        draw_table(page, 58, 330, [150, 120, 160], 26, rows, fontsize=8)
        insert_textbox(page, fitz.Rect(58, 460, 540, 660), f"Additional notes for page {page_no}: This repeated section helps detect page-level variance, queue buildup, and slow writes.", fontsize=9)
    pages = save(doc, path)
    return Fixture(path.name, pages, "long", "Twenty-page throughput and resume benchmark", "Queueing behavior, page concurrency, cache/resume behavior")


def create_bilingual(path: Path) -> Fixture:
    doc = fitz.open()
    zh_font = "china-s"
    zh_lines = [
        "\u9879\u76ee\u6982\u8ff0 / Project Overview",
        "\u8fd9\u662f\u4e00\u4efd\u4e2d\u82f1\u6df7\u6392\u7684 OCR \u6d4b\u8bd5\u6587\u6863\u3002",
        "\u5b83\u7528\u4e8e\u68c0\u67e5\u6a21\u578b\u5bf9\u4e2d\u6587\u6bb5\u843d\u3001\u82f1\u6587\u672f\u8bed\u548c\u6570\u5b57\u7684\u5904\u7406\u3002",
        "English note: preserve mixed-language order and punctuation.",
    ]
    rows = [
        ["\u5b57\u6bb5 / Field", "\u503c / Value", "\u5907\u6ce8 / Note"],
        ["\u5ba2\u6237\u7f16\u53f7", "CN-2026-001", "\u5408\u6210\u6570\u636e"],
        ["\u6587\u6863\u7c7b\u578b", "Benchmark PDF", "\u4e2d\u82f1\u6df7\u6392"],
        ["Latency target", "< 60s/page", "\u6839\u636e\u5f15\u64ce\u8c03\u6574"],
    ]
    for page_no in range(1, 3):
        page = doc.new_page(width=A4.width, height=A4.height)
        draw_header(page, "Bilingual OCR Fixture", page_no)
        insert_textbox(page, fitz.Rect(58, 90, 540, 210), "\n".join(zh_lines), fontsize=11, fontname=zh_font)
        draw_table(page, 58, 245, [160, 150, 180], 34, rows, fontsize=8.5, fontname=zh_font)
        insert_textbox(page, fitz.Rect(58, 420, 540, 620), "\u5904\u7406\u5efa\u8bae\uff1a\u5728\u6027\u80fd\u6d4b\u8bd5\u4e2d\u5355\u72ec\u8bb0\u5f55\u8fd9\u7c7b\u6587\u6863\u7684\u8bc6\u522b\u8017\u65f6\u548c\u8bed\u79cd\u8f93\u51fa\u7a33\u5b9a\u6027\u3002", fontsize=10, fontname=zh_font)
    pages = save(doc, path)
    return Fixture(path.name, pages, "bilingual", "Chinese and English mixed-language benchmark", "CJK text handling and mixed punctuation")


def create_receipt(path: Path) -> Fixture:
    doc = fitz.open()
    page = doc.new_page(width=RECEIPT.width, height=RECEIPT.height)
    insert_textbox(page, fitz.Rect(18, 25, 242, 58), "SYNTHETIC RECEIPT", fontsize=14, align=fitz.TEXT_ALIGN_CENTER)
    insert_textbox(page, fitz.Rect(18, 62, 242, 100), "Store: OCR Bench Mart\nDate: 2026-05-19 14:22\nTerminal: R-07", fontsize=8)
    rows = [["Item", "Qty", "Total"], ["Notebook", "2", "8.40"], ["Archive Box", "1", "12.99"], ["Label Pack", "3", "5.97"], ["Marker", "4", "6.80"], ["Subtotal", "", "34.16"], ["Tax", "", "3.08"], ["Total", "", "37.24"]]
    draw_table(page, 18, 120, [118, 42, 62], 26, rows, fontsize=7.2)
    insert_textbox(page, fitz.Rect(18, 365, 242, 450), "Payment: CARD\nAuth: 123456\nReference: RECEIPT-BENCH-001", fontsize=8)
    page.draw_rect(fitz.Rect(52, 480, 208, 555), color=(0, 0, 0), width=0.8)
    insert_textbox(page, fitz.Rect(62, 508, 198, 532), "BARCODE AREA", fontsize=9, align=fitz.TEXT_ALIGN_CENTER)
    insert_textbox(page, fitz.Rect(18, 585, 242, 620), "Thank you for testing OCR throughput.", fontsize=8, align=fitz.TEXT_ALIGN_CENTER)
    pages = save(doc, path)
    return Fixture(path.name, pages, "narrow", "Receipt-like narrow page", "Small fonts and non-A4 page geometry")


def build_fixtures(output_dir: Path) -> list[Fixture]:
    output_dir.mkdir(parents=True, exist_ok=True)
    builders = [
        ("01_text_simple_1p.pdf", create_simple_text),
        ("02_invoice_table_2p.pdf", create_invoice_table),
        ("03_two_column_report_3p.pdf", create_two_column_report),
        ("04_mixed_forms_5p.pdf", create_mixed_forms),
        ("05_scanned_like_2p.pdf", create_scanned_like),
        ("06_long_document_20p.pdf", create_long_document),
        ("07_bilingual_cn_en_2p.pdf", create_bilingual),
        ("08_receipt_narrow_1p.pdf", create_receipt),
    ]
    fixtures: list[Fixture] = []
    for filename, builder in builders:
        fixtures.append(builder(output_dir / filename))
    return fixtures


def write_manifest(output_dir: Path, fixtures: Iterable[Fixture]) -> None:
    fixture_dicts = [asdict(fixture) for fixture in fixtures]
    (output_dir / "manifest.json").write_text(json.dumps(fixture_dicts, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# OCR Benchmark PDFs",
        "",
        "Synthetic local fixtures for engine latency, throughput, and layout checks.",
        "",
        "| File | Pages | Category | Purpose | Expected stress |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for fixture in fixture_dicts:
        lines.append(
            f"| `{fixture['filename']}` | {fixture['pages']} | {fixture['category']} | {fixture['purpose']} | {fixture['expected_stress']} |"
        )
    lines.extend(
        [
            "",
            "Recommended first pass:",
            "",
            "- Run every engine with page concurrency 1 on all files.",
            "- Increase DotsOCR concurrency across 2, 4, 8, and 16 only after the baseline is stable.",
            "- Keep MinerU and PaddleOCR-VL at 1 or 2 concurrent pages unless the single-instance queue stays healthy.",
        ]
    )
    (output_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate synthetic OCR benchmark PDFs.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/benchmark_pdfs"),
        help="Directory where benchmark PDFs and manifest files are written.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    fixtures = build_fixtures(args.output_dir)
    write_manifest(args.output_dir, fixtures)
    total_pages = sum(fixture.pages for fixture in fixtures)
    print(f"Wrote {len(fixtures)} PDFs ({total_pages} pages) to {args.output_dir}")


if __name__ == "__main__":
    main()
