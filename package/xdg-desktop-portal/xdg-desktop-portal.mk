XDG_DESKTOP_PORTAL_VERSION = 1.22.1
XDG_DESKTOP_PORTAL_SOURCE = xdg-desktop-portal-$(XDG_DESKTOP_PORTAL_VERSION).tar.xz
XDG_DESKTOP_PORTAL_SITE = https://github.com/flatpak/xdg-desktop-portal/releases/download/$(XDG_DESKTOP_PORTAL_VERSION)
XDG_DESKTOP_PORTAL_LICENSE = LGPL-2.1+
XDG_DESKTOP_PORTAL_LICENSE_FILES = COPYING
XDG_DESKTOP_PORTAL_INSTALL_STAGING = YES
XDG_DESKTOP_PORTAL_DEPENDENCIES = \
	host-pkgconf \
	flatpak \
	bubblewrap \
	gdk-pixbuf \
	gst1-plugins-base \
	json-glib \
	libfuse3 \
	libglib2 \
	libgudev \
	pipewire \
	systemd

XDG_DESKTOP_PORTAL_MESON_EXTRA_BINARIES = bwrap='/usr/bin/bwrap'

XDG_DESKTOP_PORTAL_CONF_OPTS = \
	-Dsystemd=enabled \
	-Dgeoclue=disabled \
	-Dgudev=enabled \
	-Dflatpak-interfaces=enabled \
	-Dflatpak-interfaces-dir=$(STAGING_DIR)/usr/share/dbus-1/interfaces \
	-Ddocumentation=disabled \
	-Dman-pages=disabled \
	-Dtests=disabled \
	-Dinstalled-tests=false

$(eval $(meson-package))
