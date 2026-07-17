# OCR Benchmark PDFs

Synthetic local fixtures for engine latency, throughput, and layout checks.

| File | Pages | Category | Purpose | Expected stress |
| --- | ---: | --- | --- | --- |
| `simple_text_1p.pdf` | 1 | text | Plain-text latency baseline | Fixed overhead and paragraph ordering |
| `receipt_narrow_1p.pdf` | 1 | narrow | Receipt-like narrow page | Small fonts and non-A4 page geometry |
| `invoice_table_2p.pdf` | 2 | table | Invoice-style tables with numeric cells | Table structure, numeric alignment, repeated headers |
| `mixed_layout_2p.pdf` | 2 | mixed | Two-page mixed certification fixture | Reading order, table cells, and figure caption |

Recommended first pass:

- Run every engine with page concurrency 1 on all files.
- Increase DotsOCR concurrency across 2, 4, 8, and 16 only after the baseline is stable.
- Keep MinerU and PaddleOCR-VL at 1 or 2 concurrent pages unless the single-instance queue stays healthy.

Certification fields and reading-order expectations are stored in `expected.json`.
Validate real-engine outputs with:

```bash
python3 tools/check_engine_fixture_outputs.py --engine ENGINE --output-dir OUTPUT
```

Fixtures contain generated public text only. Do not replace them with customer documents.
