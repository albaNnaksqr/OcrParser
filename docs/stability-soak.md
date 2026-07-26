# v0.3 Stability Soak

English | [中文](stability-soak.zh-CN.md)

This runbook validates a frozen release wheel in an isolated,
production-like environment. It is a platform stability check, not a model
quality benchmark.

## Topology And Safety Boundary

- Use a task-owned PostgreSQL database, Control process, mock OCR service, and
  at least two Agents with separate work and spool directories.
- Use only public or sanitized fixtures. Do not share database, service,
  spool, or output state with production.
- Supply credentials only through named environment variables. Do not place
  secret values in commands, reports, or the repository.
- Keep reports and raw evidence outside the checkout. Only sanitized summaries
  may be published.

## Required Gates

Before processing work, verify:

- the wheel version and immutable source revision match the frozen candidate;
- build provenance reports a clean release build;
- `/source.json` resolves to the same source;
- migration checksums and concurrent PostgreSQL claim behavior pass;
- the runtime uses task-owned directories and services.

Use `python3 tools/run_stability_soak.py --help` for the current runner
interface. The invocation must provide the candidate wheel and revision,
runtime locations, environment-variable names for credentials, distinct worker
identities, report location, workload size, cycle count, and configured
duration. The runner requires both cycle completion and the configured
full-duration window before it finishes.

## Coverage

Rotate directory, existing-manifest, and distributed-snapshot inputs. Exercise
agent loss and reclaim, temporary Control unavailability with spool replay,
graceful Agent shutdown and restart, migration verification, and output audit.
Fault hooks must be argv arrays and may operate only on task-owned resources.

Real-engine checks are a separate deployment gate. They use public fixtures and
task-owned services where disruption is required, and they do not compare
throughput with historical deployments that used different runtimes or replica
counts.

## v0.3.1 Validation Conclusion

The frozen candidate completed a long-running, multi-cycle isolated validation.
Core scheduling, recovery, replay, terminal-state, manifest, and output
integrity checks passed without a product P0/P1 finding.

The review identified two validation-tool issues: completion did not continue
through the configured full-duration window after all cycles finished, and the
resource gate overreacted to low-baseline fluctuations. Both behaviors are
corrected and covered by regression tests. Based on the breadth and duration of
the completed evidence, the remaining validation risk was accepted instead of
repeating the complete long run. This conclusion must not be described as
completion of a strict full-duration soak.

Detailed evidence is retained internally outside the repository and contains
no runtime credentials.

## Acceptance And Evidence

Release evidence must show:

- source, wheel, migration, and claim gates pass;
- all jobs reach a bounded terminal state without lost or duplicate claims,
  artifacts, or events;
- spool replay, restart recovery, and shutdown behavior are correct;
- manifest and output audits pass;
- stage/fallback categories remain known and bounded;
- warm-process resource use remains bounded without a sustained upward trend;
- throughput has no material sustained regression;
- cleanup leaves no task-owned process or service running.

If a product P0/P1 is found, retain the report as release-blocking evidence and
fix the defect before tagging. The post-stabilization direction is recorded in
the [v0.4 operational-maturity RFC](rfc-v0.4.md).
