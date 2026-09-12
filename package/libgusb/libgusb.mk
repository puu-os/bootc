LIBGUSB_VERSION = 0.4.9
LIBGUSB_SOURCE = libgusb-$(LIBGUSB_VERSION).tar.xz
LIBGUSB_SITE = https://github.com/hughsie/libgusb/releases/download/$(LIBGUSB_VERSION)
LIBGUSB_LICENSE = LGPL-2.1+
LIBGUSB_LICENSE_FILES = COPYING
LIBGUSB_INSTALL_STAGING = YES
LIBGUSB_DEPENDENCIES = \
	host-pkgconf \
	json-glib \
	libglib2 \
	libusb

LIBGUSB_CONF_OPTS = \
	-Ddocs=false \
	-Dintrospection=false \
	-Dtests=false \
	-Dvapi=false \
	-Dumockdev=disabled

$(eval $(meson-package))
