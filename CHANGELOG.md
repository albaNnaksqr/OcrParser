# Changelog

## Unreleased

## 0.3.1 - 2026-07-22

- Fixed three recovery classes without changing public APIs or data formats:
  assigned jobs now resume remaining static shards, shard update spool/replay is
  durable and terminal-state monotonic across Control outages, and lease
  renewal is limited to actively running jobs so stale work can be reclaimed.
- Declared Beautiful Soup as a base Parser dependency after real PaddleOCR-VL
  multi-page validation exposed the missing table-merge dependency.
- Added an auditable stability-soak runner with release/source/migration/claim
  gates, rotating input modes, fault hooks, resource sampling, output audits,
  and machine-readable reports that never persist runtime secrets.
- Expanded the generated public engine-certification set with invoice-table and
  mixed-layout PDFs plus required-field, reading-order, and table-cell checks.
- Completed a sanitized three-cycle Spark preflight: 300/300 documents and
  30/30 shards completed with lease reclaim, a 60-second Control outage and
  spool replay, and same-server restart recovery. The restart-based two-lease
  assertion exceeded its threshold by 1.524 seconds; the lease-eligibility
  basis passed and the discrepancy remains explicitly recorded.
- Revalidated 50 public pages per engine. DotsOCR and MinerU passed 3/4 quality
  fixtures; PaddleOCR-VL passed integration after the dependency fix but only
  1/4 quality fixtures. All three remain **Verified**, not **Certified**, with
  their provenance and quality limitations recorded.
- Updated the ARM64 PaddleOCR-VL recipe to build `sglang-kernel==0.4.4` from the
  pinned SGLang source and document its compute-capability and FlashInfer
  compatibility checks. No immutable repository digest was produced, so the
  deployment remains limited and Verified.
- Documented the v0.4 operational-maturity decisions without changing v0.3
  runtime APIs.

## 0.3.0 - 2026-07-17

- Split the default Parser installation from `platform`, `s3`, `layout`,
  `full`, and `dev` extras; retained all console-script names with actionable
  missing-extra errors and added empty-environment installation coverage.
- Included the runtime JSON configuration files required by the packaged
  `dots_ocr.data_index` helpers.
- Added structured engine stage and fallback contracts to page/file events,
  status sidecars, and artifact metadata while retaining legacy fallback page
  statuses for compatibility.
- Distinguished normal MinerU/Paddle two-stage completion from real degraded
  paths and added bounded-cardinality stage/fallback metrics.
- Added one checksum-aware `MigrationRunner`, PostgreSQL advisory locking,
  migration `0019`, and the `ocr-platform-migrate status|plan|apply|verify`
  command shared by startup, diagnostics, deployment tooling, and CI.
- Split the Control monolith into jobs, workers, manifests, model profiles,
  remote administration, and diagnostics domains while preserving HTTP and
  OpenAPI behavior and a one-release Python import compatibility façade.
- Added a composed `AgentRuntime` and supervisor for heartbeat, job polling,
  scan, shard execution, manifest integrity, and spool/replay lanes with one
  cancellation and signal boundary.
- Split the dependency-free Control UI into CSS and native ES modules for API,
  auth, state, jobs, workers, profiles, diagnostics, remote administration, and
  the application entrypoint; all assets remain wheel package data.
- Embedded source revision, UTC build timestamp, and dirty state in every wheel;
  `/source.json` consumes this immutable provenance and release verification
  rejects dirty or tag-mismatched wheels.
- Revalidated the `v0.3.0rc1` parser against DotsOCR, a digest-pinned MinerU
  vLLM deployment, and PaddleOCR-VL plus PP-DocLayout; all tested flows emitted
  successful stages with `fallback.used=false`. Paddle narrow-receipt table
  quality and reproducible runtime packaging remain documented deployment
  limitations rather than release blockers.

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
