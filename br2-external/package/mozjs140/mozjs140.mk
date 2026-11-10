MOZJS140_VERSION = 140.12.0
MOZJS140_SITE = https://archive.mozilla.org/pub/firefox/releases/$(MOZJS140_VERSION)esr/source
MOZJS140_SOURCE = firefox-$(MOZJS140_VERSION)esr.source.tar.xz
MOZJS140_LICENSE = MPL-2.0
MOZJS140_LICENSE_FILES = LICENSE
MOZJS140_DEPENDENCIES = host-cbindgen host-llvm host-pkgconf host-python3 host-rustc python3 zlib icu
MOZJS140_INSTALL_STAGING = YES

MOZJS140_BUILDDIR = $(@D)/build

MOZJS140_CONF_ENV = \
	CC="$(TARGET_CC)" \
	CXX="$(TARGET_CXX)" \
	AR="$(TARGET_AR)" \
	RANLIB="$(TARGET_RANLIB)" \
	HOST_CC="$(HOSTCC)" \
	HOST_CXX="$(HOSTCXX)" \
	CBINDGEN="$(HOST_DIR)/bin/cbindgen" \
	CARGO="$(HOST_DIR)/bin/cargo" \
	RUSTC="$(HOST_DIR)/bin/rustc" \
	CARGO_HOME="$(BR_CARGO_HOME)" \
	LLVM_OBJDUMP="$(HOST_DIR)/bin/llvm-objdump"

define MOZJS140_CONFIGURE_CMDS
	mkdir -p $(MOZJS140_BUILDDIR)
	cd $(MOZJS140_BUILDDIR) && \
	$(TARGET_MAKE_ENV) \
	$(MOZJS140_CONF_ENV) \
	$(HOST_DIR)/bin/python3 $(MOZJS140_SRCDIR)/configure.py \
		--enable-project=js \
		--prefix=/usr \
		--target=$(GNU_TARGET_NAME) \
		--host=$(GNU_HOST_NAME) \
		--disable-jemalloc \
		--enable-optimize \
		--disable-debug \
		--with-intl-api \
		--with-system-icu \
		--with-system-zlib \
		--without-system-nspr \
		--disable-tests
endef

define MOZJS140_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MOZJS140_CONF_ENV) \
		$(MAKE) -C $(MOZJS140_BUILDDIR)
endef

define MOZJS140_INSTALL_STAGING_CMDS
	$(TARGET_MAKE_ENV) $(MOZJS140_CONF_ENV) \
		$(MAKE) -C $(MOZJS140_BUILDDIR) DESTDIR=$(STAGING_DIR) install
endef

define MOZJS140_PC_ADD_XP_UNIX
	$(SED) 's|^Cflags: |Cflags: -DXP_UNIX |' \
		$(STAGING_DIR)/usr/lib/pkgconfig/mozjs-140.pc
endef
MOZJS140_POST_INSTALL_STAGING_HOOKS += MOZJS140_PC_ADD_XP_UNIX

define MOZJS140_INSTALL_TARGET_CMDS
	$(TARGET_MAKE_ENV) $(MOZJS140_CONF_ENV) \
		$(MAKE) -C $(MOZJS140_BUILDDIR) DESTDIR=$(TARGET_DIR) install
endef

$(eval $(generic-package))
