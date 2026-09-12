GNOME_SESSION_VERSION_MAJOR = 50
GNOME_SESSION_VERSION = $(GNOME_SESSION_VERSION_MAJOR).1
GNOME_SESSION_SOURCE = gnome-session-$(GNOME_SESSION_VERSION).tar.xz
GNOME_SESSION_SITE = https://download.gnome.org/sources/gnome-session/$(GNOME_SESSION_VERSION_MAJOR)
GNOME_SESSION_LICENSE = GPL-2.0+
GNOME_SESSION_LICENSE_FILES = COPYING
GNOME_SESSION_INSTALL_STAGING = YES
GNOME_SESSION_DEPENDENCIES = \
	host-pkgconf \
	host-libglib2 \
	dbus \
	gnome-desktop \
	json-glib \
	libglib2 \
	libgtk3 \
	libxml2 \
	systemd \
	xkeyboard-config

GNOME_SESSION_CONF_OPTS = \
	-Dman=false \
	-Ddocbook=false \
	-Ddeprecation_flags=false

$(eval $(meson-package))
