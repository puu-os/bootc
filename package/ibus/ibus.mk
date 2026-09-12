IBUS_VERSION = 1.5.34
IBUS_SITE = https://github.com/ibus/ibus/releases/download/$(IBUS_VERSION)
IBUS_LICENSE = LGPL-2.1+
IBUS_LICENSE_FILES = COPYING
IBUS_INSTALL_STAGING = YES
IBUS_DEPENDENCIES = \
	host-pkgconf \
	host-libglib2 \
	dbus \
	gobject-introspection \
	iso-codes \
	libglib2 \
	libxkbcommon \
	wayland \
	wayland-protocols \
	xkeyboard-config

IBUS_CONF_ENV = \
	PKG_CONFIG_FOR_BUILD="env PKG_CONFIG_LIBDIR=$(HOST_DIR)/lib/pkgconfig:$(HOST_DIR)/share/pkgconfig $(HOST_DIR)/bin/pkgconf"

# Point at staged protocols directory instead of host /usr.
IBUS_MAKE_OPTS += WAYLAND_PRTCLS_DIR=$(STAGING_DIR)/usr/share/wayland-protocols

IBUS_CONF_OPTS = \
	--disable-dconf \
	--disable-engine \
	--disable-gtk2 \
	--disable-gtk3 \
	--disable-gtk4 \
	--disable-setup \
	--disable-tests \
	--disable-xim \
	--enable-introspection=yes \
	--enable-vala=no \
	--disable-appindicator \
	--disable-dbus-python-check \
	--disable-emoji-dict \
	--disable-libnotify \
	--disable-python2 \
	--disable-python-library \
	--disable-unicode-dict \
	--disable-ui \
	--enable-wayland

ifeq ($(BR2_INIT_SYSTEMD),y)
IBUS_DEPENDENCIES += systemd
else
IBUS_CONF_OPTS += --disable-systemd-services
endif

define IBUS_DISABLE_MISSING_CONFIG_COMPONENT
	$(SED) 's|ibus-daemon --replace --panel disable|ibus-daemon --replace --panel disable --config disable|' \
		$(TARGET_DIR)/usr/share/dbus-1/services/org.freedesktop.IBus.service
	$(SED) 's|ibus-daemon --panel disable|ibus-daemon --panel disable --config disable|' \
		$(TARGET_DIR)/usr/lib/systemd/user/org.freedesktop.IBus.session.GNOME.service
endef
IBUS_POST_INSTALL_TARGET_HOOKS += IBUS_DISABLE_MISSING_CONFIG_COMPONENT

$(eval $(autotools-package))
