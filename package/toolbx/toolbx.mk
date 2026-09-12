TOOLBX_VERSION = 0.3
TOOLBX_SOURCE = toolbox-$(TOOLBX_VERSION)-vendored.tar.xz
TOOLBX_SITE = https://github.com/containers/toolbox/releases/download/$(TOOLBX_VERSION)
TOOLBX_LICENSE = Apache-2.0
TOOLBX_LICENSE_FILES = COPYING
TOOLBX_DEPENDENCIES = podman shadow

TOOLBX_GOMOD = github.com/containers/toolbox
TOOLBX_BIN_NAME = toolbox

# Prefix ELF interpreter and rpath for /run/host visibility inside containers.
ifeq ($(BR2_x86_64),y)
TOOLBX_DYNAMIC_LINKER = /lib/ld-linux-x86-64.so.2
else ifeq ($(BR2_aarch64),y)
TOOLBX_DYNAMIC_LINKER = /lib/ld-linux-aarch64.so.1
endif

TOOLBX_LDFLAGS = \
	-I /run/host$(TOOLBX_DYNAMIC_LINKER) \
	-linkmode external \
	-r /run/host/lib \
	-X github.com/containers/toolbox/pkg/version.currentVersion=$(TOOLBX_VERSION)

TOOLBX_EXTLDFLAGS = \
	-Wl,--export-dynamic,--unresolved-symbols,ignore-in-object-files,-z,lazy
TOOLBX_LDFLAGS += -extldflags '$(TOOLBX_EXTLDFLAGS)'

TOOLBX_BUILD_OPTS += -mod=vendor

define TOOLBX_BUILD_CMDS
	cd $(@D)/src && \
	$(HOST_GO_TARGET_ENV) \
	$(TOOLBX_GO_ENV) \
	$(GO_BIN) build -v $(TOOLBX_BUILD_OPTS) \
		-ldflags "$(TOOLBX_LDFLAGS)" \
		-o $(@D)/bin/toolbox \
		.
endef

define TOOLBX_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/bin/toolbox \
		$(TARGET_DIR)/usr/bin/toolbox
	$(INSTALL) -D -m 0644 $(@D)/data/config/toolbox.conf \
		$(TARGET_DIR)/etc/containers/toolbox.conf
	$(INSTALL) -D -m 0644 $(@D)/data/tmpfiles.d/toolbox.conf \
		$(TARGET_DIR)/usr/lib/tmpfiles.d/toolbox.conf
	$(INSTALL) -D -m 0644 $(@D)/profile.d/toolbox.sh \
		$(TARGET_DIR)/etc/profile.d/toolbox.sh
endef

$(eval $(golang-package))

# Clear download post-process because release archive is already vendored.
TOOLBX_DOWNLOAD_POST_PROCESS =
