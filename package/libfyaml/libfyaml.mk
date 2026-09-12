LIBFYAML_VERSION = 0.9.6
LIBFYAML_SITE = $(call github,pantoniou,libfyaml,v$(LIBFYAML_VERSION))
LIBFYAML_LICENSE = MIT
LIBFYAML_LICENSE_FILES = LICENSE
LIBFYAML_INSTALL_STAGING = YES

# GitHub archive requires autoreconf.
LIBFYAML_AUTORECONF = YES

$(eval $(autotools-package))
$(eval $(host-autotools-package))
