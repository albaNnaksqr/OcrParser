# v0.3.1 Stability Maintenance Release

English | [中文](release-v0.3.1.zh-CN.md)

v0.3.1 is a focused recovery, packaging, and deployment-evidence update. It
does not change the CLI, HTTP API, database schema or migration history,
manifest wire format, output layout, job/shard status vocabulary, or Python
compatibility facade.

## Recovery Changes

- A job already assigned to a worker can resume and claim its remaining static
  shards instead of stopping after the first shard.
- Worker shard-update records are written durably, replayed idempotently, and
  cannot move a terminal shard back to a non-terminal state after a temporary
  Control outage.
- Work-lease renewal is scoped to actively running jobs. A stopped or otherwise
  inactive job cannot keep stale scan/shard work leased indefinitely.
- Same-server registration now fences the worker's previous running shard and
  current attempt together with previous running scan work, clearing owner/lease state so
  it can be reclaimed. The claim path prioritizes stale/retrying work over
  ordinary pending shards, preventing a backlog from starving recovery.

The base installation also declares `beautifulsoup4>=4.12,<5`. PaddleOCR-VL
multi-page table merging imports this dependency; the missing declaration was
an installation blocker, not an OCR algorithm change.

## Stability Evidence

An initial isolated preflight exposed same-server stale-reclaim starvation.
The registration fence and stale/retrying claim priority described above fixed
that recovery path without changing a public interface. A fresh preflight then
passed reclaim, replay, migration, output-audit, and cleanup checks.

The frozen candidate subsequently completed a long-running, multi-cycle
isolated validation across the supported input modes. Core scheduling,
recovery, spool replay, terminal-state, manifest, and output-integrity checks
passed without a product P0/P1 finding.

Review found that the validation tool did not enforce the configured
full-duration window after all cycles completed and overreacted to
low-baseline resource fluctuations. Both tool behaviors are now corrected and
covered by regression tests. Based on the completed evidence, the project
accepted the remaining validation risk rather than repeating the complete long
run. This release therefore does not claim completion of a strict
full-duration soak. Detailed reports are retained internally outside the
repository and contain no runtime credentials.

## Real-engine Status

All three engines remain **Verified**, not **Certified**:

- DotsOCR completed the integration checks, but the managed service did not
  expose immutable model or runtime provenance.
- MinerU completed integration and task-owned recovery checks, but remaining
  reading-order quality and immutable runtime-packaging limitations prevent
  certification.
- PaddleOCR-VL completed integration and task-owned recovery checks after the
  base-dependency fix, but quality, immutable image provenance, and the
  documented FlashInfer prerequisite remain certification limits.

See [Engine Certification](engine-certification.md) for the detailed fixture
matrix, revisions, digests, and limitations. Model-quality misses are recorded
as limits; they are not represented as Parser certification successes.

## Release Gate

Before tagging, build a clean wheel from the final commit and verify:

- Python 3.10, 3.11, and 3.12 tests and GitHub CI;
- base, `platform`, `s3`, `layout`, and `full` installation profiles;
- all four console scripts, UI/package data, migration checksums, and local
  documentation links;
- wheel source revision, `dirty=false`, and `release_build=true`;
- AGPL `/source` and `/source.json` resolve to the exact tagged source;
- final-candidate preflight and long-running validation evidence are reviewed
  under the documented risk acceptance.

Do not start shared GPU services as part of publishing. Real-engine evidence is
a separate deployment gate and uses only task-owned services for outage tests.
