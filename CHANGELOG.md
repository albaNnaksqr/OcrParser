# Changelog

## Unreleased

## 0.2.0

- Declared the public repository as the single source-code mainline.
- Added strict parser configuration, neutral parser/platform contracts, engine
  capabilities, and a narrow engine context.
- Replaced parser method grafting with a façade composed from runtime, document,
  inference, output, and resume components.
- Changed the control default bind address to `127.0.0.1`; non-loopback binding
  now requires `OCR_PLATFORM_API_TOKEN`.
- Disabled Remote Admin and database-saved model API keys by default.
- Moved the browser UI bearer token from `localStorage` to `sessionStorage`.
- Expanded release checks for Python 3.10-3.12, wheel installation, documentation,
  PostgreSQL migrations/claims, and a mock distributed OCR walkthrough.

- Prepared the project for a public source snapshot.
- Added public configuration examples and open-source project metadata.
- Added a minimal GitHub Actions CI workflow.

## Initial Public Snapshot

- Modular OCR parser CLI for PDF-to-Markdown/JSON workflows.
- Optional FastAPI control UI and worker agent for distributed shared-storage
  jobs.
- Manifest/shard scheduling, recovery, and observability primitives.
