GNOME_SOFTWARE_VERSION_MAJOR = 50
GNOME_SOFTWARE_VERSION = $(GNOME_SOFTWARE_VERSION_MAJOR).3
GNOME_SOFTWARE_SOURCE = gnome-software-$(GNOME_SOFTWARE_VERSION).tar.xz
GNOME_SOFTWARE_SITE = https://download.gnome.org/sources/gnome-software/$(GNOME_SOFTWARE_VERSION_MAJOR)
GNOME_SOFTWARE_LICENSE = GPL-2.0+
GNOME_SOFTWARE_LICENSE_FILES = COPYING
GNOME_SOFTWARE_INSTALL_STAGING = YES
GNOME_SOFTWARE_DEPENDENCIES = \
	host-desktop-file-utils \
	host-pkgconf \
	appstream \
	flatpak \
	fwupd \
	gdk-pixbuf \
	gsettings-desktop-schemas \
	json-glib \
	libadwaita \
	libglib2 \
	libgtk4 \
	libsoup3 \
	libxmlb \
	polkit

GNOME_SOFTWARE_CONF_OPTS = \
	-Dtests=false \
	-Dman=false \
	-Dpackagekit=false \
	-Dpolkit=true \
	-Deos_updater=false \
	-Dfwupd=true \
	-Dflatpak=true \
	-Dmalcontent=false \
	-Drpm_ostree=false \
	-Dwebapps=false \
	-Dhardcoded_foss_webapps=false \
	-Dhardcoded_proprietary_webapps=false \
	-Dgudev=false \
	-Dapt=false \
	-Dsnap=false \
	-Dexternal_appstream=false \
	-Dgtk_doc=false \
	-Dhardcoded_curated=false \
	-Ddefault_featured_apps=false \
	-Dmogwai=false \
	-Dsysprof=disabled \
	-Dopensuse-distro-upgrade=false \
	-Ddkms=false \
	-Dsystemd-sysupdate=false \
	-Dhelp=false

$(eval $(meson-package))
