#!/usr/bin/env python3
"""Check real-engine outputs against the public certification fixture contract."""

from __future__ import annotations

import argparse
import json
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence


SUCCESS_STATUSES = {"success", "success_fallback_text", "success_fallback_image"}


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


@dataclass(frozen=True)
class FixtureCheck:
    filename: str
    status: str
    markdown_path: str | None
    sidecar_path: str | None
    missing_required_fields: list[str] = field(default_factory=list)
    missing_table_cells: list[str] = field(default_factory=list)
    reading_order_ok: bool = False
    expected_pages: int = 0
    actual_pages: int | None = None
    fallback_used: bool | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "pass"


def _find_markdown(document_dir: Path, stem: str, engine: str) -> Path | None:
    preferred = document_dir / "native" / engine / f"{stem}.md"
    if preferred.is_file():
        return preferred
    matches = [path for path in document_dir.rglob(f"{stem}.md") if not path.name.startswith("page_")]
    return sorted(matches)[0] if matches else None


def _ordered(text: str, values: list[str]) -> bool:
    cursor = 0
    for value in values:
        token = normalize_text(value)
        index = text.find(token, cursor)
        if index < 0:
            return False
        cursor = index + len(token)
    return True


def check_fixture(
    expectation: dict[str, Any],
    *,
    output_dir: Path,
    engine: str,
    allow_fallback: bool,
) -> FixtureCheck:
    filename = str(expectation["filename"])
    stem = Path(filename).stem
    document_dir = output_dir / stem
    markdown_path = _find_markdown(document_dir, stem, engine)
    sidecar_path = document_dir / ".ocr_status.json"
    problems: list[str] = []
    if markdown_path is None:
        problems.append("combined_markdown_missing")
        markdown = ""
    else:
        markdown = normalize_text(markdown_path.read_text(encoding="utf-8", errors="replace"))
    missing_required = [
        value for value in expectation.get("required_fields", []) if normalize_text(str(value)) not in markdown
    ]
    missing_cells = [
        value for value in expectation.get("table_cells", []) if normalize_text(str(value)) not in markdown
    ]
    reading_order = _ordered(markdown, [str(value) for value in expectation.get("reading_order", [])])
    if missing_required:
        problems.append("required_fields_missing")
    if missing_cells:
        problems.append("table_cells_missing")
    if not reading_order:
        problems.append("reading_order_invalid")

    actual_pages: int | None = None
    fallback_used: bool | None = None
    if not sidecar_path.is_file():
        problems.append("sidecar_missing")
    else:
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            sidecar = {}
            problems.append("sidecar_unreadable")
        status = sidecar.get("status")
        if status not in SUCCESS_STATUSES:
            problems.append(f"document_status:{status or 'missing'}")
        try:
            actual_pages = int(sidecar.get("total_pages"))
        except (TypeError, ValueError):
            problems.append("total_pages_missing")
        if actual_pages is not None and actual_pages != int(expectation["pages"]):
            problems.append("page_count_mismatch")
        stages = sidecar.get("stages")
        if not isinstance(stages, list) or not stages:
            problems.append("stages_missing")
        fallback = sidecar.get("fallback")
        if not isinstance(fallback, dict):
            problems.append("fallback_metadata_missing")
        else:
            fallback_used = bool(fallback.get("used"))
            if fallback_used and not allow_fallback:
                problems.append("fallback_used")

    return FixtureCheck(
        filename=filename,
        status="pass" if not problems else "fail",
        markdown_path=str(markdown_path) if markdown_path else None,
        sidecar_path=str(sidecar_path) if sidecar_path.is_file() else None,
        missing_required_fields=missing_required,
        missing_table_cells=missing_cells,
        reading_order_ok=reading_order,
        expected_pages=int(expectation["pages"]),
        actual_pages=actual_pages,
        fallback_used=fallback_used,
        problems=problems,
    )


def check_outputs(
    expectations_path: Path,
    *,
    output_dir: Path,
    engine: str,
    allow_fallback: bool = False,
) -> dict[str, Any]:
    payload = json.loads(expectations_path.read_text(encoding="utf-8"))
    checks = [
        check_fixture(item, output_dir=output_dir, engine=engine, allow_fallback=allow_fallback)
        for item in payload.get("fixtures", [])
    ]
    return {
        "schema_version": 1,
        "engine": engine,
        "status": "pass" if checks and all(item.ok for item in checks) else "fail",
        "fixture_count": len(checks),
        "passed_fixture_count": sum(item.ok for item in checks),
        "fixtures": [asdict(item) | {"ok": item.ok} for item in checks],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate real OCR outputs against public fixture expectations.")
    parser.add_argument(
        "--expectations",
        type=Path,
        default=Path("tests/fixtures/public_pdfs/expected.json"),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--engine", choices=["dotsocr", "mineru", "paddleocr-vl"], required=True)
    parser.add_argument("--allow-fallback", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = check_outputs(
        args.expectations,
        output_dir=args.output_dir,
        engine=args.engine,
        allow_fallback=args.allow_fallback,
    )
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
