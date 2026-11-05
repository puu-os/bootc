#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

if (($# != 1)) || [[ "$1" != configure && "$1" != build ]]; then
  echo "usage: $0 <configure|build>" >&2
  exit 1
fi
command=$1
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
: "${BOARD:?BOARD is required}"
: "${BOARD_DIR:?BOARD_DIR is required}"
: "${BUILDROOT_SRC_DIR:?BUILDROOT_SRC_DIR is required}"
: "${SDK_INSTALL_DIR:?SDK_INSTALL_DIR is required}"
: "${SOURCE_DATE_EPOCH:?SOURCE_DATE_EPOCH is required}"
[[ "${FORCE:-0}" == 0 || "${FORCE:-0}" == 1 ]] || { echo 'FORCE must be 0 or 1' >&2; exit 1; }

mkdir -p "$(dirname "${BUILDROOT_PATCH_LOCK}")"
exec 9>"${BUILDROOT_PATCH_LOCK%.patch}.build"
flock 9
./scripts/setup-buildroot.sh "${BUILDROOT_PATCH_LOCK}" "${BUILDROOT_TARBALL}" \
  "${BUILDROOT_HASH}" "${BOARD_DIR}" "${BUILDROOT_SRC_DIR}" \
  "${BUILDROOT_PATCH_STAMP}" "${BUILDROOT_PATCH}"

state=(python3 "${PWD}/scripts/build-state.py")
state_dir="${BOARD_DIR}/stamp/orchestration"
mkdir -p "${state_dir}"
make_args=(
  -j"${BUILDROOT_JOBS}" -l"${BUILDROOT_JOBS}" -C "${BUILDROOT_SRC_DIR}"
  O="${BOARD_DIR}" BR2_EXTERNAL="${PWD}" BR2_CCACHE_DIR="${CCACHE_DIR}"
  BR2_DL_DIR="${BUILDROOT_DL_DIR}" SDK_INSTALL_DIR="${SDK_INSTALL_DIR}"
  PUU_VERSION="${PUU_VERSION}" PUU_VARIANT="${PUU_VARIANT}"
  SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}"
)

configure_args=(key --path "configs/${BOARD}_defconfig"
  --path Config.in --path external.desc --path "${BUILDROOT_PATCH_STAMP}"
  --path "${SDK_INSTALL_STAMP}" --path scripts/build-board.sh
  --value "source=${BUILDROOT_SRC_DIR}" --value "sdk=${SDK_INSTALL_DIR}"
  --value "board=${BOARD_DIR}" --value "epoch=${SOURCE_DATE_EPOCH}")
while IFS= read -r -d '' path; do
  configure_args+=(--path "${path}")
done < <(find package -name Config.in \( -type f -o -type l \) -print0 | sort -z)
configure_key=$("${state[@]}" "${configure_args[@]}")
if [[ "${command}" == configure ]] || ! "${state[@]}" check "${state_dir}/configure.json" \
    --key "${configure_key}" --path "${BOARD_DIR}/.config"; then
  echo "${BOARD}: applying configuration"
  rm -f "${state_dir}/build.json"
  make "${make_args[@]}" "${BOARD}_defconfig"
  "${state[@]}" save "${state_dir}/configure.json" --key "${configure_key}" --path "${BOARD_DIR}/.config"
fi
[[ "${command}" == build ]] || exit 0
: "${PUU_COSIGN_KEYS?PUU_COSIGN_KEYS must be set (empty for unsigned builds)}"
export TARGET_DIR="${BOARD_DIR}/target" BR2_EXTERNAL_PUU_PATH="${PWD}"
export PUU_ARCH="${BOARD#puu_}"

# shellcheck source=board/puu/lib.sh
. ./board/puu/lib.sh
public_keys=$(mktemp -d "${state_dir}/public-keys.XXXXXX")
trap 'rm -rf -- "${public_keys}"' EXIT
prepare_signing_keys() {
  rm -f -- "${public_keys}"/*.pub
  if split_cosign_keys; then
    for i in "${!cosign_key_paths[@]}"; do
      write_cosign_public_key "${cosign_key_paths[${i}]}" "${public_keys}/${i}.pub"
    done
  fi
}
signing_identity() {
  # Only ordered public-key contents, not private keys or temporary paths.
  find "${public_keys}" -type f -printf '%f\n' | sort -n | while IFS= read -r name; do
    sha256sum < "${public_keys}/${name}"
  done
}
prepare_signing_keys
key_identity=$(signing_identity)
flatpak_key=disabled
case "${PUU_FLATPAK_PRESEED:-1}" in
  1) flatpak_key=$(./board/puu/seed-flatpaks.sh --key) ;;
  0) ;;
  *) echo 'PUU_FLATPAK_PRESEED must be 0 or 1' >&2; exit 1 ;;
esac
input_args=(key --exclude __pycache__ --exclude '*.pyc' --exclude dist
  --path Makefile --path Config.in --path external.mk --path external.desc
  --path configs --path sdk --path linux --path board --path scripts
  --path package --path patches --path overlay --path artwork --path buildroot.patch
  --value "source=${BUILDROOT_SRC_DIR}" --value "sdk=${SDK_INSTALL_DIR}"
  --value "board=${BOARD_DIR}" --value "epoch=${SOURCE_DATE_EPOCH}"
  --value "keys=${key_identity}" --value "flatpaks=${flatpak_key}")
while IFS= read -r name; do
  case "${name}" in
    PUU_COSIGN_KEYS|*PASSWORD*|*TOKEN*|*SECRET*) continue ;;
  esac
  input_args+=(--value "${name}=${!name}")
done < <(compgen -e | LC_ALL=C sort | grep -E '^(PUU_|BR2_)')
input_key=$("${state[@]}" "${input_args[@]}")

# Buildroot does not invalidate cached packages and reverse dependencies for recipe edits.
recipe_args=(key --path package --path patches --exclude package/oksa/files \
  --exclude package/puu-firstboot/files --exclude __pycache__ --exclude '*.pyc' --exclude dist)
recipe_key=$("${state[@]}" "${recipe_args[@]}")
if [[ -f "${state_dir}/recipes.key" ]]; then
  IFS= read -r previous_recipes < "${state_dir}/recipes.key"
  if [[ "${previous_recipes}" != "${recipe_key}" ]]; then
    echo "${BOARD}: package recipes or patches changed; cached dependency snapshots may be stale." >&2
    echo 'Run make clean, then build again. FORCE=1 does not bypass package invalidation.' >&2
    exit 1
  fi
fi

build_tree=$("${state[@]}" build-tree "${BOARD_DIR}")
build_key=$("${state[@]}" key --value "inputs=${input_key}" --value "packages=${build_tree}" \
  --path "${BOARD_DIR}/.config")
outputs=(--metadata-path "${BOARD_DIR}/images/${BOARD}.iso"
  --metadata-path "${BOARD_DIR}/images/image.tar"
  --path "${BOARD_DIR}/images/image.manifest"
  --metadata-path "${BOARD_DIR}/images/rootfs.squashfs"
  --metadata-path "${BOARD_DIR}/images/rootfs-live.squashfs"
  --path "${BOARD_DIR}/images/image-${PUU_VERSION}.tar"
  --path "${BOARD_DIR}/images/image-${PUU_VERSION}.manifest"
  --path "${BOARD_DIR}/images/${BOARD}-${PUU_VERSION}.iso")
if [[ -n "${key_identity}" ]]; then
  outputs+=(--path "${BOARD_DIR}/images/cosign"
    --metadata-path "${BOARD_DIR}/images/image.signed")
fi
unsigned_clean=true
if [[ -z "${key_identity}" ]] && {
  [[ -e "${BOARD_DIR}/images/cosign" || -L "${BOARD_DIR}/images/cosign" ]] ||
  [[ -e "${BOARD_DIR}/images/image.signed" || -L "${BOARD_DIR}/images/image.signed" ]];
}; then
  unsigned_clean=false
fi
if [[ "${FORCE:-0}" == 0 && "${unsigned_clean}" == true ]] && \
    "${state[@]}" check "${state_dir}/build.json" --key "${build_key}" "${outputs[@]}"; then
  echo "${BOARD}: up to date"
  exit 0
fi
if [[ "${FORCE:-0}" == 1 ]]; then
  echo "${BOARD}: forced build"
else
  echo "${BOARD}: inputs, package state or outputs changed; running incremental build"
fi
rm -f "${state_dir}/build.json"

linux_version=$(awk -F '"' '/^BR2_LINUX_KERNEL_CUSTOM_VERSION_VALUE="/ { print $2; exit }' "configs/${BOARD}_defconfig")
[[ -n "${linux_version}" ]] || { echo 'missing kernel version in board defconfig' >&2; exit 1; }
linux_dir="${BOARD_DIR}/build/linux-${linux_version}"
linux_changed=false
linux_built=
kernel_key=$("${state[@]}" key --path "linux/${PUU_ARCH}_efi_defconfig")
if [[ -f "${state_dir}/linux.key" ]]; then
  IFS= read -r previous_kernel < "${state_dir}/linux.key"
  [[ "${kernel_key}" == "${previous_kernel}" ]] || linux_changed=true
fi
if [[ "${linux_changed}" == true || ! -d "${linux_dir}" || ! -f "${linux_dir}/.stamp_configured" || ! -d "${BOARD_DIR}/per-package/linux" || "linux/${PUU_ARCH}_efi_defconfig" -nt "${linux_dir}/.stamp_configured" ]]; then
  linux_changed=true
  if [[ -d "${linux_dir}" || -d "${BOARD_DIR}/per-package/linux" ]]; then
    make "${make_args[@]}" linux-dirclean
  fi
elif [[ ! -f "${linux_dir}/.stamp_built" ]]; then
  linux_changed=true
else
  linux_built="${linux_dir}/.stamp_built"
fi
modules_changed=${linux_changed}
nvidia_count=0
for package in linux-tools nvidia-driver; do
  if [[ "${package}" == linux-tools ]]; then
    package_dir="${BOARD_DIR}/build/linux-tools"
  elif [[ -d "${BOARD_DIR}/build" ]]; then
    package_dir=$(find "${BOARD_DIR}/build" -mindepth 1 -maxdepth 1 -type d -name 'nvidia-driver-[0-9]*' -print -quit)
    nvidia_count=$(find "${BOARD_DIR}/build" -mindepth 1 -maxdepth 1 -type d -name 'nvidia-driver-[0-9]*' -printf x | wc -c)
  else
    package_dir=
  fi
  package_cache="${BOARD_DIR}/per-package/${package}"
  if [[ ( -d "${package_dir}" || -d "${package_cache}" ) && ( "${linux_changed}" == true || ! -d "${package_cache}" || "${linux_built}" -nt "${package_dir}/.stamp_configured" || ( "${package}" == nvidia-driver && "${nvidia_count}" -gt 1 ) ) ]]; then
    make "${make_args[@]}" "${package}-dirclean"
    modules_changed=true
  fi
done
if [[ "${modules_changed}" == true ]]; then
  rm -rf -- "${BOARD_DIR}/target/usr/lib/modules"
fi

local_packages=(oksa puu-firstboot)
local_keys=()
for package in "${local_packages[@]}"; do
  local_key=$("${state[@]}" key --path "package/${package}/files" \
    --exclude __pycache__ --exclude '*.pyc' --exclude dist)
  local_keys+=("${local_key}")
  previous_local=
  if [[ -f "${state_dir}/${package}.key" ]]; then
    IFS= read -r previous_local < "${state_dir}/${package}.key"
  fi
  if [[ "${local_key}" != "${previous_local}" ]]; then
    if [[ -d "${BOARD_DIR}/per-package/${package}" ]] || compgen -G "${BOARD_DIR}/build/${package}-*" >/dev/null; then
      make "${make_args[@]}" "${package}-dirclean"
    fi
  fi
done
make "${make_args[@]}" BR2_CCACHE=y

prepare_signing_keys
current_key_identity=$(signing_identity)
current_flatpak_key=disabled
if [[ "${PUU_FLATPAK_PRESEED:-1}" == 1 ]]; then
  current_flatpak_key=$(./board/puu/seed-flatpaks.sh --key)
fi
current_input_key=$("${state[@]}" "${input_args[@]}")
if [[ "${flatpak_key}" != "${current_flatpak_key}" || "${key_identity}" != "${current_key_identity}" || \
    "${input_key}" != "${current_input_key}" ]]; then
  echo "${BOARD}: inputs changed during the build; not recording an up-to-date result" >&2
  exit 1
fi
build_tree=$("${state[@]}" build-tree "${BOARD_DIR}")
build_key=$("${state[@]}" key --value "inputs=${input_key}" --value "packages=${build_tree}" \
  --path "${BOARD_DIR}/.config")
"${state[@]}" save "${state_dir}/configure.json" --key "${configure_key}" --path "${BOARD_DIR}/.config"
printf '%s\n' "${recipe_key}" > "${state_dir}/recipes.key.tmp"
mv "${state_dir}/recipes.key.tmp" "${state_dir}/recipes.key"
printf '%s\n' "${kernel_key}" > "${state_dir}/linux.key.tmp"
mv "${state_dir}/linux.key.tmp" "${state_dir}/linux.key"
for i in "${!local_packages[@]}"; do
  package=${local_packages[${i}]}
  printf '%s\n' "${local_keys[${i}]}" > "${state_dir}/${package}.key.tmp"
  mv "${state_dir}/${package}.key.tmp" "${state_dir}/${package}.key"
done
"${state[@]}" save "${state_dir}/build.json" --key "${build_key}" "${outputs[@]}"
