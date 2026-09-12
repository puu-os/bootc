XDG_DESKTOP_PORTAL_GTK_VERSION = 1.15.3
XDG_DESKTOP_PORTAL_GTK_SOURCE = xdg-desktop-portal-gtk-$(XDG_DESKTOP_PORTAL_GTK_VERSION).tar.xz
XDG_DESKTOP_PORTAL_GTK_SITE = https://github.com/flatpak/xdg-desktop-portal-gtk/releases/download/$(XDG_DESKTOP_PORTAL_GTK_VERSION)
XDG_DESKTOP_PORTAL_GTK_LICENSE = LGPL-2.1+
XDG_DESKTOP_PORTAL_GTK_LICENSE_FILES = COPYING
XDG_DESKTOP_PORTAL_GTK_DEPENDENCIES = \
	host-pkgconf \
	libglib2 \
	libgtk3 \
	xdg-desktop-portal

XDG_DESKTOP_PORTAL_GTK_CONF_OPTS = \
	-Dappchooser=disabled \
	-Dlockdown=disabled \
	-Dsettings=disabled \
	-Dwallpaper=disabled

$(eval $(meson-package))
