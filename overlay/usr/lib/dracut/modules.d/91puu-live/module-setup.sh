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
  instmods efivarfs ext4
}

install() {
  : "${moddir:?}" "${systemdsystemunitdir:?}"
  inst_multiple bash lsblk udevadm readlink mount mountpoint umount mkdir od
  inst_script "$moddir/resolve-live-media" /usr/libexec/puu-live-media
  inst_simple "$moddir/puu-live-media.service" "$systemdsystemunitdir/puu-live-media.service"
  inst_simple "$moddir/live-media.conf" "$systemdsystemunitdir/sysroot.mount.d/50-puu-live-media.conf"
  inst_simple "$moddir/live-media.conf" "$systemdsystemunitdir/initrd-switch-root.target.d/50-puu-live-media.conf"
}
