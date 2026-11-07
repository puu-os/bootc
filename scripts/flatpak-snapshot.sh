#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

# shellcheck source=scripts/image-lib.sh
. "${BASH_SOURCE%/*}/image-lib.sh"

flatpak_snapshot_error() {
  echo "${*}; run make flatpak-update BOARD=${BOARD:-puu_${PUU_ARCH:-<board>}}" >&2
  exit 1
}

flatpak_snapshot_input_key() (
  cd "$1" || exit 1
  local path
  local -a args=(--value format=flatpak-snapshot-v1 --value "arch=${PUU_TARGET_ARCH}")
  # Package-installed triggers are executable code, not remote configuration.
  for path in usr/share/puu/flatpaks.list etc/flatpak usr/share/flatpak/remotes.d; do
    if [ -e "${path}" ] || [ -L "${path}" ]; then
      args+=(--path "${path}")
    else
      args+=(--value "absent=${path}")
    fi
  done
  python3 "${snapshot_state_tool}" key "${args[@]}"
)

flatpak_snapshot_init() {
  : "${PUU_ARCH:?PUU_ARCH must be set (amd64 or arm64)}"
  init_puu_arch "${PUU_ARCH}"
  local project manifest_refs
  project="${BR2_EXTERNAL_PUU_PATH:-${BASH_SOURCE%/*}/..}"
  project=$(cd "${project}" && pwd)
  snapshot_state_tool="${project}/scripts/state.py"
  snapshot_source="${PUU_FLATPAK_SOURCE_DIR:-${project}/overlay}"
  [ -d "${snapshot_source}" ] || flatpak_snapshot_error "missing Flatpak source: ${snapshot_source}"
  snapshot_source=$(cd "${snapshot_source}" && pwd)
  manifest="${snapshot_source}/usr/share/puu/flatpaks.list"
  remote="${snapshot_source}/usr/share/flatpak/remotes.d/flathub.flatpakrepo"
  export FLATPAK_CONFIG_DIR="${snapshot_source}/etc/flatpak"
  export FLATPAK_DATA_DIR="${snapshot_source}/usr/share/flatpak"
  cache_root="${PUU_FLATPAK_CACHE_DIR:-${HOME}/.cache/puu/flatpak}"
  # Absolute paths keep staging and publication independent of later cd calls.
  cache_root=$(realpath -m -- "${cache_root}")
  [ -f "${manifest}" ] || flatpak_snapshot_error "missing Flatpak manifest: ${manifest}"
  manifest_refs=$(sed -E '/^[[:space:]]*(#|$)/d; s/[[:space:]]+#.*$//' "${manifest}")
  refs=()
  if [ -n "${manifest_refs}" ]; then
    mapfile -t refs <<< "${manifest_refs}"
  fi
  if [ "${#refs[@]}" -gt 0 ] && [ ! -f "${remote}" ]; then
    flatpak_snapshot_error "missing Flatpak remote: ${remote}"
  fi
  snapshot_input_key=$(flatpak_snapshot_input_key "${snapshot_source}")
  [[ "${snapshot_input_key}" =~ ^[0-9a-f]{64}$ ]] || {
    echo 'invalid Flatpak snapshot input key' >&2
    exit 1
  }
  snapshot_root="${cache_root}/${PUU_TARGET_ARCH}/${snapshot_input_key}"
}

flatpak_snapshot_check_target() {
  : "${TARGET_DIR:?TARGET_DIR must be set to the actual Buildroot target}"
  [ -d "${TARGET_DIR}" ] || {
    echo "missing build target: ${TARGET_DIR}; build BOARD=${BOARD:-puu_${PUU_ARCH}} before seeding Flatpaks" >&2
    exit 1
  }
  local target_key
  target_key=$(flatpak_snapshot_input_key "${TARGET_DIR}")
  if [ "${target_key}" != "${snapshot_input_key}" ]; then
    flatpak_snapshot_error "Flatpak target configuration differs from source ${snapshot_source}; rebuild the target, or select PUU_FLATPAK_SOURCE_DIR=${TARGET_DIR} for both refresh and build"
  fi
}

flatpak_snapshot_generation_key() (
  cd "$1" || exit 1
  python3 "${snapshot_state_tool}" key --value "input=${snapshot_input_key}" \
    --path flatpak --path refs
)

flatpak_snapshot_select() {
  local generation
  generation=$(readlink -- "${snapshot_root}/current") \
    || flatpak_snapshot_error "missing Flatpak snapshot for ${PUU_TARGET_ARCH}"
  [[ "${generation}" =~ ^generations/([0-9a-f]{64})\.[a-zA-Z0-9]+$ ]] \
    || flatpak_snapshot_error 'invalid Flatpak snapshot generation'
  snapshot_generation_key="${BASH_REMATCH[1]}"
  snapshot_dir="${snapshot_root}/${generation}"
  if [ ! -d "${snapshot_dir}/flatpak/repo" ] || [ ! -s "${snapshot_dir}/refs" ]; then
    flatpak_snapshot_error 'incomplete Flatpak snapshot'
  fi
  if ! (
    cd "${snapshot_dir}" || exit 1
    python3 "${snapshot_state_tool}" check state.json \
      --key "${snapshot_input_key}:${snapshot_generation_key}" \
      --path refs --metadata-path flatpak
  ); then
    flatpak_snapshot_error 'Flatpak snapshot integrity check failed'
  fi
}
