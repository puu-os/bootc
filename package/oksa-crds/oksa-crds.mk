OKSA_CRDS_VERSION = 1.0
OKSA_CRDS_GATEWAY_API_VERSION = 1.6.1
OKSA_CRDS_GATEWAY_API_INFERENCE_EXTENSION_VERSION = 1.6.0
OKSA_CRDS_AGENTGATEWAY_VERSION = 1.2.0
OKSA_CRDS_SOURCE = experimental-install.yaml
OKSA_CRDS_SITE = https://github.com/kubernetes-sigs/gateway-api/releases/download/v$(OKSA_CRDS_GATEWAY_API_VERSION)
OKSA_CRDS_AGENTGATEWAY_SITE = https://raw.githubusercontent.com/agentgateway/agentgateway/v$(OKSA_CRDS_AGENTGATEWAY_VERSION)/controller/install/helm/agentgateway-crds/templates
OKSA_CRDS_AGENTGATEWAY_CRDS = \
	agentgateway.dev_agentgatewaybackends.yaml \
	agentgateway.dev_agentgatewayparameters.yaml \
	agentgateway.dev_agentgatewaypolicies.yaml
OKSA_CRDS_LICENSE = Apache-2.0
OKSA_CRDS_LICENSE_FILES = LICENSE
OKSA_CRDS_EXTRA_DOWNLOADS = \
	https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/v$(OKSA_CRDS_GATEWAY_API_INFERENCE_EXTENSION_VERSION)/manifests.yaml \
	$(addprefix $(OKSA_CRDS_AGENTGATEWAY_SITE)/,$(OKSA_CRDS_AGENTGATEWAY_CRDS))

define OKSA_CRDS_EXTRACT_CMDS
	$(INSTALL) -D -m 0644 $(OKSA_CRDS_PKGDIR)/LICENSE \
		$(@D)/LICENSE
endef

define OKSA_CRDS_INSTALL_TARGET_CMDS
	mkdir -p $(TARGET_DIR)/usr/share/puu-os/k3s/crds
	$(INSTALL) -D -m 0644 $(OKSA_CRDS_DL_DIR)/experimental-install.yaml \
		$(TARGET_DIR)/usr/share/puu-os/k3s/crds/10-gateway-api-experimental-install.yaml
	$(INSTALL) -D -m 0644 $(OKSA_CRDS_DL_DIR)/manifests.yaml \
		$(TARGET_DIR)/usr/share/puu-os/k3s/crds/11-gateway-api-inference-extension-manifests.yaml
	set -e; for crd in $(OKSA_CRDS_AGENTGATEWAY_CRDS); do \
		$(INSTALL) -D -m 0644 $(OKSA_CRDS_DL_DIR)/$$crd \
			$(TARGET_DIR)/usr/share/puu-os/k3s/crds/12-$$crd; \
	done
endef

$(eval $(generic-package))
