#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

if (($# < 7)); then
  echo "usage: $0 <lock_file> <tarball> <hash> <board_dir> <src_dir> <stamp_file> <base_patch> [extra_patches...]" >&2
  exit 1
fi

lock_file=$1
tarball=$2
hash=$3
board_dir=$4
src_dir=$5
stamp_file=$6
base_patch=$7
shift 7
extra_patches=("$@")

mkdir -p "$(dirname "$lock_file")"
exec 9>"$lock_file"
flock 9

if [[ -e "$stamp_file" ]]; then
  exit 0
fi

echo "$hash  $tarball" | sha256sum --status -c -

rm -rf "$board_dir"
mkdir -p "$src_dir"
tar -xf "$tarball" --strip-components=1 -C "$src_dir"

patch -d "$src_dir" -p1 -i "$base_patch"
for p in "${extra_patches[@]}"; do
  [[ -f "$p" ]] || continue
  patch -d "$src_dir" -p1 -i "$p"
done

mkdir -p "$(dirname "$stamp_file")"
touch "$stamp_file"
