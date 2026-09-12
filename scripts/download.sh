#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

if (($# != 3)); then
  echo "usage: $0 <url> <sha256> <destination>" >&2
  exit 1
fi

url=$1
expected_sha=$2
destination=$3

if [[ -f "$destination" ]] && echo "$expected_sha  $destination" | sha256sum --status -c - 2>/dev/null; then
  exit 0
fi

mkdir -p "$(dirname "$destination")"
partial=$(mktemp "${destination}.partial.XXXXXX")
trap 'rm -f "$partial"' EXIT

curl -fL --retry 3 --connect-timeout 10 --progress-bar -o "$partial" "$url"
echo "$expected_sha  $partial" | sha256sum --status -c -
mv "$partial" "$destination"
trap - EXIT
