#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

: "${BINARIES_DIR:?BINARIES_DIR must be set by Buildroot}"
: "${HOST_DIR:?HOST_DIR must be set by Buildroot}"
: "${BR2_EXTERNAL_PUU_PATH:?BR2_EXTERNAL_PUU_PATH must be set by Buildroot}"
: "${PUU_ARCH:?PUU_ARCH must be set (amd64 or arm64)}"
: "${PUU_COSIGN_KEYS?PUU_COSIGN_KEYS must be set (empty for unsigned builds)}"
: "${SOURCE_DATE_EPOCH:?SOURCE_DATE_EPOCH must be set by Buildroot}"
export SOURCE_DATE_EPOCH TZ=UTC
export PATH="${HOST_DIR}/bin:${HOST_DIR}/sbin:${PATH}"

# shellcheck source=board/puu/lib.sh
. "${BASH_SOURCE%/*}/lib.sh"
init_puu_arch "${PUU_ARCH}"

state_helper="${BR2_EXTERNAL_PUU_PATH}/scripts/build-state.py"
state="${BINARIES_DIR}/.sign-image.state"
oci_source="${BINARIES_DIR}/rootfs-oci-latest-${PUU_ARCH}-linux.oci-image.tar"
image_ref=latest
puu_version="${PUU_VERSION:-1}"
target_imgref="${PUU_TARGET_IMGREF:-quay.io/puu/puu:${PUU_VARIANT:-gnome}}"
work=$(mktemp -d "${BINARIES_DIR}/sign-image.XXXXXX")
updated=()
committed=0
cleanup() {
  if [ "${committed}" = 0 ]; then
    for record in "${updated[@]}"; do
      existed="${record%%:*}"
      name="${record#*:}"
      rm -rf -- "${BINARIES_DIR:?}/${name:?}"
      if [ "${existed}" = 1 ]; then
        mv -T "${work}/old/${name}" "${BINARIES_DIR}/${name}"
      fi
    done
  fi
  rm -rf -- "${work}"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
mkdir -p "${work}/new" "${work}/old"

inputs=(--value "arch=${PUU_ARCH}" --value "epoch=${SOURCE_DATE_EPOCH}"
  --value "version=${puu_version}" --value "target_imgref=${target_imgref}")
for path in "${BASH_SOURCE[0]}" "${BASH_SOURCE%/*}/lib.sh" "${state_helper}" "${oci_source}"; do
  resolved=$(realpath -e "${path}")
  inputs+=(--path "${path}")
  [ "${path}" = "${resolved}" ] || inputs+=(--path "${resolved}")
done
signed=0
if split_cosign_keys; then
  signed=1
  mkdir -p "${work}/new/cosign"
  tool_path=$(command -v cosign)
  for path in "${tool_path}" "${HOST_DIR}/bin/skopeo"; do
    resolved=$(realpath -e "${path}")
    inputs+=(--path "${path}")
    [ "${path}" = "${resolved}" ] || inputs+=(--path "${resolved}")
  done
  for i in "${!cosign_key_paths[@]}"; do
    write_cosign_public_key "${cosign_key_paths[${i}]}" "${work}/new/cosign/key-${i}.pub"
    test -s "${work}/new/cosign/key-${i}.pub"
    # Only public identities enter the cache key, never private keys or passwords.
    public_key=$(cat "${work}/new/cosign/key-${i}.pub")
    inputs+=(--value "public-key-${i}=${public_key}")
  done
fi
inputs+=(--value "signed=${signed}")
key=$(python3 "${state_helper}" key "${inputs[@]}")
outputs=(--metadata-path "${BINARIES_DIR}/image.tar" --path "${BINARIES_DIR}/image.manifest"
  --path "${BINARIES_DIR}/image-${puu_version}.tar"
  --path "${BINARIES_DIR}/image-${puu_version}.manifest")
if [ "${signed}" = 1 ]; then
  outputs+=(--path "${BINARIES_DIR}/cosign" --metadata-path "${BINARIES_DIR}/image.signed")
fi
if python3 "${state_helper}" check "${state}" --key "${key}" "${outputs[@]}"; then
  if [ ! -e "${BINARIES_DIR}/cosign.pub" ] && [ ! -L "${BINARIES_DIR}/cosign.pub" ] &&
    { [ "${signed}" = 1 ] || {
      [ ! -e "${BINARIES_DIR}/cosign" ] && [ ! -L "${BINARIES_DIR}/cosign" ] &&
      [ ! -e "${BINARIES_DIR}/image.signed" ] && [ ! -L "${BINARIES_DIR}/image.signed" ];
    }; }; then
    echo "image signing is up to date"
    exit 0
  fi
else
  status=$?
  [ "${status}" = 1 ] || exit "${status}"
fi

cp --reflink=auto "${oci_source}" "${work}/new/image.tar"
oci_digest=$(oci_archive_digest "${work}/new/image.tar")
if [ "${signed}" = 1 ]; then
  # The temporary directory is private (mktemp mode 0700); no secret is cached.
  printf '%s' "${PUU_COSIGN_PASSWORD:-}" > "${work}/passphrase"
  source_ref="oci-archive:${work}/new/image.tar:${image_ref}"
  for i in "${!cosign_key_paths[@]}"; do
    signed_tmp="${work}/signed-${i}"
    mkdir "${signed_tmp}"
    "${HOST_DIR}/bin/skopeo" --insecure-policy copy \
      --sign-by-sigstore-private-key "${cosign_key_paths[${i}]}" \
      --sign-identity "${target_imgref}" \
      --sign-passphrase-file "${work}/passphrase" \
      "${source_ref}" "dir:${signed_tmp}"
    source_ref="dir:${signed_tmp}"
  done
  test -s "${signed_tmp}/manifest.json"
  mv "${signed_tmp}" "${work}/new/image.signed"
  rm -f "${work}/passphrase"
else
  echo "warning: producing an unsigned image" >&2
fi
python3 -c '
import json, sys
print(json.dumps(dict(zip(("imgref", "target_imgref", "digest", "version"), sys.argv[1:])),
                 separators=(",", ":")))
' "oci:${image_ref}" "${target_imgref}" "${oci_digest}" "${puu_version}" \
  > "${work}/new/image.manifest"
ln -s image.tar "${work}/new/image-${puu_version}.tar"
ln -s image.manifest "${work}/new/image-${puu_version}.manifest"

# Keep previous outputs until every publication and the state save has succeeded.
for name in image.tar image.manifest cosign image.signed cosign.pub \
  "image-${puu_version}.tar" "image-${puu_version}.manifest"; do
  existed=0
  if [ -e "${BINARIES_DIR}/${name}" ] || [ -L "${BINARIES_DIR}/${name}" ]; then
    existed=1
    if [ -d "${BINARIES_DIR}/${name}" ] && [ ! -L "${BINARIES_DIR}/${name}" ]; then
      mv -T "${BINARIES_DIR}/${name}" "${work}/old/${name}"
    else
      ln -P "${BINARIES_DIR}/${name}" "${work}/old/${name}"
    fi
  fi
  updated+=("${existed}:${name}")
  if [ -e "${work}/new/${name}" ] || [ -L "${work}/new/${name}" ]; then
    mv -fT "${work}/new/${name}" "${BINARIES_DIR}/${name}"
  else
    rm -f "${BINARIES_DIR}/${name}"
  fi
done
python3 "${state_helper}" save "${state}" --key "${key}" "${outputs[@]}"
committed=1
