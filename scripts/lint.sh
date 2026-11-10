#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

git ls-files --cached --others --exclude-standard -z | (
  sh_files=()
  py_files=()
  br2_files=()
  oksa_files=()
  while IFS= read -r -d '' file; do
    [[ -f "$file" && ! -L "$file" ]] || continue
    case "$file" in
      br2-external/*) br2_files+=("$file") ;;
      oksa/*) oksa_files+=("$file") ;;
    esac
    shebang=
    IFS= read -r shebang < "$file" || true
    if printf '%s\n' "$shebang" | grep -Eq '^#!([^[:space:]]*/)?(env([[:space:]]+-S)?[[:space:]]+)?(a|ba|da|k)?sh([[:space:]]|$)'; then
      sh_files+=("$file")
    elif [[ "$file" == *.py ]] || printf '%s\n' "$shebang" | grep -Eq '^#!([^[:space:]]*/)?(env([[:space:]]+-S)?[[:space:]]+)?python3?([[:space:]]|$)'; then
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

  if ((${#br2_files[@]} > 0)) && grep -InE '(\$\{BASH_SOURCE%\*/\}/\.\.|\$\(BR2_EXTERNAL_PUU_PATH\)/(\.\.|scripts|oksa)|\b(\.\./)+(scripts|oksa)\b)' "${br2_files[@]}" >&2; then
    echo "error: br2-external must not reference files outside br2-external/" >&2
    exit 1
  fi

  if ((${#oksa_files[@]} > 0)) && grep -InE '(\$\{BASH_SOURCE%\*/\}/\.\.|\b(\.\./)+(br2-external|scripts)\b)' "${oksa_files[@]}" >&2; then
    echo "error: oksa must not reference files outside oksa/" >&2
    exit 1
  fi
)

awk '
  /^[[:space:]]*chart:[[:space:]]+["\047]?oci:\/\// &&
  $0 !~ /@sha256:[0-9a-f]{64}["\047]?([[:space:]]+#.*)?[[:space:]]*$/ {
    printf "error: OCI Helm chart is not pinned by digest: %s:%d: %s\n", FILENAME, FNR, $0 > "/dev/stderr"
    invalid = 1
  }
  END { if (invalid) exit 1 }
' oksa/manifests/*.yaml
