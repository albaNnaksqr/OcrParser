# AGPL Compliance

English | [中文](agpl-compliance.zh-CN.md)

This is an engineering compliance procedure, not legal advice. Original
OcrParser source remains available under MIT. PyMuPDF is a required, directly
imported dependency and is offered by Artifex under GNU AGPLv3 or a commercial
license. A deployment using its AGPL build makes the combined application
available under GNU AGPLv3 while preserving the notices for MIT, Apache-2.0,
and other separately licensed portions.

## Network source offer

The Control service exposes these public routes outside API-token middleware:

- `/source` redirects to the Corresponding Source for the running version;
- `/source.json` records package version, source revision, URL, license, and
  warranty notice;
- `/legal/agpl-3.0` serves the complete bundled GNU AGPLv3 text.

The Control UI displays copyright, no-warranty, redistribution, source, and
license notices. These routes must remain reachable by every network user,
including when `OCR_PLATFORM_API_TOKEN` is enabled.

For a tagged wheel, `/source` defaults to the repository tag matching the
package version, for example `v0.2.1`. An untagged, patched, or internally built
deployment must identify its exact source with one of:

```bash
OCR_PLATFORM_SOURCE_REVISION=<exact-public-commit>
# or, for an immutable source archive:
OCR_PLATFORM_SOURCE_URL=https://downloads.example/source/ocrparser-<commit>.tar.gz
```

`OCR_PLATFORM_SOURCE_URL` takes precedence over the generated repository URL.
It must be free to access, require no token, and remain available for the
required distribution and support period. Do not point a patched deployment at
an older release tag or at a moving branch without identifying the deployed
commit.

## Corresponding Source boundary

The source location must contain the exact deployed OcrParser source plus the
non-secret material required to build, install, run, and modify it, including
dependency declarations, container/build files, operational scripts, and any
local patches. Preserve all copyright, modification, license, and warranty
notices.

Credentials, customer documents, database contents, internal hostnames, and
other deployment secrets are not source and must not be published. Independent
model services and model weights retain their own licenses and certification
records.

## Deployment verification

Before enabling network access:

```bash
curl -fsS http://CONTROL_HOST:CONTROL_PORT/source.json
curl -fsS http://CONTROL_HOST:CONTROL_PORT/legal/agpl-3.0 | head
curl -sSI http://CONTROL_HOST:CONTROL_PORT/source
```

Confirm that the redirect resolves without authentication to the exact running
source and that the source can reproduce the deployed application. The release
wheel must contain `LICENSE`, `NOTICE`, and
`third_party/licenses/AGPL_3.0.txt`; CI checks this inventory.

If an operator deploys under a valid Artifex commercial license instead, that
agreement controls the PyMuPDF use. Record the agreement and applicable notice
policy in the private deployment inventory; do not publish the agreement or
credentials in this repository.
