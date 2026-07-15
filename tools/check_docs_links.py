#!/usr/bin/env python3
"""Check local Markdown links without depending on network availability."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
SKIPPED_SCHEMES = ("http://", "https://", "mailto:", "#")


def broken_local_links(root: Path) -> list[str]:
    broken: list[str] = []
    markdown_files = [root / "README.md", root / "README.zh-CN.md"]
    markdown_files.extend(sorted((root / "docs").rglob("*.md")))

    for source in markdown_files:
        text = source.read_text(encoding="utf-8")
        for raw_target in LINK_RE.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(SKIPPED_SCHEMES):
                continue
            path_part = unquote(target.split("#", 1)[0])
            if not path_part:
                continue
            resolved = (source.parent / path_part).resolve()
            if not resolved.exists():
                broken.append(f"{source.relative_to(root)} -> {target}")
    return broken


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    broken = broken_local_links(root)
    if broken:
        print("Broken local documentation links:", file=sys.stderr)
        for item in broken:
            print(f"- {item}", file=sys.stderr)
        return 1
    print("Local documentation links are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
