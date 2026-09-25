#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

: "${PUU_COSIGN_KEYS?PUU_COSIGN_KEYS must be set (empty for unsigned builds)}"

CONTAINER=${CONTAINER:-docker}
CONTAINER_CPUS=${CONTAINER_CPUS:-8}
CONTAINER_MEMORY=${CONTAINER_MEMORY:-16G}
container_kind=$(basename -- "${CONTAINER}")

build_args=(--tag puu-builder:latest)
if [ "${container_kind}" = container ]; then
  "${CONTAINER}" system status >/dev/null 2>&1 || "${CONTAINER}" system start
  build_args=(
    --cpus "${CONTAINER_CPUS}"
    --memory "${CONTAINER_MEMORY}"
    "${build_args[@]}"
  )
fi

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

for volume in puu-build puu-cache; do
  "${CONTAINER}" volume inspect "${volume}" >/dev/null 2>&1 ||
    "${CONTAINER}" volume create "${volume}" >/dev/null
done

run_args=(--env OUTPUT_DIR=/build)
for var in $(compgen -v PUU_); do
  run_args+=(--env "${var}")
done
if [ -n "${PUU_COSIGN_KEYS}" ]; then
  IFS=: read -r -a cosign_keys <<< "${PUU_COSIGN_KEYS}"
  for key in "${cosign_keys[@]}"; do
    if [ "${container_kind}" = container ]; then
      key_dir=$(dirname -- "${key}")
      run_args+=(--mount "type=bind,src=${key_dir},dst=${key_dir},readonly")
    else
      run_args+=(--mount "type=bind,src=${key},dst=${key},readonly")
    fi
  done
fi

"${CONTAINER}" build "${build_args[@]}" .

"${CONTAINER}" run \
  -d \
  --init \
  --cpus "${CONTAINER_CPUS}" \
  --memory "${CONTAINER_MEMORY}" \
  "${run_args[@]}" \
  --volume "${PWD}:/workspace" \
  --volume puu-build:/build \
  --volume puu-cache:/cache \
  puu-builder:latest
