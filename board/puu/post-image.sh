#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

: "${TARGET_DIR:?TARGET_DIR must be set by Buildroot}"
: "${BINARIES_DIR:?BINARIES_DIR must be set by Buildroot}"
: "${HOST_DIR:?HOST_DIR must be set by Buildroot}"
: "${BR2_EXTERNAL_PUU_PATH:?BR2_EXTERNAL_PUU_PATH must be set by Buildroot}"
export PUU_ARCH="${2:?arch argument must be set (amd64 or arm64)}"
: "${PUU_COSIGN_KEYS?PUU_COSIGN_KEYS must be set (empty for unsigned builds)}"
: "${SOURCE_DATE_EPOCH:?SOURCE_DATE_EPOCH must be set by Buildroot}"
export SOURCE_DATE_EPOCH TZ=UTC

# shellcheck source=scripts/image-lib.sh
. "${BR2_EXTERNAL_PUU_PATH}/scripts/image-lib.sh"

init_puu_arch "${PUU_ARCH}"
bootc_bin="${TARGET_DIR}/usr/bin/bootc"
bootc_ldso="${TARGET_DIR}/lib/${PUU_LDSO_NAME}"
bootc_libpath="${TARGET_DIR}/lib:${TARGET_DIR}/usr/lib"
if [ ! -x "${bootc_bin}" ]; then
  echo "warning: skipping bootc container lint: ${bootc_bin} missing" >&2
elif [ "$(uname -m)" = "${PUU_TARGET_ARCH}" ] && [ -x "${bootc_ldso}" ]; then
  "${bootc_ldso}" --library-path "${bootc_libpath}" "${bootc_bin}" \
    container lint --rootfs "${TARGET_DIR}"
else
  echo "warning: skipping bootc container lint: cannot run target bootc on host" >&2
fi

"${BR2_EXTERNAL_PUU_PATH}/scripts/image-live-rootfs.sh"
"${BR2_EXTERNAL_PUU_PATH}/scripts/image-sign.sh"
"${BR2_EXTERNAL_PUU_PATH}/scripts/image-live-iso.sh"
