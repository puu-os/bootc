LIBEI_VERSION = 1.6.0
LIBEI_SOURCE = libei-$(LIBEI_VERSION).tar.bz2
LIBEI_SITE = https://gitlab.freedesktop.org/libinput/libei/-/archive/$(LIBEI_VERSION)
LIBEI_LICENSE = MIT
LIBEI_LICENSE_FILES = COPYING
LIBEI_INSTALL_STAGING = YES
LIBEI_DEPENDENCIES = \
	host-pkgconf \
	libevdev \
	libxkbcommon \
	systemd

LIBEI_CONF_OPTS = \
	-Dtests=disabled \
	-Dliboeffis=enabled \
	-Dlibeis=enabled \
	-Dlibei=enabled \
	-Dsd-bus-provider=libsystemd \
	-Ddocumentation=[]

$(eval $(meson-package))
