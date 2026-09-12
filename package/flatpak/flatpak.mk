FLATPAK_VERSION = 1.18.1
FLATPAK_SOURCE = flatpak-$(FLATPAK_VERSION).tar.xz
FLATPAK_SITE = https://github.com/flatpak/flatpak/releases/download/$(FLATPAK_VERSION)
FLATPAK_LICENSE = LGPL-2.1+
FLATPAK_LICENSE_FILES = COPYING
FLATPAK_INSTALL_STAGING = YES

FLATPAK_DEPENDENCIES = \
	host-pkgconf \
	host-python-pyparsing \
	libarchive \
	libcap \
	libostree \
	libcurl \
	libglib2 \
	json-glib \
	libxml2 \
	libxmlb \
	bubblewrap \
	xdg-dbus-proxy \
	fuse-overlayfs \
	appstream \
	dconf \
	gdk-pixbuf \
	libgpgme \
	libseccomp \
	xlib_libXau \
	polkit \
	zstd

ifeq ($(BR2_PACKAGE_SYSTEMD),y)
FLATPAK_DEPENDENCIES += systemd
FLATPAK_CONF_OPTS += -Dsystemd=enabled
else
FLATPAK_CONF_OPTS += -Dsystemd=disabled
endif

FLATPAK_CONF_OPTS += \
	-Ddocbook_docs=disabled \
	-Dgir=disabled \
	-Dgtkdoc=disabled \
	-Dmalcontent=disabled \
	-Dman=disabled \
	-Dselinux_module=disabled \
	-Dtests=false \
	-Dinstalled_tests=false \
	-Dxauth=enabled \
	-Dseccomp=enabled \
	-Dlibzstd=enabled \
	-Dsystem_bubblewrap=/usr/bin/bwrap \
	-Dsystem_dbus_proxy=/usr/bin/xdg-dbus-proxy \
	-Dsystem_fusermount=/usr/bin/fusermount3

$(eval $(meson-package))
