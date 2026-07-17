from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from ocr_platform.optional import PLATFORM_MODULES, require_extra


DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocr-platform-migrate",
        description="Plan, apply, and verify OCR Platform SQL migrations.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "plan", "apply", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument(
            "--database-url",
            default=os.environ.get("OCR_PLATFORM_DATABASE_URL"),
            help="SQLAlchemy database URL. Defaults to OCR_PLATFORM_DATABASE_URL.",
        )
        subparser.add_argument(
            "--migrations-dir",
            default=str(DEFAULT_MIGRATIONS_DIR),
            help="Directory containing ordered *.sql migration files.",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    require_extra("platform", PLATFORM_MODULES)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or OCR_PLATFORM_DATABASE_URL is required")

    from .database import create_session_factory
    from .migration import MigrationRunner

    session_factory, engine = create_session_factory(args.database_url)
    del session_factory
    runner = MigrationRunner(engine, migrations_dir=Path(args.migrations_dir))
    try:
        if args.command == "status":
            payload = runner.status()
            exit_code = 0 if payload["is_current"] else 1
        elif args.command == "plan":
            payload = {"plan": runner.plan()}
            exit_code = 0
        elif args.command == "apply":
            payload = {
                "results": runner.apply(),
                "status": runner.status(),
            }
            exit_code = 0 if payload["status"]["is_current"] else 1
        else:
            payload = runner.verify()
            exit_code = 0 if payload["verified"] else 1
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return exit_code
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
