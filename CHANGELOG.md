# Changelog

## Unreleased

## 0.2.1

- Added an engineering third-party license inventory, complete bundled license
  texts, source attribution, and wheel license-file verification.
- Added a public GNU AGPLv3 corresponding-source offer, complete license route,
  Control UI legal notice, and exact-version deployment guidance for PyMuPDF.
- Added dated MinerU and PaddleOCR-VL engine certification evidence from real
  Spark deployments, including immutable model revisions and known limitations.
- Replaced the generic MinerU SGLang launcher with the validated vLLM and
  `MinerULogitsProcessor` deployment path.
- Clarified that real-model certification is a deployment gate rather than a
  requirement for building or publishing a GitHub release.

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
