RTKIT_VERSION = 0.13
RTKIT_SITE = $(call github,heftig,rtkit,v$(RTKIT_VERSION))
RTKIT_LICENSE = GPL-3.0+
RTKIT_LICENSE_FILES = LICENSE
RTKIT_DEPENDENCIES = \
	host-pkgconf \
	dbus \
	libcap \
	polkit \
	systemd

RTKIT_CONF_OPTS = \
	-Dinstalled_tests=false \
	-Dlibsystemd=enabled \
	-Ddbus_systemservicedir=/usr/share/dbus-1/system-services \
	-Ddbus_interfacedir=/usr/share/dbus-1/interfaces \
	-Ddbus_rulesdir=/usr/share/dbus-1/system.d \
	-Dpolkit_actiondir=/usr/share/polkit-1/actions \
	-Dlibexecdir=/usr/libexec \
	-Dsbindir=/usr/sbin

define RTKIT_INSTALL_SYSUSERS
	$(INSTALL) -d -m 0755 $(TARGET_DIR)/usr/lib/sysusers.d
	printf '%s\n' 'u rtkit 133 "RealtimeKit" /proc' \
		> $(TARGET_DIR)/usr/lib/sysusers.d/rtkit.conf
endef

RTKIT_POST_INSTALL_TARGET_HOOKS += RTKIT_INSTALL_SYSUSERS

define RTKIT_INSTALL_SERVICE_LIMITS
	$(INSTALL) -d -m 0755 $(TARGET_DIR)/usr/lib/systemd/system/rtkit-daemon.service.d
	printf '%s\n' \
		'[Service]' \
		'LimitNICE=-15' \
		'LimitRTPRIO=20' \
		'LimitRTTIME=200000' \
		'AmbientCapabilities=CAP_SYS_NICE' \
		> $(TARGET_DIR)/usr/lib/systemd/system/rtkit-daemon.service.d/10-puu-limits.conf
endef

RTKIT_POST_INSTALL_TARGET_HOOKS += RTKIT_INSTALL_SERVICE_LIMITS

$(eval $(meson-package))
