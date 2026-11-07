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

# shellcheck source=scripts/image-lib.sh
. "${BASH_SOURCE%/*}/image-lib.sh"
init_puu_arch "${PUU_ARCH}"

script_dir="${BASH_SOURCE%/*}"
state_helper="${script_dir}/state.py"
state="${BINARIES_DIR}/.image-live-iso.state"
puu_version="${PUU_VERSION:-1}"
kver=$(kernel_version)
kernel="${BINARIES_DIR}/${PUU_KERNEL_IMAGE}"
initrd="${TARGET_DIR}/usr/lib/modules/${kver}/initramfs.img"
osrel="${TARGET_DIR}/usr/lib/os-release"
stub="${TARGET_DIR}/usr/lib/systemd/boot/efi/${PUU_EFI_STUB_NAME}"
sdboot="${TARGET_DIR}/usr/lib/systemd/boot/efi/${PUU_EFI_SDBOOT_NAME}"
rootfs_squashfs="${BINARIES_DIR}/rootfs-live.squashfs"
oci_archive="${BINARIES_DIR}/image.tar"
manifest="${BINARIES_DIR}/image.manifest"
output_iso="puu_${PUU_ARCH}.iso"
output_alias="${output_iso%.iso}-${puu_version}.iso"
cmdline="puu.live SYSTEMD_SULOGIN_FORCE=1 root=live:LABEL=@ISO_VOLUME_ID@ puu.iso=@ISO_VOLUME_ID@ puu.boot-partuuid=@BOOT_PARTUUID@ ro rd.live.image rd.overlay systemd.gpt_auto=0${PUU_CMDLINE_EXTRA} quiet splash plymouth.ignore-serial-consoles plymouth.use-simpledrm vt.global_cursor_default=0 console=tty0"
image_inputs=("${rootfs_squashfs}" "${oci_archive}" "${manifest}"
  "${kernel}" "${initrd}" "${osrel}" "${stub}" "${sdboot}"
  "${BASH_SOURCE[0]}" "${script_dir}/image-lib.sh" "${script_dir}/image-live-iso.py")
inputs=(--value "arch=${PUU_ARCH}" --value "epoch=${SOURCE_DATE_EPOCH}"
  --value "version=${puu_version}" --value "kernel_version=${kver}" --value "cmdline=${cmdline}"
  --path "${state_helper}")
for path in "${image_inputs[@]}"; do
  resolved=$(realpath -e "${path}")
  inputs+=(--path "${path}")
  [ "${path}" = "${resolved}" ] || inputs+=(--path "${resolved}")
done
for tool in python3 ukify genimage mattrib xorriso; do
  path="${HOST_DIR}/bin/${tool}"
  resolved=$(realpath -e "${path}")
  inputs+=(--path "${path}")
  [ "${path}" = "${resolved}" ] || inputs+=(--path "${resolved}")
done
key=$(python3 "${state_helper}" key "${inputs[@]}")
outputs=(--metadata-path "${BINARIES_DIR}/${output_iso}" --path "${BINARIES_DIR}/${output_alias}")
if python3 "${state_helper}" check "${state}" --key "${key}" "${outputs[@]}"; then
  echo "live ISO is up to date"
  exit 0
else
  status=$?
  [ "${status}" = 1 ] || exit "${status}"
fi

iso_tmp=$(mktemp -d "${BINARIES_DIR}/iso.XXXXXX")
updated=()
committed=0
cleanup() {
  if [ "${committed}" = 0 ]; then
    for record in "${updated[@]}"; do
      existed="${record%%:*}"
      name="${record#*:}"
      if [ "${existed}" = 1 ]; then
        mv -fT "${iso_tmp}/old/${name}" "${BINARIES_DIR}/${name}"
      else
        rm -f "${BINARIES_DIR}/${name}"
      fi
    done
  fi
  rm -rf -- "${iso_tmp}"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

efi_part="${iso_tmp}/efi-part"
uki_name=puu.efi
uki="${efi_part}/EFI/Linux/${uki_name}"
mkdir -p "${efi_part}"/{EFI/{BOOT,Linux,systemd},loader}
mkdir -p "${iso_tmp}/old" "${iso_tmp}/empty" "${iso_tmp}/root"/{LiveOS,payload,boot}
ln -s "${rootfs_squashfs}" "${iso_tmp}/root/LiveOS/squashfs.img"
ln -s "${oci_archive}" "${iso_tmp}/root/payload/image.tar"
ln -s "${manifest}" "${iso_tmp}/root/payload/image.manifest"
"${HOST_DIR}/bin/python3" "${script_dir}/image-live-iso.py" \
  --arch "${PUU_ARCH}" --epoch "${SOURCE_DATE_EPOCH}" \
  --output "${iso_tmp}/genimage.cfg" \
  --cmdline "${cmdline}" --cmdline-output "${iso_tmp}/cmdline" \
  --metadata-output "${iso_tmp}/metadata" "${image_inputs[@]}"
"${HOST_DIR}/bin/ukify" build \
  --stub="${stub}" \
  --linux="${kernel}" \
  --initrd="${initrd}" \
  --cmdline=@"${iso_tmp}/cmdline" \
  --os-release=@"${osrel}" \
  --uname="${kver}" \
  --output="${uki}"

cp "${sdboot}" "${efi_part}/EFI/BOOT/${PUU_EFI_BOOT_NAME}"
cp "${sdboot}" "${efi_part}/EFI/systemd/${PUU_EFI_SDBOOT_NAME}"
cat > "${efi_part}/loader/loader.conf" <<EOF
timeout 5
default ${uki_name}
EOF
# The live ESP is immutable. Read-only placeholders stop systemd-boot and
# systemd-stub from creating these files, which breaks some firmware.
esp_ro_files=(loader/random-seed loader/boot-secret-mixin)
for file in "${esp_ro_files[@]}"; do
  : > "${efi_part}/${file}"
done
find "${efi_part}" -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +
"${HOST_DIR}/bin/genimage" \
  --rootpath "${iso_tmp}/empty" \
  --tmppath "${iso_tmp}/work" \
  --inputpath "${iso_tmp}" \
  --outputpath "${iso_tmp}/root/boot" \
  --config "${iso_tmp}/genimage.cfg"
"${HOST_DIR}/bin/mattrib" -i "${iso_tmp}/root/boot/efi.img" +r \
  "${esp_ro_files[@]/#/::/}"
read -r iso_volume_id iso_disk_guid iso_esp_guid_hex < "${iso_tmp}/metadata"
iso_date=$(date -u -d "@${SOURCE_DATE_EPOCH}" +%Y%m%d%H%M%S00)
"${HOST_DIR}/bin/xorriso" -as mkisofs \
  -follow-links -iso-level 3 -R -J -V "${iso_volume_id}" -uid 0 -gid 0 -dir-mode 0755 -file-mode 0644 \
  --modification-date="${iso_date}" --set_all_file_dates "${iso_date}" \
  --gpt_disk_guid "${iso_disk_guid}" \
  -efi-boot-part --efi-boot-image -e boot/efi.img -no-emul-boot \
  -o "${iso_tmp}/${output_iso}" "${iso_tmp}/root"
"${HOST_DIR}/bin/xorriso" -indev "${iso_tmp}/${output_iso}" \
  -report_system_area plain > "${iso_tmp}/layout"
if ! grep -Eq "^GPT partition GUID : +2 +${iso_esp_guid_hex}$" "${iso_tmp}/layout"; then
  echo "ISO ESP GUID does not match the UKI boot identity" >&2
  exit 1
fi
test -s "${iso_tmp}/${output_iso}"
ln -s "${output_iso}" "${iso_tmp}/${output_alias}"
for name in "${output_iso}" "${output_alias}"; do
  existed=0
  if [ -e "${BINARIES_DIR}/${name}" ] || [ -L "${BINARIES_DIR}/${name}" ]; then
    existed=1
    ln -P "${BINARIES_DIR}/${name}" "${iso_tmp}/old/${name}"
  fi
  updated+=("${existed}:${name}")
  mv -fT "${iso_tmp}/${name}" "${BINARIES_DIR}/${name}"
done
python3 "${state_helper}" save "${state}" --key "${key}" "${outputs[@]}"
committed=1
