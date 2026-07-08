# Contributing

Thanks for helping improve OCR Parser.

## Development Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

## Checks

Run these before opening a pull request:

```bash
python -m compileall ocr_parser dots_ocr ocr_platform
pytest tests
```

## Pull Requests

- Keep changes focused on one parser, platform, or documentation concern.
- Add or update tests for parser behavior, resume logic, output writers, control
  API behavior, worker behavior, or deployment tooling.
- Use public placeholders in docs and tests. Do not commit private hostnames,
  private endpoints, customer data, API keys, logs, runtime databases, or model
  weights.
- Include the command output you used for verification.

## Issue Reports

When reporting bugs, include:

- Python version and operating system
- Parser command or platform mode
- OCR engine type and whether the endpoint is OpenAI-compatible
- A minimal public PDF or a synthetic reproduction when possible
- Redacted logs or tracebacks
