# v0.4.0 Release

English | [中文](release-v0.4.0.zh-CN.md)

`0.4.0` promotes the accepted v0.4 release candidate. It keeps Control as a
modular monolith while making migration readiness, scheduling ownership,
operational visibility, and engine provenance explicit.

## Highlights

- PostgreSQL startup migration is opt-in, with separate process health and
  schema readiness plus one checksum-aware migration runner.
- Model Profiles can carry optional certification metadata, and Agent
  provenance can participate in preflight without exposing endpoints or
  credentials.
- Prometheus metrics, capacity recommendations, audit summaries, and alert
  templates use bounded labels and remain operator controlled.
- Control transaction, scheduling, jobs, manifests, and workers ownership is
  explicit without changing HTTP/OpenAPI, database, state, manifest, output,
  or scheduling behavior.
- Agent event/log replay is serialized per spool and acknowledges records
  against the latest on-disk state, preserving records appended in flight.

## Compatibility

The only intentional Python import break is removal of
`ocr_platform.control.service`. Use the documented domain commands, queries,
and schemas for Python integrations. Existing console scripts, CLI arguments,
HTTP paths and fields, migration history through `0020`, status values,
manifest/output formats, and Parser compatibility imports remain supported.

## Validation

The release candidate passed the Python 3.10-3.12, PostgreSQL, package-install,
mock end-to-end, recovery, provenance, and observation gates. DotsOCR, MinerU,
and PaddleOCR-VL were rechecked with public fixtures; their previously
documented quality and runtime limitations remain, and their certification
states are unchanged.

Install the required profile and follow the [v0.4 upgrade guide](migration-v0.4.md):

```bash
python -m pip install 'ocrparser-platform[platform]==0.4.0'
```

The release wheel and `/source.json` must report the exact `v0.4.0` source
revision, `dirty=false`, and `release_build=true`.
