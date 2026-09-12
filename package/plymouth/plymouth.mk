PLYMOUTH_VERSION = 26.134.222
PLYMOUTH_SITE = https://gitlab.freedesktop.org/plymouth/plymouth/-/archive/$(PLYMOUTH_VERSION)
PLYMOUTH_LICENSE = GPL-2.0+
PLYMOUTH_LICENSE_FILES = COPYING
PLYMOUTH_INSTALL_STAGING = YES
PLYMOUTH_DEPENDENCIES = \
	host-pkgconf \
	cairo \
	freetype \
	libdrm \
	libevdev \
	libpng \
	libxkbcommon \
	pango \
	xkeyboard-config \
	systemd

PLYMOUTH_CONF_OPTS = \
	-Ddocs=false \
	-Ddrm=true \
	-Dfreetype=enabled \
	-Dgtk=disabled \
	-Dpango=enabled \
	-Dsystemd-integration=true \
	-Dsystemd-ask-password-agent=/usr/bin/systemd-tty-ask-password-agent \
	-Dtracing=false \
	-Dudev=enabled

$(eval $(meson-package))
