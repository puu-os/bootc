# SPDX-License-Identifier: GPL-2.0-or-later

PUU_GATEWAY_CRDS_GATEWAY_VERSION = 1.6.1
PUU_GATEWAY_CRDS_AGENTGATEWAY_VERSION = 1.2.0
PUU_GATEWAY_CRDS_VERSION = $(PUU_GATEWAY_CRDS_GATEWAY_VERSION)+$(PUU_GATEWAY_CRDS_AGENTGATEWAY_VERSION)
PUU_GATEWAY_CRDS_SOURCE = experimental-install.yaml
PUU_GATEWAY_CRDS_SITE = https://github.com/kubernetes-sigs/gateway-api/releases/download/v$(PUU_GATEWAY_CRDS_GATEWAY_VERSION)
PUU_GATEWAY_CRDS_LICENSE = Apache-2.0
PUU_GATEWAY_CRDS_LICENSE_FILES = LICENSE

# The package only installs data files and creates their directories.
PUU_GATEWAY_CRDS_ADD_TOOLCHAIN_DEPENDENCY = NO
PUU_GATEWAY_CRDS_ADD_SKELETON_DEPENDENCY = NO
PUU_GATEWAY_CRDS_ADD_CCACHE_DEPENDENCY = NO

PUU_GATEWAY_CRDS_AGENTGATEWAY_SITE = https://raw.githubusercontent.com/agentgateway/agentgateway/v$(PUU_GATEWAY_CRDS_AGENTGATEWAY_VERSION)/controller/install/helm/agentgateway-crds/templates
PUU_GATEWAY_CRDS_AGENTGATEWAY_FILES = \
	agentgateway.dev_agentgatewaybackends.yaml \
	agentgateway.dev_agentgatewayparameters.yaml \
	agentgateway.dev_agentgatewaypolicies.yaml
PUU_GATEWAY_CRDS_EXTRA_DOWNLOADS = $(addprefix $(PUU_GATEWAY_CRDS_AGENTGATEWAY_SITE)/,$(PUU_GATEWAY_CRDS_AGENTGATEWAY_FILES))

# The downloads are plain YAML files, not archives.
define PUU_GATEWAY_CRDS_EXTRACT_CMDS
	cp $(PUU_GATEWAY_CRDS_PKGDIR)/LICENSE $(@D)/LICENSE
	cp $(addprefix $(PUU_GATEWAY_CRDS_DL_DIR)/,$(PUU_GATEWAY_CRDS_SOURCE) $(PUU_GATEWAY_CRDS_AGENTGATEWAY_FILES)) $(@D)/
endef

PUU_GATEWAY_CRDS_TARGET_DIR = $(TARGET_DIR)/usr/share/puu/k3s/crds

define PUU_GATEWAY_CRDS_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0644 $(@D)/$(PUU_GATEWAY_CRDS_SOURCE) \
		$(PUU_GATEWAY_CRDS_TARGET_DIR)/10-gateway-api-experimental-install.yaml
	set -e; for file in $(PUU_GATEWAY_CRDS_AGENTGATEWAY_FILES); do \
		$(INSTALL) -D -m 0644 $(@D)/$$file \
			$(PUU_GATEWAY_CRDS_TARGET_DIR)/12-$$file; \
	done
endef

$(eval $(generic-package))
