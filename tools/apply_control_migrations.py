from __future__ import annotations

import argparse
import os
from pathlib import Path

from ocr_platform.control.database import (
    MIGRATIONS_DIR,
    apply_schema_migrations,
    create_session_factory,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply OCR Platform control SQL migrations in filename order."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("OCR_PLATFORM_DATABASE_URL"),
        help="SQLAlchemy database URL. Defaults to OCR_PLATFORM_DATABASE_URL.",
    )
    parser.add_argument(
        "--migrations-dir",
        default=str(MIGRATIONS_DIR),
        help="Directory containing ordered *.sql migration files.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or OCR_PLATFORM_DATABASE_URL is required")
    session_factory, engine = create_session_factory(args.database_url)
    del session_factory
    results = apply_schema_migrations(
        engine,
        migrations_dir=Path(args.migrations_dir),
    )
    for result in results:
        print(f"{result['status']}: {result['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
