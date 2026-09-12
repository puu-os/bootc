LIBNMA_VERSION_MAJOR = 1.10
LIBNMA_VERSION = $(LIBNMA_VERSION_MAJOR).6
LIBNMA_SOURCE = libnma-$(LIBNMA_VERSION).tar.xz
LIBNMA_SITE = https://download.gnome.org/sources/libnma/$(LIBNMA_VERSION_MAJOR)
LIBNMA_LICENSE = LGPL-2.1+
LIBNMA_LICENSE_FILES = COPYING
LIBNMA_DEPENDENCIES = host-pkgconf network-manager iso-codes libgtk3 libgtk4

LIBNMA_INSTALL_STAGING = YES

LIBNMA_CONF_OPTS = \
	-Dlibnma_gtk4=true \
	-Dmobile_broadband_provider_info=false \
	-Dgcr=false \
	-Dgtk_doc=false \
	-Dvapi=false \
	-Dmore_asserts=0

ifeq ($(BR2_PACKAGE_GOBJECT_INTROSPECTION),y)
LIBNMA_CONF_OPTS += -Dintrospection=true
LIBNMA_DEPENDENCIES += gobject-introspection
else
LIBNMA_CONF_OPTS += -Dintrospection=false
endif

$(eval $(meson-package))
