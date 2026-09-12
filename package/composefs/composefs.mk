COMPOSEFS_VERSION = 1.0.8
COMPOSEFS_SITE = https://github.com/composefs/composefs/archive/refs/tags
COMPOSEFS_SOURCE = v$(COMPOSEFS_VERSION).tar.gz
COMPOSEFS_LICENSE = (GPL-2.0-only OR Apache-2.0) AND LGPL-2.1+
COMPOSEFS_LICENSE_FILES = \
	COPYING \
	COPYING.GPL-2.0-only \
	COPYING.GPL-2.0-or-later \
	COPYING.LGPL-2.1-or-later \
	LICENSE.Apache-2.0
COMPOSEFS_INSTALL_STAGING = YES

COMPOSEFS_DEPENDENCIES = \
	host-pkgconf \
	libfuse3 \
	openssl

COMPOSEFS_CONF_OPTS = \
	-Dfuse=enabled \
	-Dman=disabled

$(eval $(meson-package))
