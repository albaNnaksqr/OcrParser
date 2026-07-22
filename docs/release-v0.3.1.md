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

The base installation also declares `beautifulsoup4>=4.12,<5`. PaddleOCR-VL
multi-page table merging imports this dependency; the missing declaration was
an installation blocker, not an OCR algorithm change.

## Sanitized Pre-release Evidence

A clean `0.3.0` candidate at revision
`e31e494a721a23c9103ccf3f79646575ae2d468c` completed a three-cycle isolated
Spark preflight with two Agents and PostgreSQL 16:

- 300/300 generated public PDFs and 30/30 shards completed;
- cycle 1 terminated one Agent and the peer reclaimed work at attempt 2;
- cycle 2 stopped Control for 60 seconds, then replayed 36 event/log records and
  one shard update; migration `plan`, `apply`, and `verify` passed;
- cycle 3 performed a graceful same-server Agent shutdown and restart, with no
  unexpected duplicates, quarantine records, or late reporting;
- manifest and output audits passed for every cycle, and cleanup found no
  task-owned process, port, container, or GPU-process residue.

The cycle-3 runner originally measured terminal completion from the restart
request and reported 31.524 seconds, 1.524 seconds beyond the two-lease
threshold. Recovery cannot be eligible until the prior lease expires; measured
from that eligibility point, terminal completion took 25.551 seconds and
passed. Both measurements are retained rather than converting the original
result into an unconditional pass.

Sanitized evidence checksums:

- `report.json`: `e5ba4e6300ba6befc5785926043915c7d5df0769ec17f2ca8fd1a92a403601ac`
- `report.md`: `ec15bbf44cbcc200bb06e8c027aecf557d43c6424275401aa4d9d7a252976e0c`
- `SHA256SUMS`: `ec4e32f094be63d7771f02c41f64ec4f2254f10fce20421800ae31c1328020fe`

These are short preflight results, not the final 24-hour v0.3.1 soak. The
24-hour run must use the exact final tagged commit and wheel; any tracked-file
change invalidates it.

## Real-engine Status

All three engines remain **Verified**, not **Certified**:

- DotsOCR: 50/50 public pages completed and 3/4 quality fixtures passed. The
  managed service did not expose immutable model or runtime provenance.
- MinerU: 50/50 pages completed through a task-owned outage and 3/4 quality
  fixtures passed. The remaining failure is model reading-order quality, and
  the derived runtime dependency is not embodied in its own immutable image.
- PaddleOCR-VL: 50/50 pages completed through a task-owned outage. The
  `55d23996997508b61828d254e95aed8bf65d9752` candidate completed all four
  integration fixtures after the base-dependency fix, but only 1/4 met the
  quality expectations. No immutable RepoDigest was produced, and the fixed
  base composition requires an explicitly documented FlashInfer version-check
  bypass.

See [Engine Certification](engine-certification.md) for revisions, digests,
fixture results, and limitations. Model-quality misses are recorded as limits;
they are not represented as Parser certification successes.

## Release Gate

Before tagging, build a clean wheel from the final commit and verify:

- Python 3.10, 3.11, and 3.12 tests and GitHub CI;
- base, `platform`, `s3`, `layout`, and `full` installation profiles;
- all four console scripts, UI/package data, migration checksums, and local
  documentation links;
- wheel source revision, `dirty=false`, and `release_build=true`;
- AGPL `/source` and `/source.json` resolve to the exact tagged source;
- the final-candidate three-cycle preflight and 24-hour soak pass.

Do not start shared GPU services as part of publishing. Real-engine evidence is
a separate deployment gate and uses only task-owned services for outage tests.
