# Multi-Engine OCR Validation

Models used for local validation should be stored outside this repository, for
example under `/opt/ocr-models` or another host-local model cache. Tune model
server GPU memory settings for your hardware instead of using a full-card
allocation by default.

## DotsOCR

```bash
python ocr_parser_cli.py \
  --engine dotsocr \
  --input_file /path/to/sample.pdf \
  --output_dir /tmp/ocrparser-smoke/dots \
  --ip 127.0.0.1 \
  --port 8000 \
  --save_page_json
```

Expected: existing Markdown output is produced and native DotsOCR raw files appear under `native/dotsocr/` after Task 6 is wired.

## MinerU

```bash
python ocr_parser_cli.py \
  --engine mineru \
  --input_file /path/to/sample.pdf \
  --output_dir /tmp/ocrparser-smoke/mineru \
  --ip 127.0.0.1 \
  --port 30000 \
  --model_name mineru \
  --save_page_json
```

Expected: page raw files appear under `native/mineru/`; if the response contains Markdown, document Markdown is also aggregated there.

## PaddleOCR-VL

```bash
python ocr_parser_cli.py \
  --engine paddleocr-vl \
  --input_file /path/to/sample.pdf \
  --output_dir /tmp/ocrparser-smoke/paddleocr-vl \
  --ip 127.0.0.1 \
  --port 30001 \
  --model_name paddleocr-vl \
  --save_page_json
```

Expected: page raw files appear under `native/paddleocr-vl/`; if the response contains Markdown, page and document Markdown files are written.
