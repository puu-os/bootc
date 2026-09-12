GNOME_SHELL_VERSION_MAJOR = 50
GNOME_SHELL_VERSION = $(GNOME_SHELL_VERSION_MAJOR).4
GNOME_SHELL_SOURCE = gnome-shell-$(GNOME_SHELL_VERSION).tar.xz
GNOME_SHELL_SITE = https://download.gnome.org/sources/gnome-shell/$(GNOME_SHELL_VERSION_MAJOR)
GNOME_SHELL_LICENSE = GPL-2.0+
GNOME_SHELL_LICENSE_FILES = COPYING
GNOME_SHELL_INSTALL_STAGING = YES
GNOME_SHELL_DEPENDENCIES = \
	host-pkgconf \
	host-patchelf \
	evolution-data-server \
	gcr \
	gdk-pixbuf \
	gjs \
	gnome-desktop \
	gsettings-desktop-schemas \
	ibus \
	mutter \
	network-manager \
	gnome-settings-daemon \
	gnome-session \
	libglib2 \
	libgtk4 \
	libxml2 \
	pango \
	pipewire \
	libadwaita \
	polkit \
	rtkit \
	systemd

GNOME_SHELL_CONF_OPTS = \
	-Dsystemd=true \
	-Dman=false \
	-Dgtk_doc=false \
	-Dextensions_app=true \
	-Dextensions_tool=false \
	-Dnetworkmanager=true \
	-Dportal_helper=false \
	-Dtests=false

GNOME_SHELL_MESON_EXTRA_BINARIES = gjs='/usr/bin/gjs'

GNOME_SHELL_RUNPATH = /usr/lib/gnome-shell:/usr/lib/mutter-18

# Add mutter private library path to RUNPATH for GDM sessions.
define GNOME_SHELL_SET_RUNPATH
	$(HOST_DIR)/bin/patchelf --set-rpath $(GNOME_SHELL_RUNPATH) \
		$(TARGET_DIR)/usr/bin/gnome-shell
	for f in $(TARGET_DIR)/usr/lib/gnome-shell/*.so; do \
		$(HOST_DIR)/bin/patchelf --set-rpath $(GNOME_SHELL_RUNPATH) "$${f}"; \
	done
endef
GNOME_SHELL_POST_INSTALL_TARGET_HOOKS += GNOME_SHELL_SET_RUNPATH

# Rewrite host gjs path to target path in generated files.
define GNOME_SHELL_FIX_TARGET_GJS_PATHS
	$(SED) 's|$(HOST_DIR)/bin/gjs|/usr/bin/gjs|g' \
		$(TARGET_DIR)/usr/bin/gnome-extensions-app \
		$(TARGET_DIR)/usr/share/dbus-1/services/org.gnome.ScreenSaver.service \
		$(TARGET_DIR)/usr/share/dbus-1/services/org.gnome.Shell.Extensions.service \
		$(TARGET_DIR)/usr/share/dbus-1/services/org.gnome.Shell.Notifications.service \
		$(TARGET_DIR)/usr/share/dbus-1/services/org.gnome.Shell.Screencast.service
endef
GNOME_SHELL_POST_INSTALL_TARGET_HOOKS += GNOME_SHELL_FIX_TARGET_GJS_PATHS

# Ensure direct ibus-daemon fallback passes --config disable.
define GNOME_SHELL_IBUS_CONFIG_DISABLE
	$(SED) "s|\['ibus-daemon', '--panel', 'disable'|['ibus-daemon', '--panel', 'disable', '--config', 'disable'|g" \
		$(@D)/js/misc/ibusManager.js
endef
GNOME_SHELL_POST_PATCH_HOOKS += GNOME_SHELL_IBUS_CONFIG_DISABLE

$(eval $(meson-package))
