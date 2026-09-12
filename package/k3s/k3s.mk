K3S_VERSION = v1.36.3+k3s1
K3S_SITE = $(call github,k3s-io,k3s,$(K3S_VERSION))
K3S_LICENSE = Apache-2.0
K3S_LICENSE_FILES = LICENSE
K3S_CPE_ID_VENDOR = k3s

K3S_GOMOD = github.com/k3s-io/k3s

K3S_BUILD_TARGETS = cmd/server
K3S_BIN_NAME = k3s
K3S_UPSTREAM_GOLANG = go1.26.2
K3S_HELM_JOB_IMAGE = rancher/klipper-helm@sha256:5425a2b4613d2458dbdff99218e8d487ebb59c20e7a941f061c0777070f0aac1

K3S_LDFLAGS = \
	-X $(K3S_GOMOD)/pkg/version.Version=$(K3S_VERSION) \
	-X $(K3S_GOMOD)/pkg/version.GitCommit=buildroot \
	-X $(K3S_GOMOD)/pkg/version.UpstreamGolang=$(K3S_UPSTREAM_GOLANG) \
	-X github.com/k3s-io/helm-controller/pkg/controllers/chart.DefaultJobImage=$(K3S_HELM_JOB_IMAGE) \
	-w -s

K3S_TAGS = no_aufs providerless urfave_cli_no_docs sqlite_omit_load_extension

K3S_GO_ENV = \
	CGO_CFLAGS="$(TARGET_CFLAGS) -DSQLITE_ENABLE_DBSTAT_VTAB=1 -DSQLITE_USE_ALLOCA=1" \
	GOPROXY=https://proxy.golang.org

K3S_DEPENDENCIES = \
	cni-plugins \
	conntrack-tools \
	containerd \
	flannel-cni-plugin \
	host-pkgconf \
	iptables \
	kmod \
	libseccomp \
	util-linux

# Embed core manifests required for local cluster.
define K3S_PREPARE_EMBEDDED_ASSETS
	rm -rf $(@D)/pkg/deploy/embed $(@D)/pkg/static/embed
	mkdir -p $(@D)/pkg/deploy/embed $(@D)/pkg/static/embed
	for manifest in ccm.yaml coredns.yaml local-storage.yaml rolebindings.yaml runtimes.yaml; do \
		cp -dpfr $(@D)/manifests/$$manifest $(@D)/pkg/deploy/embed/; \
	done
	if [ -d $(@D)/build/static ]; then \
		cp -dpfr $(@D)/build/static/. $(@D)/pkg/static/embed/; \
	else \
		touch $(@D)/pkg/static/embed/.empty; \
	fi
endef

K3S_PRE_BUILD_HOOKS += K3S_PREPARE_EMBEDDED_ASSETS

define K3S_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/bin/k3s \
		$(TARGET_DIR)/usr/bin/k3s
	$(INSTALL) -D -m 0755 $(K3S_PKGDIR)/files/configure-k3s-cluster \
		$(TARGET_DIR)/usr/libexec/puu-os/configure-k3s-cluster
	$(INSTALL) -D -m 0755 $(K3S_PKGDIR)/files/wait-default-route \
		$(TARGET_DIR)/usr/libexec/puu-os/wait-default-route
	for link in kubectl crictl k3s-agent k3s-server k3s-token \
		k3s-etcd-snapshot k3s-secrets-encrypt k3s-certificate k3s-completion; do \
		ln -sf k3s $(TARGET_DIR)/usr/bin/$$link; \
	done
endef

define K3S_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(K3S_PKGDIR)/files/k3s.service \
		$(TARGET_DIR)/usr/lib/systemd/system/k3s.service
	$(INSTALL) -D -m 0644 $(K3S_PKGDIR)/files/k3s.service.env \
		$(TARGET_DIR)/usr/lib/systemd/system/k3s.service.env
	$(INSTALL) -D -m 0644 $(K3S_PKGDIR)/files/k3s.conf \
		$(TARGET_DIR)/usr/lib/tmpfiles.d/k3s.conf
endef

$(eval $(golang-package))
