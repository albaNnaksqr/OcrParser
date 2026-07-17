# Engine Certification

English | [中文](engine-certification.zh-CN.md)

This matrix separates parser release readiness from optional real-model engine
certification. A GitHub release does not need to start model services. Engine
certification is a dated deployment record for operators enabling an engine.

## Status Definitions

- **Certified**: the exact parser commit and model revision passed endpoint
  health, public-fixture end-to-end parsing, output review, and deployment
  license review.
- **Verified**: real services passed functional checks, but at least one
  production gate such as broad quality coverage, observability, performance,
  or license approval remains open.
- **Contract only**: mock/unit contract checks pass, with no current
  real-service evidence.
- **Blocked**: a required service or model could not start or failed the
  minimum functional checks.

A real-model smoke pass does not approve quality for every document class.
Results with different model replica counts, server versions, GPU budgets, or
model revisions must not be compared directly as a performance regression.

## Current Matrix

Evidence commit: `9c3bea6` (`v0.2.0`). Matrix refresh: 2026-07-17.
The machine-readable provenance is maintained in
[`engine-certification-records.json`](engine-certification-records.json).
Conditional rows intentionally retain a null runtime digest: they cannot become
**Certified** until the v0.3 rc run records a locked image digest and the exact
parser commit.

| Engine | Service topology | Contract | Real service and output | License review | Status |
| --- | --- | --- | --- | --- | --- |
| DotsOCR (`dotsocr`) | One OpenAI-compatible VLM endpoint | Pass | Not rerun in this Spark refresh; prior release evidence only | Parser code is MIT; AGPL source offer is implemented; model approval remains deployment-specific | **Verified**, not refreshed |
| MinerU (`mineru`) | One OpenAI-compatible VLM endpoint for layout and recognition | Pass | Both public fixtures produced readable output with the validated vLLM backend; SGLang produced semantically invalid repeated tokens | Model and validated runtime are Apache-2.0; AGPL source offer is implemented | **Verified**, vLLM only and conditional |
| PaddleOCR-VL (`paddleocr-vl`) | PP-DocLayoutV2 `/detect` plus OpenAI-compatible VLM | Pass | Both public one-page fixtures completed; text was readable, but the narrow receipt table contained empty cells and page status could not distinguish normal two-stage output from a true fallback | Paddle models and imported layout source are Apache-2.0; AGPL source offer is implemented | **Verified**, conditional |

The required PyMuPDF dependency is AGPL/commercial dual-licensed. This
repository implements the AGPL source-offer path; each deployment must still
verify that `/source` resolves to its exact running source. See
[AGPL Compliance](agpl-compliance.md).

Public fixtures used in this refresh:

- `simple_text_1p.pdf` SHA256
  `eb542ecf8b1b4052d32b3f69449d3e875f8a9f8074851ec6b964f32ca3c259ff`;
- `receipt_narrow_1p.pdf` SHA256
  `a2ef9fd25513654491136d69b6018ce6032a699f0ebfceaf06769389a63e6bb5`.

## MinerU Evidence

- Parser: `v0.2.0`, commit `9c3bea6`.
- Model: [OpenDataLab/MinerU2.5-Pro-2604-1.2B](https://huggingface.co/opendatalab/MinerU2.5-Pro-2604-1.2B),
  Hugging Face revision `d3f5e08d073c21466bbabe21c71bb1e9c2e595da`,
  ModelScope revision `5ce0a587eda752aa7e4c45e2198ec4c2f00e0bd8`,
  Apache-2.0. The `model.safetensors` SHA256
  `f2650d91aaa619534980445034f62cde27fc3fa0430aaf5c3302b91179cad0c5`
  matched the Hugging Face LFS object ID.
- Validated backend: NVIDIA vLLM container `nvcr.io/nvidia/vllm:26.03-py3`,
  Python 3.12.3, PyTorch `2.11.0a0+a6c236b9fd.nv26.03.46836102`, vLLM
  `0.17.1+a03ca76a.nv26.03.46967107`, Transformers 4.57.5, and
  `mineru-vl-utils==1.0.5` with
  `mineru_vl_utils:MinerULogitsProcessor` loaded at engine startup.
- Health: `/v1/models` and `/health` returned HTTP 200.
- `simple_text_1p.pdf`: CLI exit 0, document success, one
  `success_fallback_text` page; parser elapsed 4.520 s and wall time 7.558 s.
  The body was readable and correct, with one duplicated checklist.
- `receipt_narrow_1p.pdf`: CLI exit 0, document success, one
  `success_fallback_text` page; parser elapsed 3.407 s and wall time 4.428 s.
  Receipt fields and amounts were readable and mostly correct, but the table
  was flattened into sequential text.
- Resource observation: `gpu_memory_utilization=0.40`; the log reported 2.16
  GiB model weights and 43.4 GiB KV cache, with an approximately 47.9 GiB
  budget ceiling. This is below the validation limit, not a benchmark.
- Negative control: the generic SGLang path returned healthy HTTP and CLI
  success but generated 4096 repeated `!` characters. In a direct comparison
  using the same page and `Layout Detection:` prompt, SGLang returned 64/64
  `!` characters with `finish_reason=length` when `max_tokens=64`; vLLM with
  `mineru_vl_utils:MinerULogitsProcessor` returned 312 valid layout tokens with
  `finish_reason=stop` when `max_tokens=512`. SGLang did not load the required
  logits processor and is not certified for this model revision.

The common `success_fallback_text` page status is also present in normal MinerU
two-stage output, so this backend remains conditional until normal success and
actual fallback are distinguishable in sidecars.

## PaddleOCR-VL Evidence

- Parser: `v0.2.0`, commit `9c3bea6`.
- Recognition model: `PaddleOCR-VL-1.6`, immutable weight revision
  `d911116c363676c602c4786ad0b9667b1aee055f`; `model.safetensors` SHA256
  `85a479d506a11e724e7285d395c551be69f41dbc16b6342d3cacfb189aed71db`;
  the model card declares Apache-2.0.
- Layout model: `PP-DocLayoutV2`, revision
  `a0a882d03564ff3a9c9b37e48e2e636e7c236fd6`; `model.safetensors` SHA256
  `e60f3725aeedc88fd319416ef166bda79171a41516a301c27cab9132dc2739d2`; model card
  declares Apache-2.0. The local model snapshot did not include a separate
  license file, so downstream packaging must retain the model-card evidence.
- Observed recognition runtime: Python 3.12.13, PyTorch `2.11.0+cu130`,
  Transformers 5.5.4, SGLang source commit
  `0fe2dbd42caeb627bd8aca162dab7763d292fda9`, sglang-kernel 0.4.2.post2, and
  flashinfer-python 0.6.7.post3. The shared environment had dependency drift
  and used Triton/PyTorch backends to bypass a kernel version mismatch, so it
  is not a reproducible production-pinned image.
- Health: VLM `/v1/models`, layout `/health`, layout `/detect`, and real chat
  completions all returned HTTP 200.
- `simple_text_1p.pdf`: CLI exit 0, document success, one
  `success_fallback_text` page; parser elapsed 2.366 s and wall time 4.102 s.
  Text was readable with minor symbol errors.
- `receipt_narrow_1p.pdf`: CLI exit 0, document success, one
  `success_fallback_text` page; parser elapsed 1.670 s and wall time 2.455 s.
  Body text was readable, but the detected table contained empty cells.
- Resource observation: the recognition service log showed approximately
  45.78 GiB reserved/used, within the roughly 60 GiB validation budget. Layout
  service memory was not separately exposed, so this is not a benchmark.
- Cleanup: all task-owned MinerU, Paddle VLM, and layout services were stopped;
  validation ports no longer listened and no GPU compute process remained. The
  existing shared mock service was not stopped.

The v0.3 development line keeps `success_fallback_text` for compatibility but
now emits `stages` and structured `fallback` metadata. Normal MinerU/Paddle
two-stage completion records `fallback.used=false`; real degradation records a
bounded reason and source stage. The certification remains conditional until the
v0.3 release candidate is revalidated against the pinned real services.

## Minimum Evidence Per Engine

Every real-service record must include parser commit, public fixture checksums,
immutable model revision and license, runtime versions, sanitized parameters,
health and exit results, page/fallback state, artifact completeness, observed
time and GPU memory, manual review, limitations, and cleanup confirmation.

Do not publish internal hosts, credentials, private model paths, customer
documents, or shared-machine process details in certification evidence.

## Release And Deployment Policy

The core release gate is CI, behavior contracts, PostgreSQL migration and
concurrency checks, mock end-to-end execution, wheel installation, output
audit, and performance-regression protection. It does not start GPU services.

Production use additionally requires a current **Certified** row for the exact
model and profile, or an explicit risk acceptance for a **Verified** row. A
model revision, server major version, parser output-contract change, or
material topology change invalidates the affected evidence until it is rerun.
