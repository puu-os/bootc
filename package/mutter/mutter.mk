MUTTER_VERSION_MAJOR = 50
MUTTER_VERSION = $(MUTTER_VERSION_MAJOR).4
MUTTER_SOURCE = mutter-$(MUTTER_VERSION).tar.xz
MUTTER_SITE = https://download.gnome.org/sources/mutter/$(MUTTER_VERSION_MAJOR)
MUTTER_LICENSE = GPL-2.0+
MUTTER_LICENSE_FILES = COPYING
MUTTER_INSTALL_STAGING = YES
MUTTER_DEPENDENCIES = \
	host-pkgconf \
	host-xlib_libxcvt \
	gdk-pixbuf \
	glycin \
	gobject-introspection \
	gnome-desktop \
	gsettings-desktop-schemas \
	libglib2 \
	graphene \
	libgtk4 \
	wayland \
	wayland-protocols \
	cairo \
	colord \
	libdisplay-info \
	libei \
	libdrm \
	libgrapheme \
	harfbuzz \
	libinput \
	libgudev \
	mesa3d \
	pipewire \
	pixman \
	startup-notification \
	xkeyboard-config \
	xlib_libX11 \
	xlib_libXau \
	xlib_libXcomposite \
	xlib_libXcursor \
	xlib_libXdamage \
	xlib_libXext \
	xlib_libXfixes \
	xlib_libXi \
	xlib_libXinerama \
	xlib_libXrandr \
	libxcb \
	xwayland \
	systemd

MUTTER_CONF_OPTS = \
	-Ddevkit=disabled \
	-Dxwayland=true \
	-Degl_device=true \
	-Dlibwacom=false \
	-Dsound_player=false \
	-Dnative_backend=true \
	-Dprofiler=false \
	-Dintrospection=true \
	-Dtests=disabled \
	-Dbash_completion=false \
	-Dman=false \
	-Ddocs=false

# Pre-populate subprojects/gvdb to prevent wrap-fetch during offline build.
MUTTER_GVDB_REVISION = b54bc5da25127ef416858a3ad92e57159ff565b3
MUTTER_EXTRA_DOWNLOADS = \
	https://gitlab.gnome.org/GNOME/gvdb/-/archive/$(MUTTER_GVDB_REVISION)/gvdb-$(MUTTER_GVDB_REVISION).tar.bz2

define MUTTER_INSTALL_GVDB_SUBPROJECT
	rm -rf $(@D)/subprojects/gvdb
	mkdir -p $(@D)/subprojects/gvdb
	tar -xjf $(MUTTER_DL_DIR)/gvdb-$(MUTTER_GVDB_REVISION).tar.bz2 \
		-C $(@D)/subprojects/gvdb --strip-components=1
endef
MUTTER_POST_EXTRACT_HOOKS += MUTTER_INSTALL_GVDB_SUBPROJECT

# Provide fallback header for dma-buf sync-file UAPI.
define MUTTER_INJECT_DMA_BUF_SYNC_UAPI
	printf '%s\n' \
		'#ifndef DMA_BUF_IOCTL_EXPORT_SYNC_FILE_FALLBACK_H' \
		'#define DMA_BUF_IOCTL_EXPORT_SYNC_FILE_FALLBACK_H' \
		'#include <linux/dma-buf.h>' \
		'#ifndef DMA_BUF_IOCTL_EXPORT_SYNC_FILE' \
		'struct dma_buf_export_sync_file { __u32 flags; __s32 fd; };' \
		'#define DMA_BUF_IOCTL_EXPORT_SYNC_FILE _IOWR(DMA_BUF_BASE, 2, struct dma_buf_export_sync_file)' \
		'#endif' \
		'#endif' \
		> $(@D)/src/wayland/dma-buf-fallback.h
	$(SED) 's|^#include <linux/dma-buf.h>$$|#include "wayland/dma-buf-fallback.h"|' \
		$(@D)/src/wayland/meta-wayland-dma-buf.c
endef
MUTTER_POST_PATCH_HOOKS += MUTTER_INJECT_DMA_BUF_SYNC_UAPI

$(eval $(meson-package))
