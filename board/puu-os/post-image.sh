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

# shellcheck source=board/puu-os/lib.sh
. "${BASH_SOURCE%/*}/lib.sh"

init_puu_arch "${PUU_ARCH}"
bootc_ldso="${TARGET_DIR}/lib/${PUU_LDSO_NAME}"
stub="${TARGET_DIR}/usr/lib/systemd/boot/efi/${PUU_EFI_STUB_NAME}"
sdboot="${TARGET_DIR}/usr/lib/systemd/boot/efi/${PUU_EFI_SDBOOT_NAME}"
kernel="${BINARIES_DIR}/${PUU_KERNEL_IMAGE}"
output_img="puu_${PUU_ARCH}.img"

puu_variant="${PUU_VARIANT:-gnome}"
puu_version="${PUU_VERSION:-1}"
cleanup_paths=()
cleanup() {
  if ((${#cleanup_paths[@]})); then
    rm -rf -- "${cleanup_paths[@]}"
  fi
}
trap cleanup EXIT

embed_live_image_digest() {
  local rootfs_stage rootfs_tmp
  local machine_src flatpak_root=""

  machine_src="${BR2_EXTERNAL_PUU_PATH}/overlay/usr/share/puu-os/live/machine.yaml"
  test -f "${machine_src}"


  case "${PUU_FLATPAK_PRESEED:-1}" in
    1)
      flatpak_root=$(mktemp -d "${BINARIES_DIR}/flatpak.XXXXXX")
      cleanup_paths+=("${flatpak_root}")
      DEST_DIR="${flatpak_root}" "${BASH_SOURCE%/*}/seed-flatpaks.sh"
      ;;
    0) ;;
    *)
      echo "PUU_FLATPAK_PRESEED must be 0 or 1" >&2
      exit 1
      ;;
  esac

  rootfs_stage=$(mktemp -d "${BINARIES_DIR}/rootfs.XXXXXX")
  cleanup_paths+=("${rootfs_stage}")
  rootfs_tmp="${rootfs_squashfs}.tmp"
  cleanup_paths+=("${rootfs_tmp}")
  rm -f "${rootfs_tmp}"

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
    install -D -m 0644 "$machine_src" "$stage/usr/share/puu-os/machine.yaml"
    printf "\nimage:\n  expectedDigest: %s\n  sourceLabel: null\n" "$digest" >> "$stage/usr/share/puu-os/machine.yaml"
    install -d -m 0755 "$stage/etc/sudoers.d"
    printf "PUU_INSTALLER_CONFIG=/usr/share/puu-os/machine.yaml\n" \
      > "$stage/etc/environment"
    echo "Defaults env_file=/etc/environment" > "$stage/etc/sudoers.d/30-puu-installer"
    chmod 0644 "$stage/etc/environment"
    chmod 0440 "$stage/etc/sudoers.d/30-puu-installer"
    touch -d "@${source_date_epoch}" \
      "$stage/usr/share/puu-os/machine.yaml" \
      "$stage/etc/environment" \
      "$stage/etc/sudoers.d/30-puu-installer"
    SOURCE_DATE_EPOCH="$source_date_epoch" "$mksquashfs" "$stage" "$output" -noappend -b 128K -comp zstd
  ' _ "${HOST_DIR}/bin/unsquashfs" "${HOST_DIR}/bin/mksquashfs" \
    "${rootfs_squashfs}" "${rootfs_stage}" "${rootfs_tmp}" "${oci_digest}" \
    "${SOURCE_DATE_EPOCH}" "${machine_src}" "${flatpak_root}"
  rm -rf "${rootfs_stage}" ${flatpak_root:+"${flatpak_root}"}
  mv "${rootfs_tmp}" "${rootfs_squashfs}"
}

oci_archive="${BINARIES_DIR}/image.tar"
mv "${BINARIES_DIR}/rootfs-oci-latest-${PUU_ARCH}-linux.oci-image.tar" "${oci_archive}"
rootfs_squashfs="${BINARIES_DIR}/rootfs.squashfs"
image_ref="latest"
target_imgref="${PUU_TARGET_IMGREF:-quay.io/puu-os/${puu_variant}:${puu_version}}"

test -f "${oci_archive}"
index_json=$(tar -xOf "${oci_archive}" index.json | tr -d '[:space:]')
oci_digest=${index_json#*'"digest":"'}
oci_digest=${oci_digest%%'"'*}
if [[ ! "${oci_digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "invalid oci digest in ${oci_archive}" >&2
  exit 1
fi

bootc_bin="${TARGET_DIR}/usr/bin/bootc"
bootc_libpath="${TARGET_DIR}/lib:${TARGET_DIR}/usr/lib"
if [ ! -x "${bootc_bin}" ]; then
  echo "warning: skipping bootc container lint: ${bootc_bin} missing" >&2
elif [ "$(uname -m)" = "${PUU_TARGET_ARCH}" ] && [ -x "${bootc_ldso}" ]; then
  "${bootc_ldso}" --library-path "${bootc_libpath}" "${bootc_bin}" \
    container lint --rootfs "${TARGET_DIR}"
else
  echo "warning: skipping bootc container lint: cannot run target bootc on host" >&2
fi
embed_live_image_digest

pubkey_dir="${BINARIES_DIR}/cosign"
signed_dir="${BINARIES_DIR}/image.signed"
rm -f "${BINARIES_DIR}/cosign.pub"
rm -rf "${pubkey_dir}" "${signed_dir}"
if split_cosign_keys; then
  mkdir -p "${pubkey_dir}"

  passfile="$(mktemp)"
  cleanup_paths+=("${passfile}")
  printf '%s' "${PUU_COSIGN_PASSWORD:-}" > "${passfile}"

  source_ref="oci-archive:${oci_archive}:${image_ref}"
  for i in "${!cosign_key_paths[@]}"; do
    cosign_key="${cosign_key_paths[${i}]}"
    write_cosign_public_key "${cosign_key}" "${pubkey_dir}/key-${i}.pub"

    signed_tmp="$(mktemp -d "${BINARIES_DIR}/image.signed.XXXXXX")"
    cleanup_paths+=("${signed_tmp}")
    "${HOST_DIR}/bin/skopeo" --insecure-policy copy \
      --sign-by-sigstore-private-key "${cosign_key}" \
      --sign-identity "${target_imgref}" \
      --sign-passphrase-file "${passfile}" \
      "${source_ref}" \
      "dir:${signed_tmp}"
    rm -rf "${signed_dir}"
    mv "${signed_tmp}" "${signed_dir}"
    source_ref="dir:${signed_dir}"
  done

  rm -f "${passfile}"
else
  echo "warning: producing an unsigned image" >&2
fi

printf '{"imgref":"oci:%s","target_imgref":"%s","digest":"%s","version":"%s"}\n' \
  "${image_ref}" "${target_imgref}" "${oci_digest}" "${puu_version}" \
  > "${BINARIES_DIR}/image.manifest"

kver=$(kernel_version)

initrd="${TARGET_DIR}/usr/lib/modules/${kver}/initramfs.img"
osrel="${TARGET_DIR}/usr/lib/os-release"
uki_name="puu.efi"

efi_part="${BINARIES_DIR}/efi-part"
cleanup_paths+=("${efi_part}")
uki="${efi_part}/EFI/Linux/${uki_name}"
rm -rf "${efi_part}"
mkdir -p "${efi_part}"/{EFI/{BOOT,Linux,systemd},loader}

genimage_tmp=$(mktemp -d "${BINARIES_DIR}/genimage.XXXXXX")
cleanup_paths+=("${genimage_tmp}")
genimage_config="${genimage_tmp}/genimage.cfg"
cmdline="puu.live SYSTEMD_SULOGIN_FORCE=1 root=live:PARTUUID=@LIVE_PARTUUID@ puu.boot-partuuid=@BOOT_PARTUUID@ puu.payload-partuuid=@PAYLOAD_PARTUUID@ ro rd.live.image rd.overlay systemd.gpt_auto=0${PUU_CMDLINE_EXTRA} splash plymouth.ignore-serial-consoles vt.global_cursor_default=0 console=tty0"
"${HOST_DIR}/bin/python3" "${BASH_SOURCE%/*}/generate-image-config" \
  --arch "${PUU_ARCH}" --epoch "${SOURCE_DATE_EPOCH}" \
  --template "${BASH_SOURCE%/*}/genimage.cfg" --output "${genimage_config}" \
  --cmdline "${cmdline}" --cmdline-output "${genimage_tmp}/cmdline" \
  "${rootfs_squashfs}" "${oci_archive}" "${BINARIES_DIR}/image.manifest" \
  "${kernel}" "${initrd}" "${osrel}" "${stub}" "${sdboot}" \
  "${BASH_SOURCE[0]}" "${BASH_SOURCE%/*}/lib.sh"
"${HOST_DIR}/bin/ukify" build \
  --stub="${stub}" \
  --linux="${kernel}" \
  --initrd="${initrd}" \
  --cmdline=@"${genimage_tmp}/cmdline" \
  --os-release=@"${osrel}" \
  --uname="${kver}" \
  --output="${uki}"

cp "${sdboot}" "${efi_part}/EFI/BOOT/${PUU_EFI_BOOT_NAME}"
cp "${sdboot}" "${efi_part}/EFI/systemd/${PUU_EFI_SDBOOT_NAME}"

cat > "${efi_part}/loader/loader.conf" <<EOF
timeout 5
default ${uki_name}
EOF

mkdir -p "${genimage_tmp}/root"/{live/LiveOS,payload}
ln "${rootfs_squashfs}" "${genimage_tmp}/root/live/LiveOS/squashfs.img"
ln "${oci_archive}" "${genimage_tmp}/root/payload/image.tar"
ln "${BINARIES_DIR}/image.manifest" "${genimage_tmp}/root/payload/image.manifest"
export E2FSCK_TIME="${SOURCE_DATE_EPOCH}"
find "${efi_part}" -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +
"${HOST_DIR}/bin/genimage" \
  --rootpath "${genimage_tmp}/root" \
  --tmppath "${genimage_tmp}/work" \
  --inputpath "${BINARIES_DIR}" \
  --outputpath "${BINARIES_DIR}" \
  --config "${genimage_config}"

ln -sf image.tar "${BINARIES_DIR}/image-${puu_version}.tar"
ln -sf image.manifest "${BINARIES_DIR}/image-${puu_version}.manifest"
ln -sf "${output_img}" "${BINARIES_DIR}/${output_img%.img}-${puu_version}.img"
