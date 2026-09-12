#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

git ls-files -z | (
  sh_files=()
  py_files=()
  while IFS= read -r -d '' file; do
    [ -f "$file" ] || continue
    shebang=
    IFS= read -r shebang < "$file" || true
    if printf '%s\n' "$shebang" | grep -Eq '^#!([^[:space:]]*/)?(env([[:space:]]+-S)?[[:space:]]+)?(a|ba|da|k)?sh([[:space:]]|$)'; then
      sh_files+=("$file")
    elif printf '%s\n' "$shebang" | grep -Eq '^#!([^[:space:]]*/)?(env([[:space:]]+-S)?[[:space:]]+)?python3?([[:space:]]|$)'; then
      py_files+=("$file")
    fi
  done

  if ((${#sh_files[@]} > 0)); then
    shellcheck -x "${sh_files[@]}"
  fi

  if ((${#py_files[@]} > 0)); then
    python3 -c '
import sys

status = 0
for path in sys.argv[1:]:
    with open(path, "rb") as handle:
        source = handle.read()
    try:
        compile(source, path, "exec")
    except SyntaxError as error:
        print(f"error: {path}:{error.lineno}: {error.msg}", file=sys.stderr)
        status = 1
sys.exit(status)
' "${py_files[@]}"
  fi
)

awk '
  /^[[:space:]]*chart:[[:space:]]+["\047]?oci:\/\// &&
  $0 !~ /@sha256:[0-9a-f]{64}["\047]?([[:space:]]+#.*)?[[:space:]]*$/ {
    printf "error: OCI Helm chart is not pinned by digest: %s:%d: %s\n", FILENAME, FNR, $0 > "/dev/stderr"
    invalid = 1
  }
  END { if (invalid) exit 1 }
' package/oksa-services/files/manifests/*.yaml
