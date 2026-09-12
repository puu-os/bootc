GNOME_DESKTOP_VERSION_MAJOR = 44
GNOME_DESKTOP_VERSION = $(GNOME_DESKTOP_VERSION_MAJOR).5
GNOME_DESKTOP_SOURCE = gnome-desktop-$(GNOME_DESKTOP_VERSION).tar.xz
GNOME_DESKTOP_SITE = https://download.gnome.org/sources/gnome-desktop/$(GNOME_DESKTOP_VERSION_MAJOR)
GNOME_DESKTOP_LICENSE = GPL-2.0+, LGPL-2.1+
GNOME_DESKTOP_LICENSE_FILES = COPYING COPYING.LIB
GNOME_DESKTOP_INSTALL_STAGING = YES
GNOME_DESKTOP_DEPENDENCIES = \
	host-pkgconf \
	gobject-introspection \
	fontconfig \
	gdk-pixbuf \
	gsettings-desktop-schemas \
	iso-codes \
	libgtk3 \
	libgtk4 \
	libglib2 \
	libseccomp \
	libxkbcommon \
	xkeyboard-config

GNOME_DESKTOP_CONF_OPTS = \
	-Ddesktop_docs=false \
	-Dgtk_doc=false \
	-Dintrospection=true \
	-Dinstalled_tests=false \
	-Dlegacy_library=true \
	-Dbuild_gtk4=true \
	-Ddebug_tools=false \
	-Dudev=disabled

ifeq ($(BR2_INIT_SYSTEMD),y)
GNOME_DESKTOP_CONF_OPTS += -Dsystemd=enabled
GNOME_DESKTOP_DEPENDENCIES += systemd
else
GNOME_DESKTOP_CONF_OPTS += -Dsystemd=disabled
endif

$(eval $(meson-package))
