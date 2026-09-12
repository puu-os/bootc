GNOME_SETTINGS_DAEMON_VERSION_MAJOR = 50
GNOME_SETTINGS_DAEMON_VERSION = $(GNOME_SETTINGS_DAEMON_VERSION_MAJOR).1
GNOME_SETTINGS_DAEMON_SOURCE = gnome-settings-daemon-$(GNOME_SETTINGS_DAEMON_VERSION).tar.xz
GNOME_SETTINGS_DAEMON_SITE = https://download.gnome.org/sources/gnome-settings-daemon/$(GNOME_SETTINGS_DAEMON_VERSION_MAJOR)
GNOME_SETTINGS_DAEMON_LICENSE = GPL-2.0+
GNOME_SETTINGS_DAEMON_LICENSE_FILES = COPYING
GNOME_SETTINGS_DAEMON_INSTALL_STAGING = YES
GNOME_SETTINGS_DAEMON_DEPENDENCIES = \
	host-pkgconf \
	alsa-lib \
	dbus \
	fontconfig \
	geoclue \
	geocode-glib \
	gnome-desktop \
	gsettings-desktop-schemas \
	libgudev \
	libgtk3 \
	libcanberra \
	libgweather \
	libnotify \
	network-manager \
	pango \
	pipewire \
	polkit \
	pulseaudio \
	systemd \
	udev \
	upower \
	libglib2 \
	wayland \
	lcms2 \
	xlib_libX11 \
	xlib_libXext \
	xlib_libXfixes \
	xlib_libXi

GNOME_SETTINGS_DAEMON_CONF_OPTS = \
	-Dsystemd=true \
	-Dsmartcard=false \
	-Dnetwork_manager=true \
	-Dcups=false \
	-Dwwan=false \
	-Dcolord=false

$(eval $(meson-package))
