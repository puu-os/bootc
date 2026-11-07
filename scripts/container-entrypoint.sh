#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

readonly workspace=/workspace
readonly artifacts_dir=${workspace}/build
readonly output_dir=${OUTPUT_DIR:-${artifacts_dir}}

if ((EUID == 0)); then
  workspace_uid=$(stat -c %u "${workspace}")
  workspace_gid=$(stat -c %g "${workspace}")

  install -d -m 0755 -o "${workspace_uid}" -g "${workspace_gid}" \
    "${HOME}" "${artifacts_dir}" "${output_dir}"

  if ((workspace_uid != 0)); then
    if ! getent group "${workspace_gid}" >/dev/null; then
      groupadd --gid "${workspace_gid}" puu-builder
    fi
    if ! getent passwd "${workspace_uid}" >/dev/null; then
      useradd \
        --uid "${workspace_uid}" \
        --gid "${workspace_gid}" \
        --home-dir "${HOME}" \
        --no-create-home \
        --no-log-init \
        --shell /bin/bash \
        puu-builder
    fi
    exec gosu "${workspace_uid}:${workspace_gid}" "$0" "$@"
  fi
fi

cd "${workspace}"
shopt -s nullglob

defconfigs=(configs/*_defconfig)
if ((${#defconfigs[@]} == 0)); then
  echo "error: no defconfigs found in ${workspace}/configs" >&2
  exit 1
fi

for defconfig in "${defconfigs[@]}"; do
  board=${defconfig##*/}
  board=${board%_defconfig}
  if [[ ! "${board}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
    echo "error: invalid board name: ${board}" >&2
    exit 1
  fi
  make build BOARD="${board}" OUTPUT_DIR="${output_dir}"

  if [[ "${output_dir}" != "${artifacts_dir}" ]]; then
    install -d "${artifacts_dir}/${board}"
    rsync --archive \
      "${output_dir}/${board}/.config" \
      "${output_dir}/${board}/images" \
      "${artifacts_dir}/${board}/"
  fi
done
