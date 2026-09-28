OKSA_SERVICES_VERSION = 1.0
OKSA_SERVICES_SITE = $(OKSA_SERVICES_PKGDIR)/files
OKSA_SERVICES_SITE_METHOD = local
OKSA_SERVICES_LICENSE = GPL-2.0+
OKSA_SERVICES_LICENSE_FILES = LICENSE
OKSA_SERVICES_DEPENDENCIES = k3s oksa-crds

OKSA_SERVICES_FILES = $(OKSA_SERVICES_PKGDIR)/files

ifeq ($(BR2_aarch64),y)
OKSA_SERVICES_BONSAI_MANIFEST_DIR = arm64/
else
OKSA_SERVICES_BONSAI_MANIFEST_DIR =
endif

define OKSA_SERVICES_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(OKSA_SERVICES_FILES)/bin/model_serving.py \
		$(TARGET_DIR)/usr/libexec/puu/model_serving.py
	$(INSTALL) -D -m 0755 $(OKSA_SERVICES_FILES)/bin/install-k3s-addons \
		$(TARGET_DIR)/usr/libexec/puu/install-k3s-addons
	$(INSTALL) -D -m 0755 $(OKSA_SERVICES_FILES)/bin/oksa \
		$(TARGET_DIR)/usr/bin/oksa
	for executable in discover-vllm-models label-model-serving-node \
		collect-nvlink-topology plan-vllm-workloads preseed-vllm-models; do \
		$(INSTALL) -D -m 0755 $(OKSA_SERVICES_FILES)/bin/$$executable \
			$(TARGET_DIR)/usr/libexec/puu/$$executable || exit $$?; \
	done
	$(INSTALL) -D -m 0644 $(OKSA_SERVICES_FILES)/oksa-services.conf \
		$(TARGET_DIR)/usr/lib/tmpfiles.d/oksa-services.conf
	$(foreach manifest,20-agentgateway.yaml 21-llm-d-gateway.yaml \
		30-llm-d.yaml 30-nvidia-cdi-runtime.yaml 32-nvidia-device-plugin.yaml \
		40-litellm.yaml 45-open-webui.yaml,\
		$(INSTALL) -D -m 0644 $(OKSA_SERVICES_FILES)/manifests/$(manifest) \
			$(TARGET_DIR)/usr/share/puu/k3s/manifests/$(manifest)$(sep))
	$(INSTALL) -D -m 0644 $(OKSA_SERVICES_FILES)/manifests/$(OKSA_SERVICES_BONSAI_MANIFEST_DIR)33-bonsai-2-27b.yaml \
		$(TARGET_DIR)/usr/share/puu/k3s/manifests/33-bonsai-2-27b.yaml
	$(foreach catalog,$(notdir $(wildcard $(OKSA_SERVICES_FILES)/vllm/catalog.d/*.env)),\
		$(INSTALL) -D -m 0644 $(OKSA_SERVICES_FILES)/vllm/catalog.d/$(catalog) \
			$(TARGET_DIR)/usr/share/puu/vllm/catalog.d/$(catalog)$(sep))
	$(SED) 's/^PUU_VLLM_AUTOSTART=true$$/PUU_VLLM_AUTOSTART=false/' \
		$(TARGET_DIR)/usr/share/puu/vllm/catalog.d/qwen3.8-27b-nvfp4.env
endef

define OKSA_SERVICES_INSTALL_INIT_SYSTEMD
	for unit in puu-k3s-addons.service puu-vllm-discovery.service \
		puu-vllm-discovery.timer puu-model-serving-labeler.service \
		puu-model-serving-labeler.timer puu-nvlink-topology.service \
		puu-nvlink-topology.timer puu-vllm-planner.service \
		puu-vllm-planner.timer puu-vllm-preseed.service; do \
		$(INSTALL) -D -m 0644 $(OKSA_SERVICES_FILES)/systemd/$$unit \
			$(TARGET_DIR)/usr/lib/systemd/system/$$unit || exit $$?; \
	done
endef

$(eval $(generic-package))
