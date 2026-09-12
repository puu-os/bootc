#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

committed=0
check_tag=""

cleanup() {
  local status=$?
  if [[ -n "$check_tag" ]]; then
    git tag -d "$check_tag" >/dev/null 2>&1 || true
  fi
  if (( status != 0 && !committed )); then
    git restore -- Makefile 2>/dev/null || true
  fi
  return "$status"
}
trap cleanup EXIT

require_git_repo

next_ver="${1:-}"
[[ -n "$next_ver" ]] || die "usage: scripts/release.sh <next-version>"

[[ "$next_ver" =~ ^[0-9]+(\.[0-9]+)*$ ]] \
  || die "invalid version: $next_ver (expected N or N.N.N)"

require_clean_worktree "HEAD is detached; check out a branch before releasing"
branch="$(git symbolic-ref --short HEAD)"

[[ -z "$(git tag -l "$next_ver")" ]] || die "tag $next_ver already exists"

check_tag="puu-signing-check-$$"
git tag -s "$check_tag" -m "puu-os release signing check" \
  || die "cannot sign release tags; configure a signing key"
git tag -d "$check_tag" >/dev/null
check_tag=""

cur_ver="$(makefile_version)"
[[ -n "$cur_ver" ]] || die "cannot find PUU_VERSION in Makefile"
[[ "$cur_ver" =~ ^[0-9]+(\.[0-9]+)*$ ]] \
  || die "cannot parse PUU_VERSION: $cur_ver"

version_gt "$next_ver" "$cur_ver" \
  || die "$next_ver is not greater than current $cur_ver"

sed -i -E "s/^PUU_VERSION[[:space:]]*\?=.*/PUU_VERSION           ?= $next_ver/" Makefile
[[ "$(makefile_version)" == "$next_ver" ]] \
  || die "failed to update PUU_VERSION in Makefile"

range=()
if prev_tag="$(latest_release_tag)"; then
  range=("${prev_tag}..HEAD")
fi

log="$(git log --pretty=tformat:'- %s (%an)' --no-merges "${range[@]}")"

git commit -s -m "Bump the version to $next_ver" -- Makefile
committed=1

sob="Signed-off-by: $(git config user.name) <$(git config user.email)>"
printf '%s %s\n\n%s\n\n%s\n' "puu-os" "$next_ver" "$log" "$sob" | git tag -s "$next_ver" -F -

printf 'tagged %s\n' "$next_ver"
printf 'push the commit and tag: git push --atomic origin %q %q\n' \
  "HEAD:refs/heads/$branch" "refs/tags/$next_ver"
printf 'then run: make publish\n'
