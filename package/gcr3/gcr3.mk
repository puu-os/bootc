GCR3_VERSION = 3.41.2
GCR3_SOURCE = gcr-$(GCR3_VERSION).tar.xz
GCR3_SITE = https://download.gnome.org/sources/gcr/3.41
GCR3_LICENSE = LGPL-2.1+
GCR3_LICENSE_FILES = COPYING
GCR3_INSTALL_STAGING = YES
GCR3_DEPENDENCIES = \
	host-pkgconf \
	libgcrypt \
	libglib2 \
	p11-kit

# UI-less build providing legacy gck-1 and gcr-base-3 for gnome-keyring.
GCR3_CONF_OPTS = \
	-Dgtk=false \
	-Dgtk_doc=false \
	-Dintrospection=false \
	-Dssh_agent=false

ifeq ($(BR2_PACKAGE_GNUPG2),y)
GCR3_CONF_OPTS += -Dgpg_path=/usr/bin/gpg2
else
GCR3_CONF_OPTS += -Dgpg_path=/usr/bin/gpg
endif

$(eval $(meson-package))
