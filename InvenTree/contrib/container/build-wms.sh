#!/usr/bin/env bash

set -euo pipefail

WMS_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WMS_IMAGE_TAG="${1:-wmsye-inventree:dev}"

cd "${WMS_REPO_ROOT}/src/frontend"
corepack yarn install --frozen-lockfile

# Translation extraction rewrites every locale catalogue. Keep it opt-in so a
# normal application rebuild does not create unrelated translation churn.
if [[ "${WMS_EXTRACT_TRANSLATIONS:-0}" == "1" ]]; then
  corepack yarn extract
fi

corepack yarn compile
corepack yarn build

cd "${WMS_REPO_ROOT}"
docker build \
  --file contrib/container/Dockerfile.wms \
  --tag "${WMS_IMAGE_TAG}" \
  .
