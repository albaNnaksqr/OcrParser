# Third-Party License Audit

English | [中文](third-party-licenses.zh-CN.md)

This page records an engineering license inventory, not legal advice. The
repository-level MIT license applies only to original OCR Parser code. Bundled
or derived third-party code and separately installed dependencies retain their
own licenses.

Audit date: 2026-07-17.

## Bundled Or Derived Source

| Component | Code in this repository | Upstream and audited source | License | Current action |
| --- | --- | --- | --- | --- |
| dots.ocr | `dots_ocr/**` | [rednote-hilab/dots.ocr](https://github.com/rednote-hilab/dots.ocr) | MIT | Copyright and the complete MIT notice are retained in `NOTICE` and `third_party/licenses/DOTS_OCR_LICENSE.txt`. |
| PaddleX | Cross-page table merge and title-level helpers in `ocr_parser/engines/paddleocr_vl.py` | PaddleX v3.5.1 [`merge_table.py`](https://github.com/PaddlePaddle/PaddleX/blob/v3.5.1/paddlex/inference/pipelines/layout_parsing/merge_table.py) and [`title_level.py`](https://github.com/PaddlePaddle/PaddleX/blob/v3.5.1/paddlex/inference/pipelines/layout_parsing/title_level.py) | Apache-2.0 | Copyright, source, modification notice, and the complete Apache-2.0 text are retained. |
| MinerU | `services/layout_detection/bbox_utils.py` and `services/layout_detection/pp_doclayoutv2.py` | [MinerU commit `e52d40b`](https://github.com/opendatalab/MinerU/tree/e52d40b51ef76db5d057d84412a9d79d7aff744f/mineru/model/layout) | Apache-2.0 for this imported snapshot | Copyright, exact source revision, modification notice, and the complete Apache-2.0 text are retained. Current MinerU models and newer code require a separate license review. |

The historical repository shows that the two layout files were imported on
2026-05-05. `bbox_utils.py` is byte-for-byte identical to MinerU commit
`e52d40b51ef76db5d057d84412a9d79d7aff744f`; `pp_doclayoutv2.py` differs only
by its local import path and two documented Transformers 5 compatibility
changes. The [license at that exact commit](https://github.com/opendatalab/MinerU/blob/e52d40b51ef76db5d057d84412a9d79d7aff744f/LICENSE.md)
was Apache-2.0.

## Current MinerU Models And Newer Source

MinerU changed its repository license after the imported snapshot. The current
MinerU Open Source License is based on Apache-2.0 with additional terms. A
current MinerU model or newer source revision must be reviewed under its own
declared terms instead of assuming the older Apache-2.0 source provenance. The
current additional terms include:

- commercial use above either stated MAU or monthly-revenue threshold requires
  a separate MinerU commercial license;
- an online service provided to third parties must prominently state that it
  uses MinerU, either in the service UI or public documentation;
- operators must review the authoritative upstream terms before deployment.

Review the authoritative current terms in the upstream
[MinerU license](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md).
If those terms apply, this public page provides project-level attribution, but
a downstream product must still make sure its own UI or public documentation
satisfies the attribution condition.

## PyMuPDF AGPL Deployment Path

`PyMuPDF==1.26.3` is a required dependency and is used directly for PDF
opening, rendering, and output work. PyMuPDF is dual-licensed under GNU AGPL v3
or an Artifex commercial license. The repository's MIT license does not remove
those terms from a combined deployment.

This public repository now implements the AGPL deployment path:

- the complete GNU AGPLv3 text is distributed in
  `third_party/licenses/AGPL_3.0.txt` and in the wheel;
- the combined deployment and retained permissive notices are described in
  `LICENSE` and `NOTICE`;
- Control exposes public `/source`, `/source.json`, and `/legal/agpl-3.0`
  routes, and the UI displays the required legal and source notices;
- tagged builds resolve source from their matching version tag, while patched
  builds can pin an exact public commit or immutable archive.

See [AGPL Compliance](agpl-compliance.md) for deployment verification. An
operator can still choose one of the other available paths:

1. obtain and record an applicable commercial license from Artifex; or
2. replace PyMuPDF with a dependency whose license matches the deployment
   policy, then rerun PDF behavior and performance baselines.

See the official
[PyMuPDF license documentation](https://pymupdf.readthedocs.io/en/latest/about.html).

## Model Weights

The repository does not distribute DotsOCR, MinerU, PaddleOCR-VL, or layout
model weights. Each model repository can use a license different from the
parser, inference server, and layout-service source code. Engine certification
therefore records code license and model license separately; see
[Engine Certification](engine-certification.md).

## Release Check

For every release that adds or updates copied source, dependencies, or model
profiles:

- record the exact upstream repository, tag or commit, file path, and import
  date;
- retain upstream copyright, license, NOTICE, and local modification notices;
- include `third_party/licenses/*` in both source and wheel distributions;
- review runtime dependency licenses independently from vendored source;
- record the exact model repository, revision, and license in the engine
  certification evidence;
- verify that `/source` is public and resolves to the exact deployed source.
