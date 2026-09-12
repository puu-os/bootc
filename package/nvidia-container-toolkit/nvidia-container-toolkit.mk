NVIDIA_CONTAINER_TOOLKIT_VERSION = v1.20.0
NVIDIA_CONTAINER_TOOLKIT_SITE = \
	$(call github,NVIDIA,nvidia-container-toolkit,$(NVIDIA_CONTAINER_TOOLKIT_VERSION))
NVIDIA_CONTAINER_TOOLKIT_LICENSE = Apache-2.0
NVIDIA_CONTAINER_TOOLKIT_LICENSE_FILES = LICENSE
NVIDIA_CONTAINER_TOOLKIT_CPE_ID_VENDOR = nvidia
NVIDIA_CONTAINER_TOOLKIT_CPE_ID_PRODUCT = container_toolkit

# Explicitly link -lnvidia-ml and -lnvidia-sandboxutils for full RELRO.
NVIDIA_CONTAINER_TOOLKIT_DEPENDENCIES = nvidia-driver
NVIDIA_CONTAINER_TOOLKIT_GO_ENV = \
	CGO_LDFLAGS="$(TARGET_LDFLAGS) -lnvidia-ml -lnvidia-sandboxutils"
NVIDIA_CONTAINER_TOOLKIT_GOMOD = github.com/NVIDIA/nvidia-container-toolkit
NVIDIA_CONTAINER_TOOLKIT_CLI_VERSION_PACKAGE = \
	$(NVIDIA_CONTAINER_TOOLKIT_GOMOD)/internal/info

NVIDIA_CONTAINER_TOOLKIT_BUILD_TARGETS = \
	cmd/nvidia-ctk \
	cmd/nvidia-container-runtime \
	cmd/nvidia-container-runtime.cdi \
	cmd/nvidia-container-runtime.legacy \
	cmd/nvidia-container-runtime-hook \
	cmd/nvidia-cdi-hook

NVIDIA_CONTAINER_TOOLKIT_LDFLAGS = \
	-X $(NVIDIA_CONTAINER_TOOLKIT_CLI_VERSION_PACKAGE).version=$(patsubst v%,%,$(NVIDIA_CONTAINER_TOOLKIT_VERSION)) \
	-X $(NVIDIA_CONTAINER_TOOLKIT_CLI_VERSION_PACKAGE).gitCommit=buildroot \
	-w -s

define NVIDIA_CONTAINER_TOOLKIT_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 \
		$(NVIDIA_CONTAINER_TOOLKIT_PKGDIR)/files/nvidia-cdi-refresh.service \
		$(TARGET_DIR)/usr/lib/systemd/system/nvidia-cdi-refresh.service
	$(INSTALL) -D -m 0644 \
		$(NVIDIA_CONTAINER_TOOLKIT_PKGDIR)/files/nvidia-cdi-refresh.path \
		$(TARGET_DIR)/usr/lib/systemd/system/nvidia-cdi-refresh.path
	$(INSTALL) -D -m 0644 \
		$(NVIDIA_CONTAINER_TOOLKIT_PKGDIR)/files/nvidia-cdi-refresh.env \
		$(TARGET_DIR)/etc/nvidia-container-toolkit/nvidia-cdi-refresh.env
	$(INSTALL) -D -m 0755 \
		$(NVIDIA_CONTAINER_TOOLKIT_PKGDIR)/files/has-nvidia-hardware \
		$(TARGET_DIR)/usr/libexec/puu-os/has-nvidia-hardware
endef

$(eval $(golang-package))
