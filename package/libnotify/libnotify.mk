LIBNOTIFY_VERSION_MAJOR = 0.8
LIBNOTIFY_VERSION = $(LIBNOTIFY_VERSION_MAJOR).8
LIBNOTIFY_SOURCE = libnotify-$(LIBNOTIFY_VERSION).tar.xz
LIBNOTIFY_SITE = https://download.gnome.org/sources/libnotify/$(LIBNOTIFY_VERSION_MAJOR)
LIBNOTIFY_LICENSE = LGPL-2.1+
LIBNOTIFY_LICENSE_FILES = COPYING
LIBNOTIFY_INSTALL_STAGING = YES
LIBNOTIFY_DEPENDENCIES = host-pkgconf libglib2 gdk-pixbuf

LIBNOTIFY_CONF_OPTS = \
	-Dgtk_doc=false \
	-Dman=false \
	-Dintrospection=disabled \
	-Dtests=false

$(eval $(meson-package))
