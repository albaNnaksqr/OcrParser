# Security Policy

## Supported Versions

Security fixes target the current `main` branch unless a maintained release
branch is explicitly published.

## Reporting a Vulnerability

Please report vulnerabilities privately through GitHub Security Advisories when
available, or by contacting the maintainers using the repository's published
security contact.

Do not open public issues for suspected credential leaks, unauthorized access,
path traversal, remote command execution, or private-data exposure.

## Sensitive Data Rules

This repository must not contain:

- API keys, access keys, private tokens, or passwords
- Private hostnames, internal IP addresses, or production service URLs
- Customer PDFs or derived customer OCR output
- Runtime databases, logs, PID files, or worker state directories
- Downloaded model weights or large private datasets

Use the `*.example` files in `configs/` and `dots_ocr/` as templates, then store
real configuration outside the repository.
