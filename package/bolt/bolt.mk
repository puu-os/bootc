BOLT_VERSION = 0.9.11
BOLT_SITE = https://gitlab.freedesktop.org/bolt/bolt/-/archive/$(BOLT_VERSION)
BOLT_LICENSE = LGPL-2.1+
BOLT_LICENSE_FILES = COPYING
BOLT_DEPENDENCIES = \
	host-pkgconf \
	libglib2 \
	libgudev \
	polkit \
	systemd

BOLT_CONF_OPTS = \
	-Dinstall-tests=false \
	-Dman=false \
	-Dprivileged-group=wheel

$(eval $(meson-package))
