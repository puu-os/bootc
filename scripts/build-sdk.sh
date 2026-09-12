#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

if (($# != 9)); then
  echo "usage: $0 <arch> <defconfig> <tarball> <hash> <sdk_build_dir> <sdk_install_root> <sdk_lock> <dl_dir> <epoch>" >&2
  exit 1
fi

arch=$1
defconfig=$2
tarball=$3
hash=$4
sdk_build_dir=$5
sdk_install_root=$6
sdk_lock=$7
dl_dir=$8
epoch=$9

mkdir -p "$(dirname "$sdk_lock")"
exec 9>"$sdk_lock"
flock 9

install_stamp="${sdk_install_root}/.stamp/${arch}/install"
if [[ -e "$install_stamp" ]]; then
  exit 0
fi

echo "$hash  $tarball" | sha256sum --status -c -

src_dir="${sdk_build_dir}/src/buildroot"
unpack_stamp="${sdk_build_dir}/.stamp/unpack"
if [[ ! -e "$unpack_stamp" ]]; then
  rm -rf "$src_dir"
  mkdir -p "$src_dir"
  tar -xf "$tarball" --strip-components=1 -C "$src_dir"
  mkdir -p "$(dirname "$unpack_stamp")"
  touch "$unpack_stamp"
fi

build_dir="${sdk_build_dir}/build/${arch}"
mkdir -p "$build_dir"

make_args=(
  -C "$src_dir" O="$build_dir"
  BR2_DL_DIR="$dl_dir"
  BR2_CCACHE_DIR="${CCACHE_DIR:-${HOME}/.cache/puu-os/ccache}"
  SOURCE_DATE_EPOCH="$epoch"
)

make "${make_args[@]}" defconfig DEFCONFIG="$(realpath "$defconfig")"
make "${make_args[@]}" sdk

shopt -s nullglob
tarballs=("$build_dir/images/"*_sdk-buildroot.tar.gz)
if ((${#tarballs[@]} == 0)); then
  echo "error: no SDK tarball found in $build_dir/images" >&2
  exit 1
fi
sdk_archive="${tarballs[0]}"
topdir="$(basename "$sdk_archive" .tar.gz)"

dest_dir="${sdk_install_root}/${arch}"
rm -rf "$dest_dir"
mkdir -p "$dest_dir"
tar -xzf "$sdk_archive" -C "$dest_dir"

relocate_script="${dest_dir}/${topdir}/relocate-sdk.sh"
if [[ ! -x "$relocate_script" ]]; then
  echo "error: relocate-sdk.sh not found or not executable in $dest_dir/$topdir" >&2
  exit 1
fi
"$relocate_script"

mkdir -p "$(dirname "$install_stamp")"
touch "$install_stamp"
