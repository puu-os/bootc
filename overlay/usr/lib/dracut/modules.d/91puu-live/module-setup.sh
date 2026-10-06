#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

check() {
  return 0
}

depends() {
  echo "dmsquash-live systemd"
}

installkernel() {
  instmods efivarfs sr_mod iso9660
}

install() {
  : "${moddir:?}" "${systemdsystemunitdir:?}"
  local source
  local -a python_dirs=("${dracutsysrootdir-}"/usr/lib/python3.*)
  if ((${#python_dirs[@]} != 1)) || [[ ! -d ${python_dirs[0]} ]]; then
    dfatal "Expected one Python library directory for live-media checks"
    exit 1
  fi
  inst_multiple bash lsblk findmnt udevadm readlink mount mountpoint umount mkdir od python3 || exit 1
  for source in "${python_dirs[0]}"/os.py* "${python_dirs[0]}"/encodings/*.py* "${python_dirs[0]}"/lib-dynload/binascii*.so; do
    [[ -f ${source} ]] || {
      dfatal "Missing Python library file for live-media checks: ${source}"
      exit 1
    }
    inst_multiple "${source#"${dracutsysrootdir-}"}" || exit 1
  done
  inst_script "$moddir/validate-live-iso" /usr/libexec/puu-live-iso || exit 1
  inst_script "$moddir/resolve-live-media" /usr/libexec/puu-live-media || exit 1
  inst_simple "$moddir/puu-live-media.service" "$systemdsystemunitdir/puu-live-media.service" || exit 1
  inst_simple "$moddir/live-media.conf" "$systemdsystemunitdir/sysroot.mount.d/50-puu-live-media.conf" || exit 1
  inst_simple "$moddir/live-media.conf" "$systemdsystemunitdir/initrd-switch-root.target.d/50-puu-live-media.conf" || exit 1
}
