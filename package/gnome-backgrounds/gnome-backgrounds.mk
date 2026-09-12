GNOME_BACKGROUNDS_VERSION_MAJOR = 50
GNOME_BACKGROUNDS_VERSION = $(GNOME_BACKGROUNDS_VERSION_MAJOR).0
GNOME_BACKGROUNDS_SOURCE = gnome-backgrounds-$(GNOME_BACKGROUNDS_VERSION).tar.xz
GNOME_BACKGROUNDS_SITE = https://download.gnome.org/sources/gnome-backgrounds/$(GNOME_BACKGROUNDS_VERSION_MAJOR)
GNOME_BACKGROUNDS_LICENSE = CC-BY-SA-3.0
GNOME_BACKGROUNDS_LICENSE_FILES = COPYING
GNOME_BACKGROUNDS_INSTALL_STAGING = YES
GNOME_BACKGROUNDS_DEPENDENCIES = gsettings-desktop-schemas

# Install GNOME JXL background schema override into staging.
define GNOME_BACKGROUNDS_INSTALL_STAGING_CMDS
	$(INSTALL) -D -m 0644 $(GNOME_BACKGROUNDS_PKGDIR)/10-gnome-backgrounds.gschema.override \
		$(STAGING_DIR)/usr/share/glib-2.0/schemas/10-gnome-backgrounds.gschema.override
endef

$(eval $(meson-package))
