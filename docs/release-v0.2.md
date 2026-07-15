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

Before tagging:

- compare the baseline and candidate benchmark CSV with
  `tools/check_performance_regression.py`; regression must be at most 10%;
- verify stop/resume, worker disconnect spool/replay, manifest integrity, and
  output audit in the release environment;
- manually quality-check the small public PDFs with DotsOCR, MinerU, and
  PaddleOCR-VL model services;
- confirm `git status --short` is empty and every required CI job is green;
- tag the exact commit consumed by internal deployments.

Real-model quality checks remain manual because public CI has no model service or
GPU. They are release gates, not unit-test substitutes.
