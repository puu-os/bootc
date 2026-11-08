# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

# Read after Buildroot's Makefile. Only bounded declarative fields are expanded;
# arbitrary definitions below are inspected strictly with $(value).
BR_PATH = "$(HOST_DIR)/bin:$(HOST_DIR)/sbin:<system-path>"
PUU_STATE_EXPANDED_FIELDS := VERSION DL_VERSION SOURCE SITE SITE_METHOD PATCH \
	SUBDIR OVERRIDE_SRCDIR STRIP_COMPONENTS EXTRACT_OPTS \
	CONF_OPTS CONF_ENV MAKE_OPTS MAKE_ENV CMAKE_OPTS CMAKE_ENV MESON_OPTS MESON_ENV \
	NINJA_OPTS NINJA_ENV BUILD_OPTS BUILD_ENV INSTALL_OPTS INSTALL_ENV \
	INSTALL_TARGET_OPTS INSTALL_STAGING_OPTS INSTALL_HOST_OPTS INSTALL_IMAGES_OPTS \
	AUTORECONF AUTORECONF_OPTS AUTORECONF_ENV GETTEXTIZE LIBTOOL_PATCH \
	SUPPORTS_IN_SOURCE_BUILD CONF_SUBDIR INSTALL_TARGET INSTALL_STAGING INSTALL_IMAGES \
	SETUP_TYPE ENV CFLAGS CXXFLAGS CPPFLAGS LDFLAGS \
	CARGO_ENV CARGO_BUILD_OPTS CARGO_INSTALL_OPTS GO_ENV GO_BUILD_FLAGS GO_LDFLAGS \
	GO_TAGS GO_BUILD_TARGETS BIN_NAME MODULE_SUBDIRS KCONFIG_OPTS \
	KCONFIG_FILE KCONFIG_FRAGMENT_FILES CONFIG_FILE

PUU_STATE_COMMAND_FIELDS := DOWNLOAD_CMDS EXTRACT_CMDS PATCH_CMDS CONFIGURE_CMDS \
	BUILD_CMDS INSTALL_CMDS INSTALL_HOST_CMDS INSTALL_TARGET_CMDS INSTALL_STAGING_CMDS \
	INSTALL_IMAGES_CMDS INSTALL_INIT_SYSV INSTALL_INIT_SYSTEMD INSTALL_INIT_OPENRC \
	USERS PERMISSIONS DEVICES

PUU_STATE_HOOK_FIELDS := $(foreach step,DOWNLOAD EXTRACT RSYNC PATCH PREPARE \
	CONFIGURE BUILD INSTALL INSTALL_HOST INSTALL_TARGET INSTALL_STAGING INSTALL_IMAGES \
	LEGAL_INFO,$(addsuffix _$(step)_HOOKS,PRE POST)) TARGET_FINALIZE_HOOKS ROOTFS_PRE_CMD_HOOKS

PUU_STATE_GLOBAL_FIELDS := BR2_VERSION BR2_VERSION_EPOCH SOURCE_DATE_EPOCH \
	SDK_INSTALL_DIR GNU_TARGET_NAME GNU_HOST_NAME \
	TARGET_DIR HOST_DIR STAGING_DIR BUILD_DIR BASE_DIR BASE_TARGET_DIR PER_PACKAGE_DIR \
	BINARIES_DIR CONFIG_DIR DL_DIR \
	ARCH SUBARCH ABI GCC_TARGET_ARCH GCC_TARGET_ABI GCC_TARGET_CPU GCC_TARGET_FPU \
	TARGET_ABI TARGET_CPU TARGET_CC TARGET_CXX TARGET_LD TARGET_AR TARGET_AS \
	TARGET_CFLAGS TARGET_CXXFLAGS TARGET_CPPFLAGS TARGET_LDFLAGS TARGET_FCFLAGS \
	TARGET_OPTIMIZATION TARGET_DEBUGGING TARGET_CONFIGURE_OPTS TARGET_MAKE_ENV \
	HOSTCC HOSTCXX HOST_CFLAGS HOST_CXXFLAGS HOST_CPPFLAGS HOST_LDFLAGS \
	HOST_CONFIGURE_OPTS HOST_MAKE_ENV TOOLCHAIN_EXTERNAL_PATH TOOLCHAIN_EXTERNAL_PREFIX \
	KEEP_PYTHON_PY_FILES PACKAGES_BUSYBOX_CONFIG_FIXUPS PACKAGES_LINUX_CONFIG_FIXUPS \
	PACKAGES_SELINUX_MODULES PACKAGES_SELINUX_EXTRA_MODULES_DIRS

# The longest registered prefix owns a definition (e.g. FOO_LIBS, not FOO).
PUU_STATE_PACKAGE_PREFIXES := $(sort $(foreach pkg,$(PACKAGES_ALL),$(call UPPERCASE,$(pkg))))
PUU_STATE_VARIABLE_NAMES := $(sort $(foreach v,$(.VARIABLES),$(if $(filter-out environment% default automatic,$(origin $(v))),$(v))))
puu-state-definition-names = $(sort $(filter-out $(1)_FINAL_% %_DEPENDENCIES %_RDEPENDENCIES $(addsuffix _%,$(filter $(1)_%,$(PUU_STATE_PACKAGE_PREFIXES))),$(filter $(1)_%,$(PUU_STATE_VARIABLE_NAMES))))

# $(value) results are passed straight to mk-json-str, never through eval.
define puu-state-package
$(file >$(PUU_PACKAGE_STATE_DIR)/$(1).json,$(call clean-json,{ \
	"info": { $(call json-info,$(call UPPERCASE,$(1))) }$(comma) \
	"prefix": "$(call UPPERCASE,$(1))"$(comma) \
	"expanded": { $(foreach field,$(PUU_STATE_EXPANDED_FIELDS), \
		"$(field)": $(call mk-json-str,$($(call UPPERCASE,$(1))_$(field))),) }$(comma) \
	"raw": { $(foreach field,$(PUU_STATE_COMMAND_FIELDS) $(PUU_STATE_HOOK_FIELDS), \
		"$(field)": $(call mk-json-str,$(value $(call UPPERCASE,$(1))_$(field))),) }$(comma) \
	"definitions": { $(foreach variable,$(call puu-state-definition-names,$(call UPPERCASE,$(1))), \
		"$(variable)": $(call mk-json-str,$(value $(variable))),) }$(comma) \
	"hooks": { $(foreach field,$(PUU_STATE_HOOK_FIELDS), \
		"$(field)": { $(foreach hook,$($(call UPPERCASE,$(1))_$(field)), \
			$(call mk-json-str,$(hook)): $(call mk-json-str,$(value $(hook))),) },) } \
}))
endef

.PHONY: puu-package-state
show-info: puu-package-state
puu-package-state:
	$(if $(PUU_PACKAGE_STATE_DIR),,$(error PUU_PACKAGE_STATE_DIR is required))
	$(file >$(PUU_PACKAGE_STATE_DIR)/globals.json,$(call clean-json,{ $(foreach field,$(PUU_STATE_GLOBAL_FIELDS),"$(field)": $(call mk-json-str,$($(field))),) }))
	$(file >$(PUU_PACKAGE_STATE_DIR)/raw-definitions.json,$(call clean-json,{ "variables": { $(foreach variable,$(filter-out PUU_STATE_% PUU_PACKAGE_STATE_DIR MAKEFLAGS MFLAGS MAKELEVEL MAKE_TERMOUT MAKE_TERMERR DATE BR2_LOCALVERSION BR2_VERSION_FULL BR2_VERSION_GIT_EPOCH CUR_UMASK BR_ORIG_UMASK -*-command-variables-*- -*-eval-flags-*- -%-command-variables-%-,$(PUU_STATE_VARIABLE_NAMES)),$(call mk-json-str,$(variable)): { "value": $(call mk-json-str,$(value $(variable)))$(comma) "flavor": "$(flavor $(variable))" },) } }))
	$(foreach pkg,$(sort $(foreach p,$(PACKAGES) $(TARGETS_ROOTFS),$(p) $($(call UPPERCASE,$(p))_FINAL_RECURSIVE_DEPENDENCIES))),$(if $(filter target host,$($(call UPPERCASE,$(pkg))_TYPE)),$(call puu-state-package,$(pkg))))
	@:
