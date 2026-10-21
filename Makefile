include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-xiaomusic-mini
PKG_VERSION:=1.0.6
PKG_RELEASE:=1
PKG_LICENSE:=MIT
PKG_MAINTAINER:=

LUCI_TITLE:=XiaoMiMusic Mini - 小爱音箱点播本地音乐（LuCI + 内置后端）
LUCI_DESCRIPTION:=LuCI 控制面板 + 内置 xiaomusic-mini Python 后端：服务启停/开机自启、播放控制、\
  音量、歌单与音箱切换、配置修改、依赖自检与一键安装、运行日志。\
  后端程序安装到 /usr/lib/xiaomusic-mini/，配置与 token 保存在 /opt/xiaomusic-mini/。
# 只依赖 LuCI 基础框架。Python 运行时不写进 DEPENDS：
# OpenWrt 的 .packagedeps 会把运行时依赖串进编译图，导致 make 时从源码编译整个 CPython
#（python3-light/asyncio/logging/... 共用一个源码包，编译很慢）。
# 装好 ipk 后在 LuCI「环境检测 → 一键安装」里由设备用 opkg/pip 安装即可（无需编译）。
LUCI_DEPENDS:=+luci-base
# 若固件必须离线自带 Python，取消下面一行注释（会触发 Python 源码编译）：
#LUCI_DEPENDS+=+python3-light +python3-asyncio +python3-logging +python3-email +python3-urllib +python3-openssl
LUCI_PKGARCH:=all

# 安装后：修正可执行权限（拷贝源码树时可能丢失 755）并初始化数据目录/自启。
# 在 include luci.mk 前定义，取代其默认 postinst，因此需自行刷新 LuCI 缓存。
define Package/$(PKG_NAME)/postinst
#!/bin/sh
if [ -z "$${IPKG_INSTROOT}" ]; then
	chmod 755 /etc/init.d/xiaomusic-mini 2>/dev/null
	chmod 755 /usr/libexec/xiaomusic-mini-check 2>/dev/null
	chmod 755 /usr/libexec/xiaomusic-mini-install 2>/dev/null
	chmod 755 /usr/libexec/xiaomusic-mini-log 2>/dev/null
	chmod 755 /usr/libexec/xiaomusic-mini-qrlogin 2>/dev/null
	chmod 755 /usr/lib/xiaomusic-mini/main.py 2>/dev/null
	[ -x /etc/uci-defaults/90-luci-app-xiaomusic-mini ] && \
		/etc/uci-defaults/90-luci-app-xiaomusic-mini
	[ -x /etc/init.d/xiaomusic-mini ] && \
		/etc/init.d/xiaomusic-mini restart 2>/dev/null
	rm -f /tmp/luci-indexcache.*
	rm -rf /tmp/luci-modulecache/
	killall -HUP rpcd 2>/dev/null
fi
exit 0
endef

include $(TOPDIR)/feeds/luci/luci.mk

# call BuildPackage - OpenWrt buildroot signature
