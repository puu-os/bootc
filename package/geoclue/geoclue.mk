GEOCLUE_VERSION = 2.8.2
GEOCLUE_SITE = https://gitlab.freedesktop.org/geoclue/geoclue/-/archive/$(GEOCLUE_VERSION)
GEOCLUE_SOURCE = geoclue-$(GEOCLUE_VERSION).tar.bz2
GEOCLUE_LICENSE = LGPL-2.1+
GEOCLUE_LICENSE_FILES = COPYING
GEOCLUE_INSTALL_STAGING = YES
GEOCLUE_DEPENDENCIES = \
	host-pkgconf \
	avahi \
	dbus \
	json-glib \
	libglib2 \
	libsoup3 \
	modem-manager

GEOCLUE_CONF_OPTS = \
	-Dgtk-doc=false \
	-Dintrospection=false \
	-Dvapi=false \
	-D3g-source=true \
	-Dcdma-source=true \
	-Dmodem-gps-source=true \
	-Ddemo-agent=false

ifeq ($(BR2_PACKAGE_SYSTEMD),y)
GEOCLUE_DEPENDENCIES += systemd
endif

$(eval $(meson-package))
