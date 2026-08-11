#!/usr/bin/env python3
"""Verify that a clean Platform install is completely and exactly constrained."""

from __future__ import annotations

import argparse
import importlib.metadata
import re
from pathlib import Path
from typing import Sequence


IGNORED_BOOTSTRAP_DISTRIBUTIONS = {
    "ocrparser-platform",
    "pip",
    "setuptools",
    "wheel",
}
PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$")


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def load_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN_RE.fullmatch(line)
        if match is None:
            raise ValueError(f"{path}:{line_number}: expected an exact name==version pin")
        name = canonical_name(match.group(1))
        if name in pins:
            raise ValueError(f"{path}:{line_number}: duplicate pin for {name}")
        pins[name] = match.group(2)
    if not pins:
        raise ValueError(f"{path}: no dependency pins found")
    return pins


def installed_distributions() -> dict[str, str]:
    return {
        canonical_name(distribution.metadata["Name"]): distribution.version
        for distribution in importlib.metadata.distributions()
        if distribution.metadata.get("Name")
    }


def verify(constraints_path: Path) -> list[str]:
    pins = load_pins(constraints_path)
    installed = installed_distributions()
    errors: list[str] = []
    for name, version in sorted(installed.items()):
        if name in IGNORED_BOOTSTRAP_DISTRIBUTIONS:
            continue
        expected = pins.get(name)
        if expected is None:
            errors.append(f"installed dependency is not constrained: {name}=={version}")
        elif expected != version:
            errors.append(
                f"installed dependency does not match constraint: "
                f"{name}=={version} (expected {expected})"
            )
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("constraints", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    errors = verify(args.constraints)
    if errors:
        for error in errors:
            print(error)
        return 1
    print("Platform installation is fully constrained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
