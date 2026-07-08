# Repository Guidelines

## Project Structure & Module Organization

This repository contains a modular OCR parser split across two peer packages. `ocr_parser/` owns the PDF parsing workflow: CLI argument handling, lifecycle management, page processing, table repair, metadata, resume support, and Markdown/JSON output writers under `ocr_parser/output/`. `ocr_parser/pipeline/` contains the higher-level document and page orchestration. `dots_ocr/` contains lower-level model inference, document/image utilities, S3 helpers, and data-index tooling inherited from the original Dots.OCR codebase. Top-level entry points are `ocr_parser_cli.py` and `run_ocr_pdf_modular.sh`. Runtime dependency pins live in `requirements.txt`.

## Build, Test, and Development Commands

- `python -m pip install -r requirements.txt`: install runtime dependencies for local development.
- `python ocr_parser_cli.py --input_file /path/file.pdf --output_dir ./output --ip 127.0.0.1 --port 8000`: run the modular parser against one PDF and a local OCR/vLLM-compatible service.
- `python ocr_parser_cli.py --input_dir /path/pdfs --output_dir ./output`: process all PDFs in a directory.
- `bash run_ocr_pdf_modular.sh`: run the scripted batch configuration; edit defaults carefully because it contains service, path, and credential-like values.
- `python -m compileall ocr_parser dots_ocr`: quick syntax/import-compilation check.

## Coding Style & Naming Conventions

Use Python 3.10+ and follow the existing style: 4-space indentation, type hints for public helpers, `snake_case` functions and modules, and `PascalCase` classes. Keep workflow code in `ocr_parser/` and reusable OCR utilities in `dots_ocr/`. Prefer `pathlib.Path` for filesystem paths, `async` APIs for model/network work, and explicit argparse flags matching existing `--long_option` names.

## Testing Guidelines

Tests live under `tests/`. Add or update tests when changing parsing behavior,
resume logic, output writers, platform scheduling, deployment tooling, or utility
functions. Use `pytest` conventions such as `tests/test_resume.py` and
`test_force_reprocess_ignores_cache()`. For changes that require an OCR service,
use a small public fixture PDF or mock the service boundary rather than relying
on private infrastructure.

## Commit & Pull Request Guidelines

The current history uses very short messages such as `README.md` and `first commit`; for new work, prefer concise imperative messages, for example `Add resume state tests` or `Document parser CLI flags`. Pull requests should describe the affected OCR path, list verification commands, note any required model service or optional dependency, and include sample output paths or screenshots when Markdown/layout rendering changes.

## Security & Configuration Tips

Do not commit real API keys, internal endpoints, or customer PDFs. Treat shell-script defaults and `dots_ocr/*s3*_config*.json` as environment-specific examples; prefer local `.env` files or runtime flags for secrets.

For multi-engine validation, keep downloaded models outside the repository and
document the hardware assumptions used for any benchmark or tuning result.
