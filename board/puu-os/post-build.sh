#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

: "${TARGET_DIR:?TARGET_DIR must be set by Buildroot}"
: "${HOST_DIR:?HOST_DIR must be set by Buildroot}"
PUU_ARCH="${2:?arch argument must be set (amd64 or arm64)}"
: "${PUU_COSIGN_KEYS?PUU_COSIGN_KEYS must be set (empty for unsigned builds)}"

# shellcheck source=board/puu-os/lib.sh
. "${BASH_SOURCE%/*}/lib.sh"

init_puu_arch "${PUU_ARCH}"

install -Dm644 "${BASH_SOURCE%/*}/../../artwork/splash.png" \
  "${TARGET_DIR}/usr/share/plymouth/themes/puu/splash.png"

# Buildroot generates mime.cache when shared-mime-info is installed, before
# other packages and the overlay add their own definitions. Rebuild it from
# the complete set.
: "${STAGING_DIR:?STAGING_DIR must be set by Buildroot}"
mime_tmp="$(mktemp -d)"
mkdir -p "${mime_tmp}/packages"
cp "${STAGING_DIR}/usr/share/mime/packages/"*.xml "${mime_tmp}/packages/"
cp "${TARGET_DIR}/usr/share/mime/packages/"*.xml "${mime_tmp}/packages/"
"${HOST_DIR}/bin/update-mime-database" "${mime_tmp}"
install -m644 "${mime_tmp}/mime.cache" "${TARGET_DIR}/usr/share/mime/mime.cache"
rm -rf "${mime_tmp}"

puu_variant="${PUU_VARIANT:-gnome}"
puu_version="${PUU_VERSION:-1}"
target_imgref="${PUU_TARGET_IMGREF:-quay.io/puu-os/${puu_variant}:${puu_version}}"
target_repo="${target_imgref%@*}"
target_name="${target_repo##*/}"
if [[ "${target_name}" == *:* ]]; then
  target_repo="${target_repo%:*}"
fi

write_os_release() {
  local variant_title
  case "${puu_variant}" in
    gnome) variant_title="GNOME" ;;
    kde)   variant_title="KDE" ;;
    core)  variant_title="Core" ;;
    *)     variant_title="${puu_variant}" ;;
  esac

  install -d -m 0755 "${TARGET_DIR}/usr/lib" "${TARGET_DIR}/etc"
  cat > "${TARGET_DIR}/usr/lib/os-release" <<EOF
NAME=Puu
ID=puu
VARIANT="${variant_title}"
VARIANT_ID=${puu_variant}
VERSION="${puu_version} (${variant_title})"
VERSION_ID=${puu_version}
PRETTY_NAME="Puu ${puu_version} (${variant_title})"
IMAGE_ID=puu-${puu_variant}
IMAGE_VERSION=${puu_version}
EOF
  ln -sf ../usr/lib/os-release "${TARGET_DIR}/etc/os-release"
}

rm -f "${TARGET_DIR}/etc/pki/puu-os/cosign.pub"
rm -rf "${TARGET_DIR}/etc/pki/puu-os/cosign"
trusted_key_paths=()
if split_cosign_keys; then
  mkdir -p "${TARGET_DIR}/etc/pki/puu-os/cosign"

  for i in "${!cosign_key_paths[@]}"; do
    cosign_key="${cosign_key_paths[${i}]}"
    trusted_key_path="/etc/pki/puu-os/cosign/key-${i}.pub"
    write_cosign_public_key "${cosign_key}" \
      "${TARGET_DIR}${trusted_key_path}"
    trusted_key_paths+=("${trusted_key_path}")
  done
fi

"${BASH_SOURCE%/*}/generate-container-policy" "${target_repo}" "${trusted_key_paths[@]}" \
  > "${TARGET_DIR}/etc/containers/policy.json"

kver=$(kernel_version)

kernel="${TARGET_DIR}/boot/${PUU_KERNEL_IMAGE}"
[ -f "${kernel}" ] || { echo "kernel image not found: ${kernel}" >&2; exit 1; }
install -Dm644 "${kernel}" "${TARGET_DIR}/usr/lib/modules/${kver}/vmlinuz"

cni_plugins_tmp="${TARGET_DIR}/usr/lib/puu-cni"
rm -rf "${cni_plugins_tmp}"
if [ -d "${TARGET_DIR}/opt/cni" ]; then
  mv "${TARGET_DIR}/opt/cni" "${cni_plugins_tmp}"
fi

for d in boot home root usr/local srv opt mnt var; do
  rm -rf "${TARGET_DIR:?}/${d}"
done

mkdir -p "${TARGET_DIR}"/{sysroot,efi,boot,usr/lib/{ostree,tmpfiles.d},var/tmp}

host_dracut_link="${TARGET_DIR}${HOST_DIR}/lib/dracut"
cleanup_host_dracut_link() {
  rm -f "${host_dracut_link}"
  rmdir -p "$(dirname "${host_dracut_link}")" 2>/dev/null || true
}
trap cleanup_host_dracut_link EXIT
mkdir -p "$(dirname "${host_dracut_link}")"
ln -sfn "${HOST_DIR}/lib/dracut" "${host_dracut_link}"

rm -f \
  "${TARGET_DIR}/etc/systemd/system/emergency.service.d/10-live-root-shell.conf" \
  "${TARGET_DIR}/etc/systemd/system/rescue.service.d/10-live-root-shell.conf" \
  "${TARGET_DIR}/usr/libexec/puu-os/live-root-shell"
rmdir \
  "${TARGET_DIR}/etc/systemd/system/emergency.service.d" \
  "${TARGET_DIR}/etc/systemd/system/rescue.service.d" \
  2>/dev/null || true

for unit in \
  NetworkManager-config-initrd.service \
  NetworkManager-initrd.service \
  NetworkManager-wait-online-initrd.service; do
  rm -f "${TARGET_DIR}/usr/lib/systemd/system/${unit}"
done

bluez_main_conf="${TARGET_DIR}/etc/bluetooth/main.conf"
if [ -f "$bluez_main_conf" ]; then
  sed -i \
    's/^KernelExperimental = 6fbaf188-05e0-496a-9885-d6ddfdb4e03e$/#KernelExperimental = false/' \
    "$bluez_main_conf"
fi


DRACUT_ARCH="${PUU_TARGET_ARCH}" "${HOST_DIR}/bin/dracut" \
  --sysroot "${TARGET_DIR}" --force \
  --add-confdir "${TARGET_DIR}/usr/lib/dracut/dracut.conf.d" \
  "${TARGET_DIR}/usr/lib/modules/${kver}/initramfs.img" "${kver}"

cleanup_host_dracut_link
trap - EXIT

mkdir -p "${TARGET_DIR}/var"/{roothome,srv,opt,mnt,home,usrlocal}

if [ -d "${cni_plugins_tmp}" ]; then
  rm -rf "${TARGET_DIR}/usr/lib/cni"
  mv "${cni_plugins_tmp}" "${TARGET_DIR}/usr/lib/cni"
fi

ln -sfT sysroot/ostree "${TARGET_DIR}/ostree"
ln -sfT var/roothome "${TARGET_DIR}/root"
ln -sfT var/srv "${TARGET_DIR}/srv"
ln -sfT var/opt "${TARGET_DIR}/opt"
ln -sfT var/mnt "${TARGET_DIR}/mnt"
ln -sfT var/home "${TARGET_DIR}/home"
ln -sfT ../var/usrlocal "${TARGET_DIR}/usr/local"

mkdir -p "${TARGET_DIR}/var/lib/rancher/etc"
rm -rf "${TARGET_DIR}/etc/rancher"
ln -sfn /var/lib/rancher/etc "${TARGET_DIR}/etc/rancher"

sed -i -E '/^[Qq][[:space:]]+\/(home|srv)\b/d' \
  "${TARGET_DIR}/usr/lib/tmpfiles.d/home.conf"
sed -i -E '/^d-?[[:space:]]+\/root[[:space:]]+:?0[0-9]{3}/d' \
  "${TARGET_DIR}/usr/lib/tmpfiles.d/provision.conf"

gnome_user_unit_dir="${TARGET_DIR}/usr/lib/systemd/user"
gnome_session_unit_files=(
  "${gnome_user_unit_dir}/gnome-session-basic-services.target"
  "${gnome_user_unit_dir}/gnome-session@gnome.target.d/gnome.session.conf"
  "${gnome_user_unit_dir}/gnome-session@gnome-login.target.d/gnome-login.session.conf"
)
gnome_session_components=(Color PrintNotifications Smartcard Wacom Wwan)
for component in "${gnome_session_components[@]}"; do
  component_target="${gnome_user_unit_dir}/org.gnome.SettingsDaemon.${component}.target"
  [[ -e "$component_target" ]] && continue

  for session_unit_file in "${gnome_session_unit_files[@]}"; do
    if [ -f "$session_unit_file" ]; then
      sed -i "/^Wants=org\.gnome\.SettingsDaemon\.${component}\.target$/d" \
        "$session_unit_file"
    fi
  done
done

gnome_greeter_skipped_components=(MediaKeys Rfkill Sharing)
for component in "${gnome_greeter_skipped_components[@]}"; do
  component_service="org.gnome.SettingsDaemon.${component}.service"
  [[ -f "${gnome_user_unit_dir}/${component_service}" ]] || continue

  component_dropin_dir="${gnome_user_unit_dir}/${component_service}.d"
  mkdir -p "$component_dropin_dir"
  cat > "${component_dropin_dir}/10-puu-skip-greeter.conf" <<EOF
[Unit]
ConditionEnvironment=!XDG_SESSION_CLASS=greeter
EOF
done

rm -f "${TARGET_DIR}/usr/share/xsessions/gnome.desktop"

# Buildroot only applies the system presets.
"${HOST_DIR}/bin/systemctl" --root="${TARGET_DIR}" --global preset-all

rm -f "${TARGET_DIR}/etc/xdg/autostart/pulseaudio.desktop"
rm -f \
  "${gnome_user_unit_dir}/flatpak-sync.service" \
  "${gnome_user_unit_dir}/flatpak-sync.timer" \
  "${gnome_user_unit_dir}/gnome-session.target.wants/localsearch-3.service" \
  "${gnome_user_unit_dir}/gnome-session.target.wants/localsearch-control-3.service" \
  "${gnome_user_unit_dir}/gnome-session.target.wants/localsearch-writeback-3.service"
portal_conf="${TARGET_DIR}/usr/share/xdg-desktop-portal/gnome-portals.conf"
if [ -f "$portal_conf" ]; then
  sed -i '/^org\.freedesktop\.impl\.portal\.Secret=gnome-keyring;$/d' "$portal_conf"
fi
mutter_typelib_dir="${TARGET_DIR}/usr/lib/mutter-18"
if [ -d "$mutter_typelib_dir" ]; then
  mkdir -p "${TARGET_DIR}/usr/lib/girepository-1.0"
  for f in "$mutter_typelib_dir"/*.typelib; do
    [ -e "$f" ] || continue
    typelib=$(basename "$f")
    ln -sf "../mutter-18/${typelib}" \
      "${TARGET_DIR}/usr/lib/girepository-1.0/${typelib}"
  done
fi
rm -f "${TARGET_DIR}/usr/share/gdm/greeter/autostart/orca-autostart.desktop"

# Regenerate gdk-pixbuf so that librsvg's SVG loader gets filled in.
pixbuf_module_dir=lib/gdk-pixbuf-2.0/2.10.0
pixbuf_loaders_cache="${TARGET_DIR}/usr/${pixbuf_module_dir}/loaders.cache"
if [ -d "${TARGET_DIR}/usr/${pixbuf_module_dir}/loaders" ]; then
  GDK_PIXBUF_MODULEDIR="${HOST_DIR}/${pixbuf_module_dir}/loaders" \
    "${HOST_DIR}/bin/gdk-pixbuf-query-loaders" |
    sed -e '/^#/d' -e 's,^"lib,"/usr/lib,' > "${pixbuf_loaders_cache}"

  while read -r loader; do
    if [ ! -f "${TARGET_DIR}${loader}" ]; then
      echo "${BASH_SOURCE##*/}: ${loader} is in loaders.cache but not in the target" >&2
      exit 1
    fi
  done < <(sed -n 's,^"\(/usr/.*\.so\)".*,\1,p' "${pixbuf_loaders_cache}")
fi
mkdir -p "${TARGET_DIR}/etc"/{pulse/default.pa.d,tcb}

write_os_release
rm -f "${TARGET_DIR}/usr/share/dbus-1/services/org.freedesktop.systemd1.service"
chmod 0440 \
  "${TARGET_DIR}/etc/sudoers.d/10-puu-wheel" \
  "${TARGET_DIR}/usr/share/puu-os/live/sudoers-nopasswd"

if [ "${PUU_ARCH}" = amd64 ] && [ ! -e "${TARGET_DIR}/lib64" ]; then
  ln -s lib "${TARGET_DIR}/lib64"
fi

# Ensure ldconfig is present in the target for container runtimes (such as nvidia-container-cli).
ldconfig_bin=$(find "${HOST_DIR}" -path "*/sysroot/usr/bin/ldconfig" 2>/dev/null | head -n1)
if [ -n "${ldconfig_bin}" ] && [ -f "${ldconfig_bin}" ]; then
  install -Dm0755 "${ldconfig_bin}" "${TARGET_DIR}/usr/bin/ldconfig"
fi

mkdir -p "${TARGET_DIR}/etc/systemd/system/getty.target.wants"
ln -sfn /usr/lib/systemd/system/serial-getty@.service \
  "${TARGET_DIR}/etc/systemd/system/getty.target.wants/serial-getty@${PUU_SERIAL_TTY}.service"

find "${TARGET_DIR}/etc/systemd/system" -type l -regextype posix-extended \
  -regex '.*/Cu[[:alnum:]]{6}' -delete
