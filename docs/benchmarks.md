# Benchmark Notes

English | [中文](benchmarks.zh-CN.md)

This document summarizes sanitized benchmark evidence from OcrParser
development. The numbers are useful for understanding framework behavior, not
for ranking OCR models.

Resource budgets differed between runs:

- DotsOCR validation used a larger API concurrency budget and was tuned for
  high-throughput batch execution.
- MinerU-style and PaddleOCR-VL-style validation used smaller two-stage smoke
  and gray runs to prove bounded layout/recognition behavior.
- Endpoint queueing, hardware, PDF shape, and OCR model quality were not
  normalized across engines.

## How To Reproduce On Your Endpoint

Generate synthetic fixtures:

```bash
python3 tools/generate_benchmark_pdfs.py --output-dir /tmp/ocr-benchmark-pdfs
```

Run a directory benchmark:

```bash
python3 tools/run_performance_baseline.py \
  --input-dir /tmp/ocr-benchmark-pdfs \
  --output-root /tmp/ocr-benchmark-results \
  --variant current=. \
  --run-mode directory \
  --engine dotsocr \
  --ip YOUR_MODEL_ENDPOINT \
  --port 13080 \
  --model-name DotsOCR \
  --file-concurrency 4 \
  --page-concurrency 16
```

The runner writes CSV and Markdown summaries with duration, pages per second,
status, output paths, and extracted runtime metrics when present in logs.

## DotsOCR: Page Concurrency Curve

Synthetic fixture set:

- 8 PDFs
- 36 total pages
- single parser process
- `num_cpu_workers=1`
- `md_gen_concurrency=1`
- resume disabled and force reprocess enabled

| Page concurrency | Files OK | Pages | Total s | Avg s/page | Speedup vs c=1 | Long document speedup |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 8/8 | 36 | 193.612 | 5.378 | 1.00x | 1.00x |
| 2 | 8/8 | 36 | 131.533 | 3.654 | 1.47x | 1.59x |
| 4 | 8/8 | 36 | 81.911 | 2.275 | 2.36x | 3.17x |
| 8 | 8/8 | 36 | 79.364 | 2.205 | 2.44x | 4.64x |
| 16 | 8/8 | 36 | 64.241 | 1.784 | 3.01x | 7.42x |

Reading:

- DotsOCR-style full-page VLM extraction benefited strongly from client-side
  page concurrency on multi-page files.
- One-page and two-page files showed more variance because fixed overhead
  dominates.
- A conservative production starting point should consider endpoint queue
  health and failure rate, not just the fastest local timing.

## DotsOCR: File Concurrency And Global API Pool

The key scheduler question was whether directory jobs should process one PDF at
a time or let multiple PDFs share one bounded global API pool.

Small A/B:

| Files | Pages | Global API cap | File concurrency | Duration | Throughput |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 5 | 251 | 80 | 1 | 79.11s | 3.17 page/s |
| 5 | 251 | 80 | 3 | 50.17s | 5.00 page/s |

`file_concurrency=3` reduced wall time by 28.94s and improved throughput by
about 58% on this sample.

Medium sweep:

| Files | Pages | Global API cap | Best tested file concurrency | Best throughput |
| ---: | ---: | ---: | ---: | ---: |
| 16 | 1156 | 80 | 5 | 5.91 page/s |
| 50 | 2969 | 80 | 8, stable repeat | 7.82 page/s |

Reading:

- File-level concurrency improved utilization while individual PDFs moved
  through render, API, and post-processing phases.
- Higher file concurrency did not raise the API ceiling once the API pool was
  already saturated.
- Tail latency and post-processing can dominate the end of a run, so the best
  value is workload-specific.

## Platform Path Overhead

The same 50-PDF, 2969-page DotsOCR gray workload was run through the
control/agent/shard path:

| Path | Files | Pages | Settings | Duration | Throughput |
| --- | ---: | ---: | --- | ---: | ---: |
| Direct CLI repeat | 50 | 2969 | API cap 80, `file_concurrency=8` | 379.70s | 7.82 page/s |
| Control/agent/shard | 50 | 2969 | Same parser profile, one shard | 376.07s | 7.90 page/s |

Reading:

- The platform path propagated the same parser profile to the worker.
- At this gray-test scale, the control API, agent loop, manifest scan, and shard
  execution path did not add meaningful throughput overhead.
- This result does not prove infinite scaling. It shows the orchestration layer
  was not the bottleneck for this workload shape.

## MinerU-Style Two-Stage Validation

MinerU-style parsing uses a VLM endpoint for both layout and block recognition.
That means a single global API cap is not enough; layout needs reserved
capacity so recognition does not starve new pages.

Medium gray run:

| Files | Pages | File concurrency | Page concurrency | API cap | Recognition cap | Result |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 50 | 50 | 4 | 4 | 8 | 6 | 50/50 success, no API errors/timeouts |

Observed behavior:

- layout calls and recognition calls were counted separately;
- recognition queue depth was visible;
- block filtering reduced unnecessary recognition work;
- output artifacts were non-empty for every file in the run.

## PaddleOCR-VL-Style Two-Stage Validation

PaddleOCR-VL-style parsing separates layout detection from VLM block
recognition. The stability risk is layout running too far ahead and creating a
large block backlog.

Medium gray run:

| Files | Pages | File concurrency | Page concurrency | API cap | Layout cap | Result |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 50 | 50 | 4 | 4 | 8 | 2 | 50/50 success, no API errors or layout fallbacks |

Observed behavior:

- layout calls were bounded separately from recognition calls;
- block queue depth was visible;
- high/low watermark backpressure was available;
- output artifacts were non-empty for every file in the run.

## Interpreting These Results

The strongest conclusions are framework-level:

- concurrency has to be tuned per engine and per endpoint;
- a single global "pages at once" knob is not enough for two-stage engines;
- bounded file concurrency improves batch throughput when the endpoint has
  headroom;
- the control/worker path can preserve parser throughput when it passes the
  same profile through correctly;
- observability counters are part of the performance story because they show
  whether the bottleneck is API, layout, recognition, CPU, or tail output work.
