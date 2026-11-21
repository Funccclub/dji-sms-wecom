include $(TOPDIR)/rules.mk

PKG_NAME:=dji-sms-wecom
PKG_VERSION:=0.1.0
PKG_RELEASE:=1
PKG_LICENSE:=MIT
include $(INCLUDE_DIR)/package.mk

define Package/dji-sms-wecom
  SECTION:=utils
  CATEGORY:=Utilities
  TITLE:=DJI SMS to WeCom forwarding service
  DEPENDS:=+python3-light +python3-logging +python3-sqlite3 +python3-urllib +python3-openssl +ca-bundle
  PKGARCH:=all
endef

define Package/dji-sms-wecom/conffiles
/etc/dji-sms-wecom.json
endef

define Build/Compile
endef

define Package/dji-sms-wecom/install
	$(INSTALL_DIR) $(1)/usr/sbin $(1)/etc/init.d $(1)/etc
	$(INSTALL_BIN) ./files/dji-sms-wecom.py $(1)/usr/sbin/dji-sms-wecom
	$(INSTALL_BIN) ./files/init $(1)/etc/init.d/dji-sms-wecom
	$(INSTALL_CONF) ./files/config.json $(1)/etc/dji-sms-wecom.json
	chmod 600 $(1)/etc/dji-sms-wecom.json
endef

$(eval $(call BuildPackage,dji-sms-wecom))
