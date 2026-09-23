#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

: "${TARGET_DIR:?TARGET_DIR must be set by Buildroot}"
: "${DEST_DIR:?DEST_DIR must be set}"

dest_dir="${DEST_DIR}"

catalog_dir="${TARGET_DIR}/usr/share/puu-os/vllm/catalog.d"
cache_root="${PUU_MODELS_CACHE_DIR:-${HOME}/.cache/puu-os/models}"

[ -d "${catalog_dir}" ] || exit 0

export PUU_VLLM_MODELS_DIR="${dest_dir}/var/lib/vllm/models"
export PUU_VLLM_MODEL_CATALOG_DIR="${catalog_dir}"
export HF_HOME="${cache_root}"

mkdir -p "${PUU_VLLM_MODELS_DIR}" "${cache_root}"

if [ -x "${TARGET_DIR}/usr/libexec/puu-os/preseed-vllm-models" ]; then
  "${TARGET_DIR}/usr/libexec/puu-os/preseed-vllm-models"
elif [ -f "${BR2_EXTERNAL_PUU_PATH:-${0%/*}/../..}/package/oksa-services/files/bin/preseed-vllm-models" ]; then
  "${BR2_EXTERNAL_PUU_PATH:-${0%/*}/../..}/package/oksa-services/files/bin/preseed-vllm-models"
fi
