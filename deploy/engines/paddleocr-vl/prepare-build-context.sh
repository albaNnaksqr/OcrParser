#!/usr/bin/env bash
set -euo pipefail

readonly REVISION="0fe2dbd42caeb627bd8aca162dab7763d292fda9"
readonly SHA256="c2bcbd305cfd9f0260a8ede833b1120bb804562369b77f4798855616857aeb16"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ARCHIVE="${SCRIPT_DIR}/sglang-source.tar.gz"

curl --fail --location --silent --show-error \
  --retry 5 --retry-all-errors \
  --output "${ARCHIVE}.tmp" \
  "https://codeload.github.com/sgl-project/sglang/tar.gz/${REVISION}"
actual_sha256="$(shasum -a 256 "${ARCHIVE}.tmp" | awk '{print $1}')"
test "${actual_sha256}" = "${SHA256}"
mv "${ARCHIVE}.tmp" "${ARCHIVE}"
echo "Prepared ${ARCHIVE}"
