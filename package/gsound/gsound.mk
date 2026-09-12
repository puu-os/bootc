GSOUND_VERSION_MAJOR = 1.0
GSOUND_VERSION = $(GSOUND_VERSION_MAJOR).3
GSOUND_SOURCE = gsound-$(GSOUND_VERSION).tar.xz
GSOUND_SITE = https://download.gnome.org/sources/gsound/$(GSOUND_VERSION_MAJOR)
GSOUND_LICENSE = LGPL-2.1+
GSOUND_LICENSE_FILES = COPYING
GSOUND_INSTALL_STAGING = YES
GSOUND_DEPENDENCIES = host-pkgconf libcanberra libglib2

GSOUND_CONF_OPTS = \
	-Dgtk_doc=false \
	-Dintrospection=false \
	-Denable_vala=false

$(eval $(meson-package))
