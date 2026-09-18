PUU_INSTALLER_VERSION = 1.0.57
PUU_INSTALLER_SOURCE = installer-$(PUU_INSTALLER_VERSION).tar.gz
PUU_INSTALLER_SITE = $(call gitlab,puu-os,installer,$(PUU_INSTALLER_VERSION))
PUU_INSTALLER_LICENSE = GPL-2.0-or-later, BSD-3-Clause, MIT
PUU_INSTALLER_LICENSE_FILES = \
	LICENSE \
	subprojects/argtable3/LICENSE \
	subprojects/fmt/LICENSE \
	subprojects/ftxui/LICENSE \
	subprojects/nlohmann-json/LICENSE.MIT \
	subprojects/spdlog/LICENSE \
	subprojects/yaml-cpp/LICENSE
PUU_INSTALLER_DEPENDENCIES = \
	bootc \
	dosfstools \
	e2fsprogs \
	host-pkgconf \
	libabseil-cpp \
	openssl \
	systemd \
	util-linux \
	xfsprogs

PUU_INSTALLER_EXTRA_DOWNLOADS = \
	https://github.com/argtable/argtable3/releases/download/v3.3.1/argtable-v3.3.1.tar.gz \
	https://github.com/fmtlib/fmt/archive/12.0.0.tar.gz \
	https://github.com/ArthurSonzogni/FTXUI/archive/v6.1.9.tar.gz \
	https://github.com/nlohmann/json/releases/download/v3.12.0/json.tar.xz \
	https://github.com/gabime/spdlog/archive/v1.17.0.tar.gz \
	https://github.com/jbeder/yaml-cpp/archive/0.8.0.zip

PUU_INSTALLER_CONF_OPTS = \
	-DBUILD_SHARED_LIBS=OFF \
	-DFETCHCONTENT_FULLY_DISCONNECTED=ON \
	-DFETCHCONTENT_SOURCE_DIR_ARGTABLE3=$(@D)/subprojects/argtable3 \
	-DFETCHCONTENT_SOURCE_DIR_FMT=$(@D)/subprojects/fmt \
	-DFETCHCONTENT_SOURCE_DIR_FTXUI=$(@D)/subprojects/ftxui \
	-DFETCHCONTENT_SOURCE_DIR_NLOHMANN_JSON=$(@D)/subprojects/nlohmann-json \
	-DFETCHCONTENT_SOURCE_DIR_SPDLOG=$(@D)/subprojects/spdlog \
	-DFETCHCONTENT_SOURCE_DIR_YAML-CPP=$(@D)/subprojects/yaml-cpp

define PUU_INSTALLER_STAGE_SUBPROJECTS
	mkdir -p \
		$(@D)/subprojects/argtable3 \
		$(@D)/subprojects/fmt \
		$(@D)/subprojects/ftxui \
		$(@D)/subprojects/nlohmann-json \
		$(@D)/subprojects/spdlog \
		$(@D)/subprojects/yaml-cpp
	$(TAR) -xf $(PUU_INSTALLER_DL_DIR)/argtable-v3.3.1.tar.gz \
		-C $(@D)/subprojects/argtable3 --strip-components=1
	$(TAR) -xf $(PUU_INSTALLER_DL_DIR)/12.0.0.tar.gz \
		-C $(@D)/subprojects/fmt --strip-components=1
	$(TAR) -xf $(PUU_INSTALLER_DL_DIR)/v6.1.9.tar.gz \
		-C $(@D)/subprojects/ftxui --strip-components=1
	$(TAR) -xf $(PUU_INSTALLER_DL_DIR)/json.tar.xz \
		-C $(@D)/subprojects/nlohmann-json --strip-components=1
	$(TAR) -xf $(PUU_INSTALLER_DL_DIR)/v1.17.0.tar.gz \
		-C $(@D)/subprojects/spdlog --strip-components=1
	$(UNZIP) -q $(PUU_INSTALLER_DL_DIR)/0.8.0.zip \
		-d $(@D)/subprojects/yaml-cpp
	cp -a $(@D)/subprojects/yaml-cpp/yaml-cpp-0.8.0/. \
		$(@D)/subprojects/yaml-cpp/
	rm -rf $(@D)/subprojects/yaml-cpp/yaml-cpp-0.8.0
endef
PUU_INSTALLER_POST_EXTRACT_HOOKS += PUU_INSTALLER_STAGE_SUBPROJECTS

$(eval $(cmake-package))
