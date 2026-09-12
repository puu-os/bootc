#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

if (($# != 2)); then
  echo "usage: $0 <image_path> <device>" >&2
  exit 1
fi

image_path=$1
device=$2

if [[ ! -f "$image_path" ]]; then
  echo "error: image not found: $image_path" >&2
  exit 1
fi

if [[ ! -b "$device" ]]; then
  echo "error: $device is not a block device" >&2
  exit 1
fi

if [[ $(uname -s) == Darwin ]]; then
  diskutil unmountDisk "$device"
  sudo dd if="$image_path" of="$device" bs=1m
  sync
else
  sudo dd if="$image_path" of="$device" bs=1M status=progress oflag=sync
fi
