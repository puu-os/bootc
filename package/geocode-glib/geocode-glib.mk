GEOCODE_GLIB_VERSION_MAJOR = 3.26
GEOCODE_GLIB_VERSION = $(GEOCODE_GLIB_VERSION_MAJOR).4
GEOCODE_GLIB_SOURCE = geocode-glib-$(GEOCODE_GLIB_VERSION).tar.xz
GEOCODE_GLIB_SITE = https://download.gnome.org/sources/geocode-glib/$(GEOCODE_GLIB_VERSION_MAJOR)
GEOCODE_GLIB_LICENSE = LGPL-2.0+
GEOCODE_GLIB_LICENSE_FILES = COPYING.LIB
GEOCODE_GLIB_INSTALL_STAGING = YES
GEOCODE_GLIB_DEPENDENCIES = \
	host-pkgconf \
	json-glib \
	libglib2 \
	libsoup3

GEOCODE_GLIB_CONF_OPTS = \
	-Denable-installed-tests=false \
	-Denable-introspection=false \
	-Denable-gtk-doc=false \
	-Dsoup2=false

$(eval $(meson-package))
