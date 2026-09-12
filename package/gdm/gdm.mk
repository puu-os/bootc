GDM_VERSION_MAJOR = 50
GDM_VERSION = $(GDM_VERSION_MAJOR).2
GDM_SOURCE = gdm-$(GDM_VERSION).tar.xz
GDM_SITE = https://download.gnome.org/sources/gdm/$(GDM_VERSION_MAJOR)
GDM_LICENSE = GPL-2.0+
GDM_LICENSE_FILES = COPYING
GDM_INSTALL_STAGING = YES
GDM_DEPENDENCIES = \
	host-pkgconf \
	host-dconf \
	accountsservice \
	dbus \
	dconf \
	gnome-session \
	gnome-shell \
	libglib2 \
	linux-pam \
	systemd

GDM_CONF_OPTS = \
	-Dc_std=gnu11 \
	-Dsystemd-journal=true \
	-Dsystemdsystemunitdir=/usr/lib/systemd/system \
	-Dsystemduserunitdir=/usr/lib/systemd/user \
	-Dpam-mod-dir=/usr/lib/security \
	-Drun-dir=/run/gdm \
	-Dlog-dir=/var/log/gdm \
	-Dprofiling=false \
	-Dx11-support=false \
	-Dgdm-xsession=false

ifeq ($(BR2_PACKAGE_PLYMOUTH),y)
GDM_DEPENDENCIES += plymouth
GDM_CONF_OPTS += -Dplymouth=enabled
else
GDM_CONF_OPTS += -Dplymouth=disabled
endif

$(eval $(meson-package))
