LIBADWAITA_VERSION_MAJOR = 1.9
LIBADWAITA_VERSION = $(LIBADWAITA_VERSION_MAJOR).3
LIBADWAITA_SOURCE = libadwaita-$(LIBADWAITA_VERSION).tar.xz
LIBADWAITA_SITE = https://download.gnome.org/sources/libadwaita/$(LIBADWAITA_VERSION_MAJOR)
LIBADWAITA_LICENSE = LGPL-2.1+
LIBADWAITA_LICENSE_FILES = COPYING
LIBADWAITA_INSTALL_STAGING = YES
LIBADWAITA_DEPENDENCIES = \
	gobject-introspection \
	host-pkgconf \
	host-sassc \
	appstream \
	libfribidi \
	libgtk4 \
	libglib2

LIBADWAITA_CONF_OPTS = \
	-Dintrospection=enabled \
	-Dvapi=false \
	-Dtests=false \
	-Dexamples=false

$(eval $(meson-package))
