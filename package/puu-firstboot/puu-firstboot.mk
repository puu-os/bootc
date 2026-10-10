# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

PUU_FIRSTBOOT_VERSION = 1.0
PUU_FIRSTBOOT_SITE = $(PUU_FIRSTBOOT_PKGDIR)/files
PUU_FIRSTBOOT_SITE_METHOD = local
PUU_FIRSTBOOT_LICENSE = GPL-2.0-or-later
PUU_FIRSTBOOT_LICENSE_FILES = LICENSE
PUU_FIRSTBOOT_DEPENDENCIES = \
	dconf \
	gdm \
	gnome-control-center \
	gnome-session \
	gnome-settings-daemon \
	gnome-shell \
	host-dconf \
	libgtk4 \
	oksa-services \
	python3 \
	python-gobject \
	systemd

PUU_FIRSTBOOT_GUI_SCRIPTS = session-dispatch firstboot-wizard
PUU_FIRSTBOOT_BACKEND_SCRIPTS = firstboot-enroll firstboot-monitor firstboot-retry
PUU_FIRSTBOOT_SYSTEM_UNITS = \
	puu-firstboot-monitor.service \
	puu-firstboot-retry.service \
	puu-firstboot-homectl.service.d/puu-firstboot.conf
PUU_FIRSTBOOT_USER_FILES = \
	puu-firstboot.service \
	gnome-session@puu-firstboot.target.d/puu-firstboot.conf \
	puu-bootc-notify.service.d/puu-firstboot.conf
PUU_FIRSTBOOT_SHARE_FILES = \
	wayland-sessions/gnome.desktop \
	gnome-session/sessions/puu-firstboot.session \
	gnome-shell/modes/initial-setup.json
PUU_FIRSTBOOT_DCONF_FILES = \
	profile/puu-firstboot \
	db/puu-firstboot.d/00-lockdown \
	db/puu-firstboot.d/locks/00-lockdown
PUU_FIRSTBOOT_POLKIT_FILES = 50-puu-firstboot.rules

define PUU_FIRSTBOOT_INSTALL_TARGET_CMDS
	$(foreach script,$(PUU_FIRSTBOOT_GUI_SCRIPTS) $(PUU_FIRSTBOOT_BACKEND_SCRIPTS),\
		$(INSTALL) -D -m 0755 $(@D)/bin/$(script) \
			$(TARGET_DIR)/usr/libexec/puu/$(script)$(sep))
	$(INSTALL) -D -m 0644 $(@D)/bin/firstboot_state.py \
		$(TARGET_DIR)/usr/libexec/puu/firstboot_state.py
	$(foreach file,$(PUU_FIRSTBOOT_SHARE_FILES),\
		$(INSTALL) -D -m 0644 $(@D)/share/$(file) \
			$(TARGET_DIR)/usr/share/$(file)$(sep))
	for entry in $(TARGET_DIR)/usr/share/wayland-sessions/*.desktop \
		$(TARGET_DIR)/usr/share/xsessions/*.desktop; do \
		test -f "$$entry" || continue; \
		if grep -Eq '^DesktopNames=.*GNOME|^Exec=.*gnome-session' "$$entry"; then \
			$(INSTALL) -m 0644 $(@D)/share/wayland-sessions/gnome.desktop \
				"$$entry" || exit $$?; \
		fi; \
	done
	$(foreach file,$(PUU_FIRSTBOOT_USER_FILES),\
		$(INSTALL) -D -m 0644 $(@D)/systemd/user/$(file) \
			$(TARGET_DIR)/usr/lib/systemd/user/$(file)$(sep))
	$(foreach file,$(PUU_FIRSTBOOT_DCONF_FILES),\
		$(INSTALL) -D -m 0644 $(@D)/dconf/$(file) \
			$(TARGET_DIR)/etc/dconf/$(file)$(sep))
	if ! test -f $(TARGET_DIR)/etc/dconf/profile/user && \
		! test -f $(TARGET_DIR)/usr/local/share/dconf/profile/user && \
		! test -f $(TARGET_DIR)/usr/share/dconf/profile/user; then \
		$(INSTALL) -D -m 0644 $(@D)/dconf/profile/user \
			$(TARGET_DIR)/etc/dconf/profile/user; \
	fi
	$(HOST_DIR)/bin/dconf compile $(TARGET_DIR)/etc/dconf/db/puu-firstboot \
		$(@D)/dconf/db/puu-firstboot.d
	$(INSTALL) -d -m 0755 $(TARGET_DIR)/etc/puu-firstboot
	for file in $(PUU_FIRSTBOOT_POLKIT_FILES); do \
		$(INSTALL) -D -m 0644 $(@D)/polkit-1/rules.d/$$file \
			$(TARGET_DIR)/usr/share/polkit-1/rules.d/$$file || exit $$?; \
	done
endef

define PUU_FIRSTBOOT_INSTALL_INIT_SYSTEMD
	for unit in $(PUU_FIRSTBOOT_SYSTEM_UNITS); do \
		$(INSTALL) -D -m 0644 $(@D)/systemd/system/$$unit \
			$(TARGET_DIR)/usr/lib/systemd/system/$$unit || exit $$?; \
	done
	$(INSTALL) -d $(TARGET_DIR)/usr/lib/systemd/system-preset
	printf '%s\n' 'enable puu-firstboot-monitor.service' > \
		$(TARGET_DIR)/usr/lib/systemd/system-preset/84-puu-firstboot.preset
endef

$(eval $(generic-package))
