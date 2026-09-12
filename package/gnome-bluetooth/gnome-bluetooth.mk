GNOME_BLUETOOTH_VERSION_MAJOR = 47
GNOME_BLUETOOTH_VERSION = $(GNOME_BLUETOOTH_VERSION_MAJOR).2
GNOME_BLUETOOTH_SOURCE = gnome-bluetooth-$(GNOME_BLUETOOTH_VERSION).tar.xz
GNOME_BLUETOOTH_SITE = https://download.gnome.org/sources/gnome-bluetooth/$(GNOME_BLUETOOTH_VERSION_MAJOR)
GNOME_BLUETOOTH_LICENSE = GPL-2.0+, LGPL-2.1+
GNOME_BLUETOOTH_LICENSE_FILES = COPYING COPYING.LIB
GNOME_BLUETOOTH_INSTALL_STAGING = YES
GNOME_BLUETOOTH_DEPENDENCIES = \
	host-pkgconf \
	gsound \
	libadwaita \
	libgtk4 \
	libglib2 \
	libnotify \
	udev \
	upower

GNOME_BLUETOOTH_CONF_OPTS = \
	-Dgtk_doc=false \
	-Dintrospection=false \
	-Dsendto=false

$(eval $(meson-package))
