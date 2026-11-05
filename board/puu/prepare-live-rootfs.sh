#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

: "${TARGET_DIR:?TARGET_DIR must be set by Buildroot}"
: "${BINARIES_DIR:?BINARIES_DIR must be set by Buildroot}"
: "${HOST_DIR:?HOST_DIR must be set by Buildroot}"
: "${BR2_EXTERNAL_PUU_PATH:?BR2_EXTERNAL_PUU_PATH must be set by Buildroot}"
: "${PUU_ARCH:?PUU_ARCH must be set (amd64 or arm64)}"
: "${SOURCE_DATE_EPOCH:?SOURCE_DATE_EPOCH must be set by Buildroot}"
export SOURCE_DATE_EPOCH TZ=UTC
export PATH="${HOST_DIR}/bin:${HOST_DIR}/sbin:${PATH}"

script_dir="${BASH_SOURCE%/*}"
# shellcheck source=board/puu/lib.sh
. "${script_dir}/lib.sh"
init_puu_arch "${PUU_ARCH}"
state_helper="${BR2_EXTERNAL_PUU_PATH}/scripts/build-state.py"
state="${BINARIES_DIR}/.prepare-live-rootfs.state"
rootfs="${BINARIES_DIR}/rootfs.squashfs"
oci_source="${BINARIES_DIR}/rootfs-oci-latest-${PUU_ARCH}-linux.oci-image.tar"
output="${BINARIES_DIR}/rootfs-live.squashfs"
machine_src="${BR2_EXTERNAL_PUU_PATH}/overlay/usr/share/puu/live/machine.yaml"
oci_digest=$(oci_archive_digest "${oci_source}")

flatpak_key=""
case "${PUU_FLATPAK_PRESEED:-1}" in
  1) flatpak_key=$("${script_dir}/seed-flatpaks.sh" --key) ;;
  0) ;;
  *) echo "PUU_FLATPAK_PRESEED must be 0 or 1" >&2; exit 1 ;;
esac

inputs=(--value "arch=${PUU_ARCH}" --value "epoch=${SOURCE_DATE_EPOCH}"
  --value "oci_digest=${oci_digest}" --value "preseed=${PUU_FLATPAK_PRESEED:-1}"
  --value "flatpak_snapshot=${flatpak_key}")
for path in "${BASH_SOURCE[0]}" "${script_dir}/lib.sh" "${state_helper}" \
  "${rootfs}" "${machine_src}" "${HOST_DIR}/bin/fakeroot" "${HOST_DIR}/bin/faked" \
  "${HOST_DIR}/bin/unsquashfs" "${HOST_DIR}/bin/mksquashfs"; do
  resolved=$(realpath -e "${path}")
  inputs+=(--path "${path}")
  [ "${path}" = "${resolved}" ] || inputs+=(--path "${resolved}")
done
if [ "${PUU_FLATPAK_PRESEED:-1}" = 1 ]; then
  inputs+=(--path "${script_dir}/seed-flatpaks.sh" --path "${script_dir}/flatpak-snapshot.sh")
  for path in "${TARGET_DIR}/usr/share/puu/flatpaks.list" "${TARGET_DIR}/etc/flatpak" \
    "${TARGET_DIR}/usr/share/flatpak/remotes.d"; do
    if [ -e "${path}" ] || [ -L "${path}" ]; then
      inputs+=(--path "${path}")
    else
      inputs+=(--value "missing=${path}")
    fi
  done
fi
key=$(python3 "${state_helper}" key "${inputs[@]}")
if python3 "${state_helper}" check "${state}" --key "${key}" --metadata-path "${output}"; then
  echo "live rootfs is up to date"
  exit 0
else
  status=$?
  [ "${status}" = 1 ] || exit "${status}"
fi

work=$(mktemp -d "${BINARIES_DIR}/rootfs-live.XXXXXX")
published=0
committed=0
cleanup() {
  if [ "${published}" = 1 ] && [ "${committed}" = 0 ]; then
    if [ -e "${work}/previous" ] || [ -L "${work}/previous" ]; then
      mv -fT "${work}/previous" "${output}"
    else
      rm -f "${output}"
    fi
  fi
  rm -rf -- "${work}"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

flatpak_root=""
if [ "${PUU_FLATPAK_PRESEED:-1}" = 1 ]; then
  flatpak_root="${work}/flatpak"
  mkdir -p "${flatpak_root}"
  DEST_DIR="${flatpak_root}" "${script_dir}/seed-flatpaks.sh"
  copied_key=$("${script_dir}/seed-flatpaks.sh" --key)
  if [ "${copied_key}" != "${flatpak_key}" ]; then
    echo "Flatpak snapshot changed during live rootfs preparation; rerun the image stage" >&2
    exit 1
  fi
fi
mkdir -p "${work}/root"

# Extraction, ownership changes and packing must share one fakeroot session.
# shellcheck disable=SC2016
"${HOST_DIR}/bin/fakeroot" -- bash -c '
  set -euo pipefail
  unsquashfs="$1"
  mksquashfs="$2"
  rootfs="$3"
  stage="$4"
  output="$5"
  digest="$6"
  source_date_epoch="$7"
  machine_src="$8"
  flatpak_root="$9"

  "$unsquashfs" -q -d "$stage" "$rootfs"
  if [ -n "$flatpak_root" ] && [ -d "$flatpak_root/var/lib/flatpak" ]; then
    mkdir -p "$stage/var/lib"
    rm -rf "$stage/var/lib/flatpak"
    mv "$flatpak_root/var/lib/flatpak" "$stage/var/lib/flatpak"
    chown -hR 0:0 "$stage/var/lib/flatpak"
  fi
  install -D -m 0644 "$machine_src" "$stage/usr/share/puu/machine.yaml"
  printf "\nimage:\n  expectedDigest: %s\n  sourceLabel: null\n" "$digest" >> "$stage/usr/share/puu/machine.yaml"
  install -d -m 0755 "$stage/etc/sudoers.d"
  printf "PUU_INSTALLER_CONFIG=/usr/share/puu/machine.yaml\n" > "$stage/etc/environment"
  echo "Defaults env_file=/etc/environment" > "$stage/etc/sudoers.d/30-puu-installer"
  chmod 0644 "$stage/etc/environment"
  chmod 0440 "$stage/etc/sudoers.d/30-puu-installer"
  touch -d "@${source_date_epoch}" \
    "$stage/usr/share/puu/machine.yaml" \
    "$stage/etc/environment" \
    "$stage/etc/sudoers.d/30-puu-installer"
  SOURCE_DATE_EPOCH="$source_date_epoch" "$mksquashfs" "$stage" "$output" -noappend -b 128K -comp zstd
' _ "${HOST_DIR}/bin/unsquashfs" "${HOST_DIR}/bin/mksquashfs" \
  "${rootfs}" "${work}/root" "${work}/rootfs.squashfs" "${oci_digest}" \
  "${SOURCE_DATE_EPOCH}" "${machine_src}" "${flatpak_root}"

test -s "${work}/rootfs.squashfs"
if [ -e "${output}" ] || [ -L "${output}" ]; then
  ln -P "${output}" "${work}/previous"
fi
published=1
mv -fT "${work}/rootfs.squashfs" "${output}"
python3 "${state_helper}" save "${state}" --key "${key}" --metadata-path "${output}"
committed=1
