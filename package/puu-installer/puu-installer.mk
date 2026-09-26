PUU_INSTALLER_VERSION = 1.0.60
PUU_INSTALLER_SOURCE = installer-$(PUU_INSTALLER_VERSION).tar.gz
PUU_INSTALLER_SITE = $(call gitlab,puu-os,installer,$(PUU_INSTALLER_VERSION))
PUU_INSTALLER_LICENSE = GPL-2.0-or-later, BSD-3-Clause
PUU_INSTALLER_LICENSE_FILES = \
	LICENSE \
	subprojects/argtable3/LICENSE
PUU_INSTALLER_DEPENDENCIES = \
	bootc \
	dosfstools \
	e2fsprogs \
	fmt \
	ftxui \
	host-pkgconf \
	json-for-modern-cpp \
	libabseil-cpp \
	openssl \
	spdlog \
	systemd \
	util-linux \
	xfsprogs \
	yaml-cpp

PUU_INSTALLER_EXTRA_DOWNLOADS = \
	https://github.com/argtable/argtable3/releases/download/v3.3.1/argtable-v3.3.1.tar.gz

PUU_INSTALLER_CONF_OPTS = \
	-DBUILD_SHARED_LIBS=OFF \
	-DFETCHCONTENT_FULLY_DISCONNECTED=ON \
	-DFETCHCONTENT_SOURCE_DIR_ARGTABLE3=$(@D)/subprojects/argtable3

define PUU_INSTALLER_STAGE_SUBPROJECTS
	mkdir -p $(@D)/subprojects/argtable3
	$(TAR) -xf $(PUU_INSTALLER_DL_DIR)/argtable-v3.3.1.tar.gz \
		-C $(@D)/subprojects/argtable3 --strip-components=1
endef
PUU_INSTALLER_POST_EXTRACT_HOOKS += PUU_INSTALLER_STAGE_SUBPROJECTS

$(eval $(cmake-package))
