LIBWACOM_VERSION = 2.19.1
LIBWACOM_SOURCE = libwacom-$(LIBWACOM_VERSION).tar.xz
LIBWACOM_SITE = https://github.com/linuxwacom/libwacom/releases/download/libwacom-$(LIBWACOM_VERSION)
LIBWACOM_LICENSE = HPND
LIBWACOM_LICENSE_FILES = COPYING
LIBWACOM_INSTALL_STAGING = YES
LIBWACOM_DEPENDENCIES = host-pkgconf libevdev libglib2 libgudev

LIBWACOM_CONF_OPTS = \
	-Ddocumentation=disabled \
	-Dtests=disabled

$(eval $(meson-package))
