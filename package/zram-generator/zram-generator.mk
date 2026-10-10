ZRAM_GENERATOR_VERSION = 1.2.1
ZRAM_GENERATOR_SOURCE = zram-generator-$(ZRAM_GENERATOR_VERSION).crate
ZRAM_GENERATOR_SITE = https://static.crates.io/crates/zram-generator
ZRAM_GENERATOR_LICENSE = MIT
ZRAM_GENERATOR_LICENSE_FILES = LICENSE
ZRAM_GENERATOR_DEPENDENCIES = systemd util-linux
ZRAM_GENERATOR_CARGO_ENV = SYSTEMD_UTIL_DIR=/usr/lib/systemd

define ZRAM_GENERATOR_EXTRACT_CMDS
	$(TAR) --strip-components=1 \
		-C $(@D) -xzf $(ZRAM_GENERATOR_DL_DIR)/$(ZRAM_GENERATOR_SOURCE)
endef

define ZRAM_GENERATOR_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 \
		$(@D)/target/$(RUSTC_TARGET_NAME)/$(if $(BR2_ENABLE_DEBUG),debug,release)/zram-generator \
		$(TARGET_DIR)/usr/lib/systemd/zram-generator
	mkdir -p $(TARGET_DIR)/usr/lib/systemd/system-generators
	ln -sf ../zram-generator \
		$(TARGET_DIR)/usr/lib/systemd/system-generators/zram-generator
	$(INSTALL) -D -m 0644 \
		$(@D)/units/systemd-zram-setup@.service.in \
		$(TARGET_DIR)/usr/lib/systemd/system/systemd-zram-setup@.service
	$(SED) 's,@SYSTEMD_SYSTEM_GENERATOR_DIR@,/usr/lib/systemd/system-generators,g' \
		$(TARGET_DIR)/usr/lib/systemd/system/systemd-zram-setup@.service
endef

$(eval $(cargo-package))
