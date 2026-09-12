FLANNEL_CNI_PLUGIN_VERSION = v1.9.1-flannel3
FLANNEL_CNI_PLUGIN_SITE = \
	$(call github,flannel-io,cni-plugin,$(FLANNEL_CNI_PLUGIN_VERSION))
FLANNEL_CNI_PLUGIN_LICENSE = Apache-2.0
FLANNEL_CNI_PLUGIN_LICENSE_FILES = LICENSE
FLANNEL_CNI_PLUGIN_GOMOD = github.com/flannel-io/cni-plugin
FLANNEL_CNI_PLUGIN_BIN_NAME = flannel

define FLANNEL_CNI_PLUGIN_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/bin/flannel \
		$(TARGET_DIR)/opt/cni/bin/flannel
endef

$(eval $(golang-package))
