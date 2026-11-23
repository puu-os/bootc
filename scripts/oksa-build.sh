#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

: "${BOARD_DIR:?BOARD_DIR must be set}"
: "${BUILDROOT_DL_DIR:?BUILDROOT_DL_DIR must be set}"
: "${SOURCE_DATE_EPOCH:?SOURCE_DATE_EPOCH must be set}"
export SOURCE_DATE_EPOCH TZ=UTC

script_dir=$(cd "${BASH_SOURCE%/*}" && pwd)
repo_root=$(cd "${script_dir}/.." && pwd)
oksa_src="${repo_root}/oksa"
state_helper="${script_dir}/state.py"
state_file="${BOARD_DIR}/stamp/orchestration/oksa.json"
overlay_dir="${BOARD_DIR}/oksa-overlay"
dl_dir="${BUILDROOT_DL_DIR}/oksa"

inputs=(
  --exclude __pycache__ --exclude '*.pyc' --exclude dist
  --value "epoch=${SOURCE_DATE_EPOCH}"
  --path "${BASH_SOURCE[0]}"
  --path "${state_helper}"
  --path "${oksa_src}"
)
key=$(python3 "${state_helper}" key "${inputs[@]}")
outputs=(
  --path "${overlay_dir}"
)
if python3 "${state_helper}" check "${state_file}" --key "${key}" "${outputs[@]}"; then
  echo "oksa overlay is up to date"
  exit 0
else
  status=$?
  [ "${status}" = 1 ] || exit "${status}"
fi

mkdir -p "${dl_dir}" "${BOARD_DIR}/stamp/orchestration"
work=$(mktemp -d "${BOARD_DIR}/oksa-build.XXXXXX")
trap 'rm -rf -- "${work}"' EXIT
trap 'exit 1' HUP INT TERM

stage="${work}/overlay"
mkdir -p \
  "${stage}/usr/libexec/puu" \
  "${stage}/usr/share/puu/k3s/crds" \
  "${stage}/usr/share/puu/k3s/manifests" \
  "${stage}/usr/share/puu/vllm/catalog.d" \
  "${stage}/usr/share/puu/vllm/runtime"

while IFS=$'\t' read -r dest_name src_name sha256 url; do
  [ -n "${dest_name}" ] || continue
  cached="${dl_dir}/${src_name}"
  if [ ! -f "${cached}" ] || ! echo "${sha256}  ${cached}" | sha256sum --status -c -; then
    "${script_dir}/download.sh" "${url}" "${sha256}" "${cached}"
  fi
  install -D -m 0644 "${cached}" "${stage}/usr/share/puu/k3s/crds/${dest_name}"
done < "${oksa_src}/crds.manifest"

for file in "${oksa_src}"/bin/*; do
  [[ -f "${file}" ]] || continue
  name="${file##*/}"
  if [[ "${name}" == oksa_*.py ]]; then
    install -D -m 0644 "${file}" "${stage}/usr/libexec/puu/${name}"
  else
    install -D -m 0755 "${file}" "${stage}/usr/libexec/puu/${name}"
  fi
done

install -D -m 0644 "${oksa_src}/oksa.conf" \
  "${stage}/usr/lib/tmpfiles.d/oksa-services.conf"

for manifest in "${oksa_src}"/manifests/*.yaml; do
  install -D -m 0644 "${manifest}" "${stage}/usr/share/puu/k3s/manifests/${manifest##*/}"
done
for catalog in "${oksa_src}"/vllm/catalog.d/*.env; do
  install -D -m 0644 "${catalog}" "${stage}/usr/share/puu/vllm/catalog.d/${catalog##*/}"
done
for runtime in "${oksa_src}"/vllm/runtime/*; do
  install -D -m 0644 "${runtime}" "${stage}/usr/share/puu/vllm/runtime/${runtime##*/}"
done
for unit in \
  puu-k3s-addons.service \
  puu-model-serving-labeler.service \
  puu-model-serving-labeler.timer \
  puu-vllm-planner.service \
  puu-vllm-planner.timer \
  puu-vllm-preseed.service \
  puu-postgresql.service; do
  install -D -m 0644 "${oksa_src}/systemd/${unit}" \
    "${stage}/usr/lib/systemd/system/${unit}"
done
install -D -m 0644 "${oksa_src}/systemd/postgresql.service.d/puu.conf" \
  "${stage}/usr/lib/systemd/system/postgresql.service.d/puu.conf"

rm -rf -- "${overlay_dir}"
mv -T "${stage}" "${overlay_dir}"
python3 "${state_helper}" save "${state_file}" --key "${key}" "${outputs[@]}"
