# RFC: OcrParser v0.4 Operational Maturity

English | [中文](rfc-v0.4.zh-CN.md)

Status: proposed after v0.3.1 stabilization. This RFC is not implemented by the
v0.3.1 maintenance release.

Related documents:

- [v0.4.0 Execution Plan](rfc-v0.4-execution-plan.md)
- [Control Internals RFC](rfc-v0.4-control-internals.md)
- [Control Module Map](control-module-map.md)

## Decision Summary

v0.4 prioritizes explicit operations, certified engine profiles, alerts,
capacity planning, and auditability. It does not continue decomposition for its
own sake and does not rewrite OCR, layout, table, or Markdown algorithms.

## Migration Policy

- Add `OCR_PLATFORM_AUTO_MIGRATE`. Production PostgreSQL deployments default to
  disabled and must run `ocr-platform-migrate plan|apply|verify` before Control
  startup.
- Development helpers explicitly set auto-migration on. A direct SQLite
  developer launch may keep the convenience behavior only when production
  guards are not enabled.
- Control readiness fails with actionable migration instructions when the
  schema is not current. It never silently applies migrations when the
  production default is disabled.
- Preserve migration history and checksums; v0.4 does not adopt Alembic.

## Compatibility Policy

- Remove the `ocr_platform.control.service` compatibility façade in v0.4.
  Integrations must import from the relevant Control domain before upgrading.
- Continue accepting and emitting `success_fallback_text` and
  `success_fallback_image` throughout v0.4. New consumers must use structured
  `stages` and `fallback`; removal cannot occur before v0.5 and requires a
  separate compatibility decision.
- Preserve console scripts, HTTP paths, Job/Shard state, manifest JSONL, output
  formats, and parser top-level façades unless a later RFC explicitly changes
  them.

## Certified Engine Profiles

- Bind a model profile to parser revision, model revision, runtime image
  digest/source revision, layout revision where applicable, fixture-set digest,
  and certification status.
- Job preflight rejects a changed or missing provenance field when a profile is
  configured to require certification. Operators may use an explicit,
  auditable risk-acceptance mode for `Verified` profiles.
- Profile secrets remain environment references. Certification metadata never
  contains keys or internal endpoint credentials.

## Observability And Capacity

- Ship bounded-label alert examples for stage failure rate, fallback rate,
  worker heartbeat age, stale leases, spool backlog, migration drift, and
  artifact audit failures.
- Add read-only capacity output using worker slots, queue depth, recent page
  duration, API concurrency, and observed resource budgets. It is advisory and
  does not autoscale workers or model services.
- Retain job/shard/attempt audit evidence long enough to explain reclaim,
  replay, stop/resume, and output provenance without logging document content.

## Entry Gate

Documentation and design review may start immediately. The seven-day
observation window starts from the v0.3.1 release publication time,
2026-07-26 14:51:07+08:00. Implementation starts no earlier than
2026-08-02 14:51:07+08:00, after seven full days without a new P0/P1 stability
defect and after the v0.3.1 long-running, multi-cycle validation has been
reviewed under the recorded risk acceptance. Any unresolved data-loss,
duplicate-claim, migration, replay, shutdown, or resource-leak issue remains a
v0.3 maintenance priority instead.
