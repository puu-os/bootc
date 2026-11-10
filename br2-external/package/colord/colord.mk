COLORD_VERSION = 1.4.8
COLORD_SOURCE = colord-$(COLORD_VERSION).tar.xz
COLORD_SITE = https://www.freedesktop.org/software/colord/releases
COLORD_LICENSE = GPL-2.0+, LGPL-2.1+
COLORD_LICENSE_FILES = COPYING
COLORD_INSTALL_STAGING = YES
COLORD_DEPENDENCIES = \
	host-colord \
	host-pkgconf \
	dbus \
	hwdata \
	lcms2 \
	libglib2 \
	libgudev \
	libgusb \
	polkit \
	sqlite \
	systemd

COLORD_CONF_OPTS = \
	-Ddaemon=true \
	-Dargyllcms_sensor=false \
	-Dbash_completion=false \
	-Ddocs=false \
	-Dinstalled_tests=false \
	-Dintrospection=false \
	-Dlibcolordcompat=false \
	-Dman=false \
	-Dprint_profiles=false \
	-Dsane=false \
	-Dsession_example=false \
	-Dsystemd=true \
	-Dtests=false \
	-Dudev_rules=true \
	-Dvapi=false

$(eval $(meson-package))

HOST_COLORD_DEPENDENCIES = host-pkgconf host-gettext host-libglib2 host-lcms2
HOST_COLORD_CONF_OPTS = \
	$(filter-out -Ddaemon=true -Dsystemd=true -Dudev_rules=true,$(COLORD_CONF_OPTS)) \
	-Ddaemon=false \
	-Dsystemd=false \
	-Dudev_rules=false

$(eval $(host-meson-package))
