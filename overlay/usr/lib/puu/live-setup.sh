#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

live_device=$(readlink -f /run/initramfs/livedev 2>/dev/null || true)
if [[ -b ${live_device} ]]; then
  parent=$(lsblk -ndo PKNAME "${live_device}")
  if [[ -n ${parent} ]]; then
    mapfile -t live_devices < <(lsblk -nrpo NAME "/dev/${parent}")
  else
    live_devices=("${live_device}")
  fi
  for device in "${live_devices[@]}"; do
    blockdev --setro "${device}"
  done
fi

for group in adm audio cdrom dip input lpadmin netdev plugdev render sudo video wheel; do
  grep -q "^${group}:" /etc/group || groupadd -r "${group}"
done

grep -q "^puu:" /etc/group || groupadd puu

if ! id -u puu >/dev/null 2>&1; then
  useradd -m -g puu -G adm,audio,cdrom,dip,input,lpadmin,netdev,plugdev,render,sudo,video,wheel \
    -s /bin/bash -c "Puu live user" puu
fi

install -d -m 0700 -o puu -g puu /home/puu

install -d -m 0755 /etc/dconf/profile /etc/dconf/db/local.d
cat > /etc/dconf/profile/user <<'EOF'
user-db:user
system-db:local
EOF
cat > /etc/dconf/db/local.d/00-puu-live-session <<'EOF'
[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/desktop/screensaver]
lock-enabled=false

[org/gnome/desktop/lockdown]
disable-lock-screen=true

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'

[org/gnome/software]
allow-updates=false
download-updates=false
download-updates-notify=false
EOF
/usr/bin/dconf update

install -d -m 0755 /var/lib/AccountsService/users
cat > /var/lib/AccountsService/users/puu <<'EOF'
[User]
Session=gnome
SessionType=wayland
XSession=gnome
SystemAccount=false
EOF

install -Dm0644 /usr/share/puu/live/gdm-custom.conf /etc/gdm/custom.conf
install -Dm0440 /usr/share/puu/live/sudoers-nopasswd \
  /etc/sudoers.d/20-puu-nopasswd
