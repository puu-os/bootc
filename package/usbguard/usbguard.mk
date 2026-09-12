# D-Bus activation must not depend on the alias created by enabling the unit.
define USBGUARD_FIX_SYSTEMD
	$(SED) 's|SystemdService=dbus-org.usbguard.service|SystemdService=usbguard-dbus.service|' \
		$(TARGET_DIR)/usr/share/dbus-1/system-services/org.usbguard1.service
	$(SED) 's/^PresentDevicePolicy=.*/PresentDevicePolicy=keep/' \
		$(TARGET_DIR)/etc/usbguard/usbguard-daemon.conf
	$(INSTALL) -D -m 0644 \
		$(BR2_EXTERNAL_PUU_PATH)/package/usbguard/usbguard-dbus.service.d/wait-for-daemon.conf \
		$(TARGET_DIR)/etc/systemd/system/usbguard-dbus.service.d/wait-for-daemon.conf
endef
USBGUARD_POST_INSTALL_TARGET_HOOKS += USBGUARD_FIX_SYSTEMD
