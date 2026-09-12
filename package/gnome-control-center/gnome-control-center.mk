GNOME_CONTROL_CENTER_VERSION_MAJOR = 50
GNOME_CONTROL_CENTER_VERSION = $(GNOME_CONTROL_CENTER_VERSION_MAJOR).4
GNOME_CONTROL_CENTER_SOURCE = gnome-control-center-$(GNOME_CONTROL_CENTER_VERSION).tar.xz
GNOME_CONTROL_CENTER_SITE = https://download.gnome.org/sources/gnome-control-center/$(GNOME_CONTROL_CENTER_VERSION_MAJOR)
GNOME_CONTROL_CENTER_LICENSE = GPL-2.0+
GNOME_CONTROL_CENTER_LICENSE_FILES = COPYING
GNOME_CONTROL_CENTER_INSTALL_STAGING = YES
GNOME_CONTROL_CENTER_DEPENDENCIES = \
	host-blueprint-compiler \
	host-pkgconf \
	accountsservice \
	bolt \
	colord \
	colord-gtk \
	cups \
	dbus \
	gcr \
	gdk-pixbuf \
	gnutls \
	gnome-bluetooth \
	gnome-desktop \
	gnome-online-accounts \
	gnome-settings-daemon \
	gsettings-desktop-schemas \
	gsound \
	ibus \
	json-glib \
	libepoxy \
	libgudev \
	libgtop \
	libgtk4 \
	libadwaita \
	libglib2 \
	libkrb5 \
	libnma \
	libpwquality \
	libwacom \
	libxml2 \
	modem-manager \
	network-manager \
	pulseaudio \
	polkit \
	udisks \
	upower \
	xlib_libX11 \
	xlib_libXi

# Point XDG data search at target sysroot.
GNOME_CONTROL_CENTER_CONF_ENV = \
	XDG_DATA_DIRS=$(STAGING_DIR)/usr/share:$(HOST_DIR)/share

GNOME_CONTROL_CENTER_CONF_OPTS = \
	-Ddocumentation=false \
	-Dtests=false \
	-Dibus=true \
	-Dmalcontent=false \
	-Dlocation-services=disabled \
	-Dsnap=false

# Pre-populate subprojects/tecla to prevent wrap-fetch during offline build.
GNOME_CONTROL_CENTER_TECLA_VERSION = 50.0
GNOME_CONTROL_CENTER_EXTRA_DOWNLOADS = \
	https://download.gnome.org/sources/tecla/50/tecla-$(GNOME_CONTROL_CENTER_TECLA_VERSION).tar.xz

define GNOME_CONTROL_CENTER_INSTALL_TECLA_SUBPROJECT
	rm -rf $(@D)/subprojects/tecla
	mkdir -p $(@D)/subprojects/tecla
	tar -xJf $(GNOME_CONTROL_CENTER_DL_DIR)/tecla-$(GNOME_CONTROL_CENTER_TECLA_VERSION).tar.xz \
		-C $(@D)/subprojects/tecla --strip-components=1
endef
GNOME_CONTROL_CENTER_POST_EXTRACT_HOOKS += GNOME_CONTROL_CENTER_INSTALL_TECLA_SUBPROJECT

# Stub out Samba printer discovery helper.
define GNOME_CONTROL_CENTER_INSTALL_SAMBA_STUB
	cp $(GNOME_CONTROL_CENTER_PKGDIR)/pp-samba-stub.c $(@D)/panels/printers/pp-samba.c
endef
GNOME_CONTROL_CENTER_POST_PATCH_HOOKS += GNOME_CONTROL_CENTER_INSTALL_SAMBA_STUB

# Locate GObject-introspection typelibs for blueprint-compiler.
GNOME_CONTROL_CENTER_NINJA_ENV = GI_TYPELIB_PATH=$(STAGING_DIR)/usr/lib/girepository-1.0

$(eval $(meson-package))
