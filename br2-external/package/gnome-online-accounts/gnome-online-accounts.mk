GNOME_ONLINE_ACCOUNTS_VERSION_MAJOR = 3.58
GNOME_ONLINE_ACCOUNTS_VERSION = $(GNOME_ONLINE_ACCOUNTS_VERSION_MAJOR).1
GNOME_ONLINE_ACCOUNTS_SOURCE = gnome-online-accounts-$(GNOME_ONLINE_ACCOUNTS_VERSION).tar.xz
GNOME_ONLINE_ACCOUNTS_SITE = https://download.gnome.org/sources/gnome-online-accounts/$(GNOME_ONLINE_ACCOUNTS_VERSION_MAJOR)
GNOME_ONLINE_ACCOUNTS_LICENSE = LGPL-2.0+
GNOME_ONLINE_ACCOUNTS_LICENSE_FILES = COPYING
GNOME_ONLINE_ACCOUNTS_INSTALL_STAGING = YES
GNOME_ONLINE_ACCOUNTS_DEPENDENCIES = \
	host-pkgconf \
	dbus \
	json-glib \
	libadwaita \
	libgtk4 \
	libglib2 \
	librest \
	libsecret \
	libsoup3 \
	libxml2

GNOME_ONLINE_ACCOUNTS_CONF_OPTS = \
	-Dgoabackend=true \
	-Dexchange=false \
	-Dfedora=false \
	-Dgoogle=false \
	-Dimap_smtp=false \
	-Dkerberos=false \
	-Dms_graph=false \
	-Downcloud=false \
	-Dwebdav=false \
	-Dintrospection=false \
	-Dvapi=false \
	-Ddocumentation=false \
	-Dman=false

$(eval $(meson-package))
