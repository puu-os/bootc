OKSA_VERSION = 1.0
OKSA_GATEWAY_API_VERSION = 1.6.1
OKSA_AGENTGATEWAY_VERSION = 1.2.0
OKSA_SOURCE = experimental-install.yaml
OKSA_SITE = https://github.com/kubernetes-sigs/gateway-api/releases/download/v$(OKSA_GATEWAY_API_VERSION)
OKSA_AGENTGATEWAY_SITE = https://raw.githubusercontent.com/agentgateway/agentgateway/v$(OKSA_AGENTGATEWAY_VERSION)/controller/install/helm/agentgateway-crds/templates
OKSA_AGENTGATEWAY_CRDS = \
	agentgateway.dev_agentgatewaybackends.yaml \
	agentgateway.dev_agentgatewayparameters.yaml \
	agentgateway.dev_agentgatewaypolicies.yaml
OKSA_EXTRA_DOWNLOADS = $(addprefix $(OKSA_AGENTGATEWAY_SITE)/,$(OKSA_AGENTGATEWAY_CRDS))
OKSA_LICENSE = GPL-2.0+ (native files), Apache-2.0 (CRDs)
OKSA_LICENSE_FILES = LICENSE LICENSE.Apache-2.0
OKSA_SETUP_TYPE = hatch
OKSA_SUBDIR = native
OKSA_DEPENDENCIES = dbus k3s python-gobject python-nicegui xdg-dbus-proxy

define OKSA_USERS
	oksa -1 oksa -1 * /var/empty /sbin/nologin - OKSA dashboard
endef

OKSA_FILES = $(OKSA_PKGDIR)/files

define OKSA_EXTRACT_CMDS
	$(INSTALL) -D -m 0644 $(OKSA_FILES)/LICENSE $(@D)/LICENSE
	$(INSTALL) -D -m 0644 $(OKSA_PKGDIR)/LICENSE.Apache-2.0 $(@D)/LICENSE.Apache-2.0
endef

define OKSA_STAGE_NATIVE_SOURCES
	mkdir -p $(@D)/native
	rsync -a --delete --delete-excluded $(RSYNC_VCS_EXCLUSIONS) \
		--exclude='__pycache__' --exclude='/dist' $(OKSA_FILES)/ $(@D)/native/
endef
OKSA_PRE_CONFIGURE_HOOKS += OKSA_STAGE_NATIVE_SOURCES

define OKSA_INSTALL_ASSETS
	$(INSTALL) -D -m 0644 $(OKSA_DL_DIR)/experimental-install.yaml \
		$(TARGET_DIR)/usr/share/puu/k3s/crds/10-gateway-api-experimental-install.yaml
	set -e; for crd in $(OKSA_AGENTGATEWAY_CRDS); do \
		$(INSTALL) -D -m 0644 $(OKSA_DL_DIR)/$$crd \
			$(TARGET_DIR)/usr/share/puu/k3s/crds/12-$$crd; \
	done
	chmod 0644 $(TARGET_DIR)/usr/libexec/puu/oksa_*.py
	chmod 0755 $(TARGET_DIR)/usr/libexec/puu/oksa-init-litellm-db
	$(INSTALL) -D -m 0644 $(OKSA_FILES)/dbus/io.github.puu_os.Oksa.conf \
		$(TARGET_DIR)/usr/share/dbus-1/system.d/io.github.puu_os.Oksa.conf
	$(INSTALL) -D -m 0644 $(OKSA_FILES)/dbus/io.github.puu_os.Oksa.service \
		$(TARGET_DIR)/usr/share/dbus-1/system-services/io.github.puu_os.Oksa.service
	$(INSTALL) -D -m 0644 $(OKSA_FILES)/oksa.conf \
		$(TARGET_DIR)/usr/lib/tmpfiles.d/oksa-services.conf
	$(foreach manifest,20-agentgateway.yaml 21-puu-gateway.yaml \
		30-nvidia-cdi-runtime.yaml 32-nvidia-device-plugin.yaml \
		40-litellm.yaml 45-open-webui.yaml 46-oksa.yaml,\
		$(INSTALL) -D -m 0644 $(OKSA_FILES)/manifests/$(manifest) \
			$(TARGET_DIR)/usr/share/puu/k3s/manifests/$(manifest)$(sep))
	$(INSTALL) -d -m 0755 $(TARGET_DIR)/usr/share/puu/vllm/catalog.d
	$(foreach catalog,$(notdir $(wildcard $(OKSA_FILES)/vllm/catalog.d/*.env)),\
		$(INSTALL) -D -m 0644 $(OKSA_FILES)/vllm/catalog.d/$(catalog) \
			$(TARGET_DIR)/usr/share/puu/vllm/catalog.d/$(catalog)$(sep))
	$(foreach runtime,$(notdir $(wildcard $(OKSA_FILES)/vllm/runtime/*)),\
		$(INSTALL) -D -m 0644 $(OKSA_FILES)/vllm/runtime/$(runtime) \
			$(TARGET_DIR)/usr/share/puu/vllm/runtime/$(runtime)$(sep))
endef
OKSA_POST_INSTALL_TARGET_HOOKS += OKSA_INSTALL_ASSETS

define OKSA_INSTALL_INIT_SYSTEMD
	for unit in puu-k3s-addons.service puu-model-serving-labeler.service \
		puu-model-serving-labeler.timer puu-vllm-planner.service \
		puu-vllm-planner.timer puu-vllm-preseed.service \
		puu-postgresql.service \
		puu-oksa.service puu-oksa-proxy.service; do \
		$(INSTALL) -D -m 0644 $(OKSA_FILES)/systemd/$$unit \
			$(TARGET_DIR)/usr/lib/systemd/system/$$unit || exit $$?; \
	done; \
	$(INSTALL) -D -m 0644 $(OKSA_FILES)/systemd/postgresql.service.d/puu.conf \
		$(TARGET_DIR)/usr/lib/systemd/system/postgresql.service.d/puu.conf || exit $$?;
endef

define OKSA_SAVE_LOCAL_SOURCES
	mkdir -p $(OKSA_REDIST_SOURCES_DIR)/local-src
	rsync -a --delete $(RSYNC_VCS_EXCLUSIONS) --exclude='__pycache__' \
		--exclude='/files/dist' $(OKSA_PKGDIR)/ $(OKSA_REDIST_SOURCES_DIR)/local-src/
endef
OKSA_POST_LEGAL_INFO_HOOKS += OKSA_SAVE_LOCAL_SOURCES

$(eval $(python-package))

$(OKSA_TARGET_SOURCE): $(OKSA_PKGDIR)/oksa.mk $(OKSA_PKGDIR)/oksa.hash
$(OKSA_TARGET_EXTRACT): $(OKSA_FILES)/LICENSE $(OKSA_PKGDIR)/LICENSE.Apache-2.0

# Refresh the dependency snapshot so it cannot overwrite newer K3s support files.
$(OKSA_TARGET_CONFIGURE): $(K3S_TARGET_INSTALL_TARGET)

# Reinstall when the support files change so the image gets the current copies and
# the file accounting from the configure/install steps stays consistent.
$(OKSA_TARGET_CONFIGURE): $(wildcard $(OKSA_FILES)/* \
	$(OKSA_FILES)/*/* $(OKSA_FILES)/*/*/*) $(OKSA_PKGDIR)/oksa.mk
