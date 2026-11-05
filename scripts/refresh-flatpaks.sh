#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

# shellcheck source=board/puu/flatpak-snapshot.sh
. "${BASH_SOURCE%/*}/../board/puu/flatpak-snapshot.sh"
flatpak_snapshot_init
if [ "${#refs[@]}" -eq 0 ]; then
  echo 'No Flatpaks configured in the build target.'
  exit 0
fi

mkdir -p "${cache_root}/.locks" "${cache_root}/cache/${PUU_TARGET_ARCH}"
exec 9> "${cache_root}/.locks/${PUU_TARGET_ARCH}.${snapshot_input_key}"
flock -x 9
mkdir -p "${snapshot_root}/generations"
staging=$(mktemp -d "${snapshot_root}/.staging.XXXXXX")
publish_tmp=""
cleanup() {
  rm -rf -- ${staging:+"${staging}"} ${publish_tmp:+"${publish_tmp}"}
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
export FLATPAK_USER_DIR="${staging}/flatpak"
export XDG_CACHE_HOME="${cache_root}/cache/${PUU_TARGET_ARCH}"
mkdir -p "${FLATPAK_USER_DIR}"

flatpak remote-add --user --if-not-exists flathub "${remote}"
flatpak install --user --noninteractive --assumeyes --or-update \
  --arch="${PUU_TARGET_ARCH}" flathub "${refs[@]}"

installed_refs=$(flatpak list --user --all --columns=ref)
[ -n "${installed_refs}" ] || { echo 'Flatpak refresh installed no refs' >&2; exit 1; }
while IFS= read -r ref; do
  [[ "${ref}" =~ ^(app|runtime)/[^/[:space:]]+/${PUU_TARGET_ARCH}/[^/[:space:]]+$ ]] \
    || { echo "invalid installed Flatpak ref: ${ref}" >&2; exit 1; }
  commit=$(flatpak info --user --show-commit "${ref}")
  [[ "${commit}" =~ ^[0-9a-f]{64}$ ]] \
    || { echo "invalid Flatpak commit for ${ref}: ${commit}" >&2; exit 1; }
  printf '%s\t%s\n' "${ref}" "${commit}"
done <<< "${installed_refs}" > "${staging}/refs"
LC_ALL=C sort -o "${staging}/refs" "${staging}/refs"
[ -d "${staging}/flatpak/repo" ] || { echo 'Flatpak refresh created no repository' >&2; exit 1; }
[ "$(flatpak_snapshot_input_key "${snapshot_source}")" = "${snapshot_input_key}" ] \
  || flatpak_snapshot_error 'Flatpak source configuration changed during refresh'
snapshot_generation_key=$(flatpak_snapshot_generation_key "${staging}")
[[ "${snapshot_generation_key}" =~ ^[0-9a-f]{64}$ ]] \
  || { echo 'invalid Flatpak snapshot generation key' >&2; exit 1; }

# Retain old generations so readers can finish without taking a writable lock.
generation="generations/${snapshot_generation_key}.${staging##*.}"
mv -T -- "${staging}" "${snapshot_root}/${generation}"
staging="${snapshot_root}/${generation}"
(
  cd "${staging}"
  python3 "${snapshot_state_tool}" save state.json \
    --key "${snapshot_input_key}:${snapshot_generation_key}" \
    --path refs --metadata-path flatpak
  python3 "${snapshot_state_tool}" check state.json \
    --key "${snapshot_input_key}:${snapshot_generation_key}" \
    --path refs --metadata-path flatpak
)
# Once state.json is verified, keep the published generation directory on exit.
staging=""
publish_tmp=$(mktemp -d "${snapshot_root}/.publish.XXXXXX")
ln -s "${generation}" "${publish_tmp}/current"
mv -Tf -- "${publish_tmp}/current" "${snapshot_root}/current"
printf 'Published Flatpak snapshot %s\n' "${snapshot_generation_key}"
