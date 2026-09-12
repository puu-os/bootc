APPSTREAM_VERSION = 1.1.6
APPSTREAM_SITE = $(call github,ximion,appstream,v$(APPSTREAM_VERSION))
APPSTREAM_LICENSE = LGPL-2.1+
APPSTREAM_LICENSE_FILES = COPYING
APPSTREAM_INSTALL_STAGING = YES

APPSTREAM_COMMON_CONF_OPTS = \
	-Dstemming=false \
	-Dvapi=false \
	-Dqt=false \
	-Dcompose=false \
	-Dapt-support=false \
	-Dgir=false \
	-Ddisplay-detection=none \
	-Dsvg-support=false \
	-Ddocs=false \
	-Dapidocs=false \
	-Dman=false

APPSTREAM_DEPENDENCIES = \
	host-appstream \
	host-gperf \
	host-pkgconf \
	bash-completion \
	libglib2 \
	libcurl \
	libxml2 \
	libfyaml \
	libxmlb \
	zstd

APPSTREAM_CONF_OPTS = $(APPSTREAM_COMMON_CONF_OPTS)

ifeq ($(BR2_PACKAGE_SYSTEMD),y)
APPSTREAM_DEPENDENCIES += systemd
APPSTREAM_CONF_OPTS += -Dsystemd=true
else
APPSTREAM_CONF_OPTS += -Dsystemd=false
endif

HOST_APPSTREAM_DEPENDENCIES = \
	host-gperf \
	host-pkgconf \
	host-libglib2 \
	host-libcurl \
	host-libxml2 \
	host-libfyaml \
	host-libxmlb

HOST_APPSTREAM_CONF_OPTS = $(APPSTREAM_COMMON_CONF_OPTS) \
	-Dsystemd=false \
	-Dbash-completion=false \
	-Dzstd-support=false \
	-Dinstall-docs=false

$(eval $(meson-package))
$(eval $(host-meson-package))
