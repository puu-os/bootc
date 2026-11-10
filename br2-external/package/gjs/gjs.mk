GJS_VERSION_MAJOR = 1.88
GJS_VERSION = $(GJS_VERSION_MAJOR).1
GJS_SOURCE = gjs-$(GJS_VERSION).tar.xz
GJS_SITE = https://download.gnome.org/sources/gjs/$(GJS_VERSION_MAJOR)
GJS_LICENSE = MIT OR LGPL-2.0+
GJS_LICENSE_FILES = COPYING
GJS_INSTALL_STAGING = YES
GJS_DEPENDENCIES = \
	host-pkgconf \
	mozjs140 \
	libglib2 \
	cairo \
	gobject-introspection

GJS_CONF_OPTS = \
	-Dinstalled_tests=false \
	-Dprofiler=disabled \
	-Dskip_gtk_tests=true \
	-Dskip_dbus_tests=true \
	-Dreadline=disabled

$(eval $(meson-package))
