#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

die() {
  printf '%s\n' "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

makefile_version() {
  awk '/^PUU_VERSION[[:space:]]*\?=/ { print $NF; exit }' Makefile
}

latest_release_tag() {
  local tag

  tag="$(git tag --merged HEAD --sort=-v:refname \
    | awk '/^[0-9]+(\.[0-9]+)*$/ { print; exit }')"
  [[ -n "$tag" ]] || return 1
  printf '%s\n' "$tag"
}

require_git_repo() {
  local repo_root
  repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || die "not inside a Git repository"
  cd "$repo_root" || die "cannot enter repository"
}

require_clean_worktree() {
  git symbolic-ref --quiet --short HEAD >/dev/null \
    || die "${1:-HEAD is detached}"
  [[ -z "$(git status --porcelain)" ]] || die "working directory is not clean"
}

version_gt() {
  local next=$1 cur=$2
  [[ "$next" != "$cur" ]] &&
    [[ "$(printf '%s\n%s\n' "$cur" "$next" | sort -V | tail -n1)" == "$next" ]]
}
