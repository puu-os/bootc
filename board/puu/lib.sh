#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

cosign_key_paths=()

kernel_version() {
  local kver
  kver=$(find "${TARGET_DIR}/usr/lib/modules" -mindepth 1 -maxdepth 1 -type d \
    | sort -V | tail -n1)
  test -n "$kver"
  basename "$kver"
}

split_cosign_keys() {
  cosign_key_paths=()
  local keys="${PUU_COSIGN_KEYS:-}"
  if [ -z "${keys}" ]; then
    return 1
  fi
  if [[ "${keys}" == :* || "${keys}" == *: || "${keys}" == *::* ]]; then
    echo "PUU_COSIGN_KEYS contains an empty entry" >&2
    exit 1
  fi
  IFS=: read -r -a cosign_key_paths <<< "${keys}"
}

write_cosign_public_key() {
  local key=$1 output=$2
  test -f "${key}" \
    || { echo "cosign key not found: ${key}" >&2; exit 1; }

  COSIGN_PASSWORD="${PUU_COSIGN_PASSWORD:-}" \
    cosign public-key --key "${key}" > "${output}"
  chmod 0644 "${output}"
}

init_puu_arch() {
  local arch="${1:-${PUU_ARCH:-}}"
  case "${arch}" in
    amd64)
      PUU_TARGET_ARCH=x86_64
      PUU_KERNEL_IMAGE=bzImage
      PUU_SERIAL_TTY=ttyS0
      PUU_EFI_BOOT_NAME=BOOTX64.EFI
      PUU_EFI_SDBOOT_NAME=systemd-bootx64.efi
      PUU_EFI_STUB_NAME=linuxx64.efi.stub
      PUU_LDSO_NAME=ld-linux-x86-64.so.2
      PUU_CMDLINE_EXTRA=" console=ttyS0,115200n8"
      ;;
    arm64)
      PUU_TARGET_ARCH=aarch64
      PUU_KERNEL_IMAGE=Image
      PUU_SERIAL_TTY=
      PUU_EFI_BOOT_NAME=BOOTAA64.EFI
      PUU_EFI_SDBOOT_NAME=systemd-bootaa64.efi
      PUU_EFI_STUB_NAME=linuxaa64.efi.stub
      PUU_LDSO_NAME=ld-linux-aarch64.so.1
      PUU_CMDLINE_EXTRA=" cma=256M plymouth.ignore-udev"
      ;;
    *)
      echo "unknown PUU_ARCH: ${arch}" >&2
      exit 1
      ;;
  esac
}
