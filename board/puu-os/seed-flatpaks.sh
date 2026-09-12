#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

: "${TARGET_DIR:?TARGET_DIR must be set by Buildroot}"
: "${DEST_DIR:?DEST_DIR must be set}"
: "${PUU_ARCH:?PUU_ARCH must be set (amd64 or arm64)}"

# shellcheck source=board/puu-os/lib.sh
. "${BASH_SOURCE%/*}/lib.sh"

manifest="${TARGET_DIR}/usr/share/puu-os/flatpaks.list"
remote="${TARGET_DIR}/usr/share/flatpak/remotes.d/flathub.flatpakrepo"
cache_root="${PUU_FLATPAK_CACHE_DIR:-${HOME}/.cache/puu-os/flatpak}"

init_puu_arch "${PUU_ARCH}"

[ -f "${manifest}" ] || exit 0
[ -f "${remote}" ] || { echo "missing Flatpak remote: ${remote}" >&2; exit 1; }
mapfile -t refs < <(sed -E '/^[[:space:]]*(#|$)/d; s/[[:space:]]+#.*$//' "${manifest}")
[ "${#refs[@]}" -gt 0 ] || exit 0

export FLATPAK_USER_DIR="${DEST_DIR}/var/lib/flatpak"
export FLATPAK_CONFIG_DIR="${TARGET_DIR}/etc/flatpak"
export FLATPAK_DATA_DIR="${TARGET_DIR}/usr/share/flatpak"
export XDG_CACHE_HOME="${cache_root}/${PUU_TARGET_ARCH}"
mkdir -p "${FLATPAK_USER_DIR}" "${XDG_CACHE_HOME}"

flatpak remote-add --user --if-not-exists flathub "${remote}"
flatpak install --user --noninteractive --assumeyes --or-update \
  --arch="${PUU_TARGET_ARCH}" flathub "${refs[@]}"
