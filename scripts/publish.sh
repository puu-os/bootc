#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026
# Publish a GitLab release from a signed tag.

set -euo pipefail

# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

GLAB="${GLAB:-glab}"

require_command git
require_command "$GLAB"
require_git_repo
require_clean_worktree \
  "HEAD is detached; check out the release branch before publishing"

version="${1:-}"
if [[ -z "$version" ]]; then
  version="$(latest_release_tag)" || die "no version tag reachable from HEAD"
fi
[[ "$version" =~ ^[0-9]+(\.[0-9]+)*$ ]] \
  || die "invalid version: $version (expected N or N.N.N)"

makefile_ver="$(makefile_version)"
[[ "$makefile_ver" == "$version" ]] \
  || die "Makefile PUU_VERSION $makefile_ver does not match $version"

tag_commit="$(git rev-parse "$version^{commit}" 2>/dev/null)" \
  || die "tag $version does not exist"
git verify-tag "$version" >/dev/null \
  || die "tag $version does not have a valid signature"
git merge-base --is-ancestor "$tag_commit" HEAD \
  || die "tag $version is not an ancestor of HEAD"

local_tag="$(git rev-parse "refs/tags/$version")"
remote_output="$(git ls-remote --refs origin "refs/tags/$version")" \
  || die "cannot query tag $version from origin"
remote_tag="${remote_output%%$'\t'*}"
[[ -n "$remote_tag" && "$remote_tag" == "$local_tag" ]] \
  || die "push tag $version to origin before publishing"

notes="$(mktemp)"
trap 'rm -f "$notes"' EXIT
git for-each-ref --format='%(contents:subject)%0a%0a%(contents:body)' \
  "refs/tags/$version" >"$notes"

"$GLAB" repo view >/dev/null 2>&1 || die "cannot access the GitLab project"
if "$GLAB" release view "$version" >/dev/null 2>&1; then
  printf 'GitLab release %s already exists\n' "$version"
else
  "$GLAB" release create "$version" \
    --name "puu $version" --notes-file "$notes"
fi

printf 'published puu %s\n' "$version"
