ACCOUNTSSERVICE_VERSION = 26.27.3
ACCOUNTSSERVICE_SITE = https://gitlab.freedesktop.org/accountsservice/accountsservice/-/archive/$(ACCOUNTSSERVICE_VERSION)
ACCOUNTSSERVICE_LICENSE = GPL-3.0+
ACCOUNTSSERVICE_LICENSE_FILES = COPYING
ACCOUNTSSERVICE_INSTALL_STAGING = YES
ACCOUNTSSERVICE_DEPENDENCIES = \
	host-pkgconf \
	dbus \
	gobject-introspection \
	json-c \
	libglib2 \
	polkit

ACCOUNTSSERVICE_CONF_OPTS = \
	-Dintrospection=true \
	-Dvapi=false \
	-Dwtmpfile=/var/log/wtmp

ifeq ($(BR2_INIT_SYSTEMD),y)
ACCOUNTSSERVICE_DEPENDENCIES += systemd
ACCOUNTSSERVICE_CONF_OPTS += -Dsystemdsystemunitdir=/usr/lib/systemd/system
else
ACCOUNTSSERVICE_CONF_OPTS += -Dsystemdsystemunitdir=no
endif

$(eval $(meson-package))
