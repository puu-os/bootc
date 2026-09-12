LIBREST_VERSION_MAJOR = 0.10
LIBREST_VERSION = $(LIBREST_VERSION_MAJOR).2
LIBREST_SOURCE = librest-$(LIBREST_VERSION).tar.xz
LIBREST_SITE = https://download.gnome.org/sources/librest/$(LIBREST_VERSION_MAJOR)
LIBREST_LICENSE = LGPL-2.1+
LIBREST_LICENSE_FILES = COPYING
LIBREST_INSTALL_STAGING = YES
LIBREST_DEPENDENCIES = host-pkgconf json-glib libglib2 libsoup3 libxml2

LIBREST_CONF_OPTS = \
	-Dca_certificates=false \
	-Dintrospection=false \
	-Dvapi=false \
	-Dexamples=false \
	-Dgtk_doc=false \
	-Dsoup2=false \
	-Dtests=false

$(eval $(meson-package))
