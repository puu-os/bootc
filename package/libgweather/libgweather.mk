LIBGWEATHER_VERSION_MAJOR = 4.6
LIBGWEATHER_VERSION = $(LIBGWEATHER_VERSION_MAJOR).0
LIBGWEATHER_SOURCE = libgweather-$(LIBGWEATHER_VERSION).tar.xz
LIBGWEATHER_SITE = https://download.gnome.org/sources/libgweather/$(LIBGWEATHER_VERSION_MAJOR)
LIBGWEATHER_LICENSE = LGPL-2.1+
LIBGWEATHER_LICENSE_FILES = COPYING
LIBGWEATHER_INSTALL_STAGING = YES
LIBGWEATHER_DEPENDENCIES = \
	host-pkgconf \
	gobject-introspection \
	geocode-glib \
	json-glib \
	gweather-locations \
	libglib2 \
	libsoup3 \
	libxml2

LIBGWEATHER_CONF_OPTS = \
	-Dgtk_doc=false \
	-Dintrospection=true \
	-Dtests=false \
	-Denable_vala=false

$(eval $(meson-package))
