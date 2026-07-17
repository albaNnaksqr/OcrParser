# v0.2 Release Checklist

Run from a clean public-repository checkout:

```bash
python -m compileall -q ocr_parser dots_ocr ocr_platform tools
python -m pytest -q
python tools/check_docs_links.py
python tools/run_mock_e2e.py
python -m build --wheel
```

In CI, the PostgreSQL job applies every committed migration and runs concurrent
shard/scan-unit claim checks. The package job installs the wheel into an empty
environment, verifies all three console-script declarations, and checks UI and
migration package data.

Before tagging the parser release:

- compare the baseline and candidate benchmark CSV with
  `tools/check_performance_regression.py`; regression must be at most 10%;
- verify stop/resume, worker disconnect spool/replay, manifest integrity, and
  output audit in the release environment;
- confirm `git status --short` is empty and every required CI job is green;
- tag the exact commit consumed by internal deployments.
- confirm the wheel includes `AGPL_3.0.txt`, `LICENSE`, and `NOTICE`;
- start Control with API authentication enabled and confirm `/source`,
  `/source.json`, and `/legal/agpl-3.0` remain public;
- confirm `/source` resolves to the exact tagged commit. For an untagged or
  patched build, set `OCR_PLATFORM_SOURCE_REVISION` or
  `OCR_PLATFORM_SOURCE_URL` before exposing the service.

Real-model checks are maintained separately in the
[engine certification matrix](engine-certification.md). They are deployment
gates for an enabled engine, not a requirement to start GPU services while
creating a GitHub release and not a substitute for unit tests.
