GLYCIN_VERSION_MAJOR = 2.1
GLYCIN_VERSION = $(GLYCIN_VERSION_MAJOR).5
GLYCIN_SOURCE = glycin-$(GLYCIN_VERSION).tar.xz
GLYCIN_SITE = https://download.gnome.org/sources/glycin/$(GLYCIN_VERSION_MAJOR)
GLYCIN_LICENSE = MPL-2.0 OR LGPL-2.1+, GPL-3.0+ (glycin-jxl)
GLYCIN_LICENSE_FILES = LICENSE-MPL-2.0 LICENSE-LGPL-2.1
GLYCIN_INSTALL_STAGING = YES

# Vendor cargo dependencies from release tarball.
GLYCIN_DOWNLOAD_DEPENDENCIES = host-rustc
GLYCIN_DOWNLOAD_POST_PROCESS = cargo
GLYCIN_DL_ENV = CARGO_HOME=$(BR_CARGO_HOME)

GLYCIN_DEPENDENCIES = \
	host-pkgconf \
	host-rustc \
	libglib2 \
	fontconfig \
	lcms2 \
	libjxl \
	libseccomp

# Build raster and JXL loaders for GNOME 49 backgrounds.
GLYCIN_CONF_OPTS = \
	-Drust-target=$(RUSTC_TARGET_NAME) \
	-Dglycin-loaders=true \
	-Dloaders=glycin-image-rs,glycin-jxl \
	-Dlibglycin=true \
	-Dlibglycin-gtk4=false \
	-Dglycin-thumbnailer=false \
	-Dintrospection=false \
	-Dvapi=false \
	-Dcapi_docs=false \
	-Dtests=false

GLYCIN_MESON_EXTRA_BINARIES += \
	rust=['$(HOST_DIR)/bin/rustc','--target=$(RUSTC_TARGET_NAME)'] \
	rust_ld='$(TARGET_CROSS)gcc'

# Pkg-config configuration for -sys cargo crates.
GLYCIN_CARGO_PKG_CONFIG_ENV = \
	PKG_CONFIG="$(PKG_CONFIG_HOST_BINARY)" \
	PKG_CONFIG_ALLOW_CROSS=1

GLYCIN_CONF_ENV = $(PKG_CARGO_ENV) $(GLYCIN_CARGO_PKG_CONFIG_ENV)
GLYCIN_NINJA_ENV = $(PKG_CARGO_ENV) $(GLYCIN_CARGO_PKG_CONFIG_ENV)

# Provide empty po directory required by release build.
define GLYCIN_CREATE_MISSING_PO_DIR
	mkdir -p $(@D)/po
	touch $(@D)/po/LINGUAS
endef
GLYCIN_POST_EXTRACT_HOOKS += GLYCIN_CREATE_MISSING_PO_DIR

$(eval $(meson-package))
