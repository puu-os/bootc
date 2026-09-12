GNOME_KEYRING_VERSION_MAJOR = 50
GNOME_KEYRING_VERSION = $(GNOME_KEYRING_VERSION_MAJOR).0
GNOME_KEYRING_SOURCE = gnome-keyring-$(GNOME_KEYRING_VERSION).tar.xz
GNOME_KEYRING_SITE = https://download.gnome.org/sources/gnome-keyring/$(GNOME_KEYRING_VERSION_MAJOR)
GNOME_KEYRING_LICENSE = GPL-2.0+, LGPL-2.0+
GNOME_KEYRING_LICENSE_FILES = COPYING COPYING.LIB
GNOME_KEYRING_INSTALL_STAGING = YES
GNOME_KEYRING_DEPENDENCIES = \
	host-pkgconf \
	host-libglib2 \
	dbus \
	gcr3 \
	libcap-ng \
	libgcrypt \
	libglib2 \
	linux-pam \
	p11-kit \
	systemd

GNOME_KEYRING_CONF_OPTS = \
	-Dpam=true \
	-Dssh-agent=false \
	-Dselinux=disabled \
	-Dsystemd=enabled \
	-Dmanpage=false

$(eval $(meson-package))
