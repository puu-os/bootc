#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

case "${1:-}" in
  '') : "${DEST_DIR:?DEST_DIR must be set}" ;;
  --key) ;;
  *) echo "usage: $0 [--key]" >&2; exit 1 ;;
esac
[ "$#" -le 1 ] || { echo "usage: $0 [--key]" >&2; exit 1; }

# shellcheck source=board/puu/flatpak-snapshot.sh
. "${BASH_SOURCE%/*}/flatpak-snapshot.sh"
flatpak_snapshot_init
if [ "${1:-}" != --key ]; then
  flatpak_snapshot_check_target
fi

if [ "${#refs[@]}" -eq 0 ]; then
  if [ "${1:-}" = --key ]; then
    printf '%s\n' "${snapshot_input_key}"
  fi
  exit 0
fi

flatpak_snapshot_select
if [ "${1:-}" = --key ]; then
  printf '%s\n' "${snapshot_generation_key}"
  exit 0
fi

mkdir -p "${DEST_DIR}/var/lib"
if [ -e "${DEST_DIR}/var/lib/flatpak" ] || [ -L "${DEST_DIR}/var/lib/flatpak" ]; then
  echo "Flatpak seed destination already exists: ${DEST_DIR}/var/lib/flatpak" >&2
  exit 1
fi
cp -a -- "${snapshot_dir}/flatpak" "${DEST_DIR}/var/lib/flatpak"
