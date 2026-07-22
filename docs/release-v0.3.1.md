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

## Sanitized Wave 5 Evidence

The first short preflight at revision
`12abb795aa55f986cea29aa0e24451be40bd6f77` exposed a real P1: after a
same-server restart, stale work was reclaimable but normal pending shards could
starve it. Eligibility-to-claim took 49.862 seconds and eligibility-to-job
terminal took 57.436 seconds. The 24-hour soak was not started.

The retained P1 failure summary SHA256 values are
`9d4387207b37f649ee2c48abe1f509c42188883560d5429be7224e3565f1fdbe`
for `report.json` and
`cb9e0a999b3f26bd404c270cc46257bcbe3844a3c19fd50ed443d08612a4fa1e`
for `report.md`.

Revision `1d3c8f560e94c4550718fc9910e8344ef38eae89` fixed that recovery path by
adding the registration fence and stale/retrying claim priority described
above. It did not change any public interface. GitHub CI run `29895536565`
passed all 11 jobs, including 846 tests. Its clean `0.3.1` wheel has SHA256
`33482a9265f68b9be0b7b9bebc8b845bc9d5e6443b9a4c606c844f70f2c838d3`.

One post-fix run fired the restart fault too early. The core fence and reclaim
assertions passed, but seven ordinary pending shards legitimately remained and
the job took 48 seconds to finish. That run is retained as the original
**FAIL**; it is not relabeled as a pass.

The retained early-timing failure summary SHA256 values are
`051f14d8d6352f996564e6f04129fd1507871b8cd84e54cf74d1d16fff4dfb01`
for JSON and
`9e4e8a6c16a8442b86f984232e2c899f10641c24e80d0a8b7566bd49dde0dfa4`
for Markdown.

A fresh strict r4 run then injected the restart while exactly two ordinary
pending shards remained and completed all three cycles:

- cycle 1 reclaimed the terminated Agent's work in 19.327 seconds;
- cycle 2 replayed 30 event/log records and one shard update after the Control
  outage; migration `plan`, `apply`, and `verify` passed;
- cycle 3 fenced the old assignment in 0.095 seconds, claimed the stale shard
  0.550 seconds after eligibility, reached the target shard terminal 1.725 seconds after eligibility,
  seconds, selected stale work before pending work, and completed the job in
  18.487 seconds;
- 300/300 documents and 30/30 shards completed; duplicate counts were zero,
  spool and quarantine were empty, manifest/output audits were 100%, resource
  checks passed, and cleanup found zero task-owned residue.

Sanitized successful-run checksums:

- `report.json`: `107a8d18edca13bd5c8458c12f5e73b631c3468ef0ae066a83dfe34619e7fcce`
- `report.md`: `a0d4d98051aae0d0d60aadf169a182e8597204f2eea388ddff5f1949e06a3959`
- operator timeline: `671edca29d5bc89843baae42cc3b291e393c339ed0f4d7fd50b8bbacf68f82f7`
- audit: `422e5359cbade42e930f05885ae1db7f41aa6091f77d4a0863c25c514ed45848`
- cleanup: `e93b0ac4a63bdf17998b60deceaf3d0b00988e21ee61ddecffc0f34bde597e53`

This is still a short preflight, not the final 24-hour v0.3.1 soak. The
24-hour run must use the exact final candidate commit and wheel; any tracked
file change invalidates it.

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
