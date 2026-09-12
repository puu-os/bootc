DCONF_VERSION_MAJOR = 0.40
DCONF_VERSION = $(DCONF_VERSION_MAJOR).0
DCONF_SOURCE = dconf-$(DCONF_VERSION).tar.xz
DCONF_SITE = https://download.gnome.org/sources/dconf/$(DCONF_VERSION_MAJOR)
DCONF_LICENSE = LGPL-2.1+
DCONF_LICENSE_FILES = COPYING
DCONF_INSTALL_STAGING = YES
DCONF_DEPENDENCIES = host-pkgconf host-dconf dbus libglib2

DCONF_CONF_OPTS = \
	-Dbash_completion=false \
	-Dman=false \
	-Dgtk_doc=false \
	-Dvapi=false

ifeq ($(BR2_INIT_SYSTEMD),y)
DCONF_DEPENDENCIES += systemd
endif

HOST_DCONF_DEPENDENCIES = host-pkgconf host-libglib2 host-dbus
HOST_DCONF_CONF_OPTS = \
	-Dbash_completion=false \
	-Dman=false \
	-Dgtk_doc=false \
	-Dvapi=false \
	-Dsystemduserunitdir=$(HOST_DIR)/lib/systemd/user

$(eval $(meson-package))
$(eval $(host-meson-package))
