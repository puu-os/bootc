BOOTC_VERSION = 1.16.10
BOOTC_SITE = $(call github,bootc-dev,bootc,v$(BOOTC_VERSION))
BOOTC_LICENSE = Apache-2.0 OR MIT
BOOTC_LICENSE_FILES = LICENSE-APACHE LICENSE-MIT

BOOTC_DEPENDENCIES = \
	host-dracut \
	host-pkgconf \
	composefs \
	openssl \
	libostree \
	zstd

# Skip manpages and completions to avoid host cargo invocations during install.
define BOOTC_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 \
		$(@D)/target/$(RUSTC_TARGET_NAME)/release/bootc \
		$(TARGET_DIR)/usr/bin/bootc
	$(INSTALL) -D -m 0755 \
		$(@D)/target/$(RUSTC_TARGET_NAME)/release/system-reinstall-bootc \
		$(TARGET_DIR)/usr/bin/system-reinstall-bootc
	$(INSTALL) -D -m 0755 \
		$(@D)/target/$(RUSTC_TARGET_NAME)/release/bootc-initramfs-setup \
		$(TARGET_DIR)/usr/lib/bootc/initramfs-setup
	$(INSTALL) -D -m 0755 \
		$(@D)/crates/initramfs/dracut/module-setup.sh \
		$(TARGET_DIR)/usr/lib/dracut/modules.d/51bootc/module-setup.sh
	$(INSTALL) -D -m 0644 -t \
		$(TARGET_DIR)/usr/lib/systemd/system \
		$(@D)/crates/initramfs/bootc-root-setup.service
	$(INSTALL) -D -m 0755 \
		$(@D)/crates/cli/bootc-generator-stub \
		$(TARGET_DIR)/usr/lib/systemd/system-generators/bootc-systemd-generator
	mkdir -p $(TARGET_DIR)/usr/lib/bootc/bound-images.d
	mkdir -p $(TARGET_DIR)/usr/lib/bootc/kargs.d
	mkdir -p $(TARGET_DIR)/usr/lib/bootc/install
	ln -sf ../../../sysroot/ostree/bootc/storage \
		$(TARGET_DIR)/usr/lib/bootc/storage
	$(INSTALL) -D -m 0644 -t $(TARGET_DIR)/usr/lib/systemd/system \
		$(wildcard $(@D)/systemd/*.service) \
		$(wildcard $(@D)/systemd/*.timer) \
		$(wildcard $(@D)/systemd/*.path) \
		$(wildcard $(@D)/systemd/*.target)
	$(INSTALL) -D -m 0644 \
		$(@D)/baseimage/base/usr/lib/ostree/prepare-root.conf \
		$(TARGET_DIR)/usr/share/doc/bootc/baseimage/base/usr/lib/ostree/prepare-root.conf
	mkdir -p $(TARGET_DIR)/usr/share/doc/bootc/baseimage/base/sysroot
	cp -PfT $(@D)/baseimage/base/ostree \
		$(TARGET_DIR)/usr/share/doc/bootc/baseimage/base/ostree
	rm -rf $(TARGET_DIR)/usr/share/doc/bootc/baseimage/dracut \
		$(TARGET_DIR)/usr/share/doc/bootc/baseimage/systemd
	cp -Prf $(@D)/baseimage/dracut \
		$(TARGET_DIR)/usr/share/doc/bootc/baseimage/dracut
	cp -Prf $(@D)/baseimage/systemd \
		$(TARGET_DIR)/usr/share/doc/bootc/baseimage/systemd
endef

$(eval $(cargo-package))
