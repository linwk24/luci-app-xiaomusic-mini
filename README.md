<img width="827" height="886" alt="Screenshot (2)" src="https://github.com/user-attachments/assets/e16dd255-670a-4616-a28e-4b26c672d32d" />
<img width="1590" height="808" alt="Screenshot (1)" src="https://github.com/user-attachments/assets/d86b1104-c042-496b-a2c8-30a1b89edce3" />

# luci-app-xiaomusic-mini

小爱音箱「语音点播本地音乐」的 **LuCI 应用 + 内置 Python 后端** 一体化软件包。

之前后端（`xiaomusic-mini/`）和 LuCI 界面（`luci-app-xiaomusic-mini/`）是两套文件、分开安装；
现在后端已并入本软件包，装一个 ipk 即可同时得到：

- LuCI 页面：`服务 → 小爱音乐 Mini`（状态、播放、音量、歌单、音箱切换、配置、依赖自检/安装、日志）
- Python 后端：`/usr/lib/xiaomusic-mini/main.py`（+ `plugins/`）
- procd 服务：`/etc/init.d/xiaomusic-mini`（开机自启，未填账号密码时自动跳过启动）

## 下载 / 安装预编译包

不想自己编译的话，直接到 **[Releases](https://github.com/linwk24/luci-app-xiaomusic-mini/releases)** 下载最新的
`luci-app-xiaomusic-mini_<版本>_all.ipk`，然后：

```sh
opkg install luci-app-xiaomusic-mini_1.0.1-r1_all.ipk
# 或在 LuCI → 系统 → 软件包 → 上传安装
```

- `Architecture: all`，硬依赖只有 `luci-base`；
- Python 运行时/第三方库不随包编译，装好后到「环境检测」一键安装（opkg/pip）；
- 升级保留 `/opt/xiaomusic-mini/config.json`、`mi.token` 与日志，`postinst` 会自动重启服务。

## 目录 ↔ 安装路径

| 源码路径 | 安装到设备 | 说明 |
|---|---|---|
| `htdocs/luci-static/resources/view/xiaomusic-mini/overview.js` | `/www/luci-static/resources/view/xiaomusic-mini/overview.js` | LuCI 页面（`luci.mk` 自动安装 `htdocs/`） |
| `root/usr/lib/xiaomusic-mini/main.py` | `/usr/lib/xiaomusic-mini/main.py` | 后端主程序（纯 Python 单文件） |
| `root/usr/lib/xiaomusic-mini/plugins/*.py` | `/usr/lib/xiaomusic-mini/plugins/` | 自带插件（`code1`、`httpget`），供 `exec#` 自定义口令调用 |
| `root/etc/init.d/xiaomusic-mini` | `/etc/init.d/xiaomusic-mini` | procd 服务脚本 |
| `root/etc/uci-defaults/90-luci-app-xiaomusic-mini` | `/etc/uci-defaults/` | 首次安装：建数据目录、生成默认配置、修权限、开机自启 |
| `root/usr/libexec/xiaomusic-mini-check` | `/usr/libexec/xiaomusic-mini-check` | 环境检测，输出 JSON |
| `root/usr/libexec/xiaomusic-mini-install` | `/usr/libexec/xiaomusic-mini-install` | 依赖安装器（白名单 + 后台执行 + 状态轮询） |
| `root/usr/libexec/xiaomusic-mini-log` | `/usr/libexec/xiaomusic-mini-log` | 输出运行日志尾部 |
| `root/usr/share/xiaomusic-mini/config.json` | `/usr/share/xiaomusic-mini/config.json` | 默认配置模板（**不含任何账号信息**） |
| `root/usr/share/luci/menu.d/...` | `/usr/share/luci/menu.d/` | 菜单项 |
| `root/usr/share/rpcd/acl.d/...` | `/usr/share/rpcd/acl.d/` | ACL（仅放行配置读写与三个 helper） |

运行期数据（不随包覆盖，升级保留）：

| 路径 | 内容 | 权限 |
|---|---|---|
| `/opt/xiaomusic-mini/config.json` | 主配置（账号、密码、音乐目录、端口等） | `600` |
| `/opt/xiaomusic-mini/mi.token` | 小米登录 token（首次登录后生成） | `600` |
| `/opt/xiaomusic-mini/xiaomusic-mini.log` | 运行日志 | `600` |
| `/opt/xiaomusic-mini/plugins/` | 可选：用户自定义插件（优先于自带插件目录加载） | — |

> 说明：数据目录沿用独立版的 `/opt/xiaomusic-mini`，方便从旧安装平滑升级（配置、token 直接复用）。
> 若 `/opt` 挂载在 U 盘，请确保开机时已挂载，否则服务会因找不到配置而跳过启动。

## 构建

把整个 `luci-app-xiaomusic-mini/` 放到 LuCI feed 的应用目录（或任意能被 `feeds/luci/luci.mk` include 到的位置）：

```sh
# 方式一：放进 luci feed（推荐，随固件/ipk 构建）
cp -r luci-app-xiaomusic-mini ~/openwrt/feeds/luci/applications/
# 或者直接从本仓库 clone 到该位置：
# git clone https://github.com/linwk24/luci-app-xiaomusic-mini.git \
#   ~/openwrt/feeds/luci/applications/luci-app-xiaomusic-mini
cd ~/openwrt
./scripts/feeds update -a && ./scripts/feeds install -a
make menuconfig     # LuCI → Applications → luci-app-xiaomusic-mini
make package/luci-app-xiaomusic-mini/compile V=s
# 产物: bin/packages/<arch>/luci/luci-app-xiaomusic-mini_1.0.1-1_all.ipk
```

- 页面文件必须放在 `htdocs/luci-static/...`（`luci.mk` 只安装 `htdocs/`、`root/`、`luasrc/`、`ucode/`、`src/`；放在旧的 `luci-static/` 目录**不会**被打包）。
- 后端是纯 Python 文件，无需编译；`root/` 下的文件由 `luci.mk` 原样安装。
- **默认不编译 Python**：`LUCI_DEPENDS` 只有 `luci-base`，ipk 是纯文件（秒级编译）。
- 如果之前已在 menuconfig 里选中过 `python3-*`，更新 Makefile 后执行一次 `make menuconfig`
  （或 `make defconfig`）让它们从 `.config` 中移除；只想编 ipk 的话，直接
  `make package/luci-app-xiaomusic-mini/compile` 不会再去编译 Python。
- `postinst` 会修正可执行权限、初始化数据目录并刷新 LuCI 缓存。

依赖策略：**运行时安装，不写死进 DEPENDS**。
OpenWrt 的 `.packagedeps` 会把运行时依赖串进编译图——只要 `DEPENDS` 里有 `python3-light`，
`make` 就会从源码编译整个 CPython（`python3-light/asyncio/logging/...` 共用一个源码包，非常慢）。
所以默认只依赖 `luci-base`（任何 LuCI 环境都有，且自带 `cgi-io`），
装好 ipk 后由页面「环境检测 → 一键安装」在设备上用 opkg/pip 安装：

```
python3（或 python3-asyncio python3-logging python3-email python3-urllib python3-openssl）
python3-pip、python3-aiohttp（若 feed 有）或 pip：mutagen、miservice-fork、fake-useragent
```

如果设备完全离线、需要把 Python 打进固件，把 Makefile 里那行注释掉的
`LUCI_DEPENDS+=+python3-light ...` 打开即可（代价就是编译 CPython）。

## 首次使用

1. 安装 ipk 后刷新 LuCI，进入 **服务 → 小爱音乐 Mini**。
2. 「环境检测」默认折叠（有未通过项会自动展开）；点开可查看缺失的 Python 依赖，
   点「一键安装」（后台执行，完成后自动复检），或点「查看安装命令」复制到 SSH 执行。
3. 在「配置」中填写小米账号/密码、音乐目录（如 `/mnt/sda1/music`），确认端口，点「保存并重启」。
4. 回到「播放控制」确认设备、歌单、音量正常；对小爱说「播放歌曲晴天」验证语音点播。

> 服务在未填写账号密码时不会启动（`init.d` 会输出 `未配置小米账号/密码，跳过启动`），
> 填好并保存后自动重启生效。

> **小米账号二次验证**：部分账号在网页/新设备登录时会要求手机确认或验证码，
> 此时代码登录会失败（日志里的 `captchaUrl` / `KeyError: 'userId'`）。
> 请从一台能正常登录小爱/米家的机器上取得 `mi.token`，放到
> `/opt/xiaomusic-mini/mi.token`（`600`）后重启服务；token 有效时不需要密码。
> `token_path` 可在配置里改。

## 语音口令

| 口令 | 功能 |
|---|---|
| 播放歌曲/放歌曲 + 歌名 | 本地模糊匹配播放 |
| 播放本地歌曲/本地播放歌曲 + 歌名 | 同上（明确本地） |
| 播放歌单/播放列表 + 目录名 | 播放指定目录（子目录=歌单） |
| 播放列表第 N | 歌单第 N 首 |
| 下一首 / 上一首 | 切换歌曲 |
| 单曲循环 / 全部循环 / 随机播放 / 单曲播放 / 顺序播放 | 播放模式 |
| N 分钟后关机 | 定时停止 |
| 停止 / 暂停 / 关机 | 停止播放（带语音确认） |
| 刷新列表 | 重新扫描音乐目录 |

页面「配置 → 自定义口令」可直接编辑 `key_word_dict`（JSON 对象），格式：

```json
{
  "测试自定义口令": "exec#code1(\"hello\")",
  "提醒我喝水": "exec#httpget(\"http://192.168.1.100:8089/ring\")"
}
```

`exec#函数名(字面量参数)` 只会调用 `plugins/` 目录中同名函数，仅支持字面量参数（AST 校验）。

## 与独立版 `xiaomusic-mini/` 的关系

- 本仓库即完整可编译的 LuCI 应用：后端源文件在 `root/usr/lib/xiaomusic-mini/`，不依赖外部的独立版目录。
- 如果另外保留了独立版 `xiaomusic-mini/`（免 LuCI 部署用）：**构建 ipk 时以本仓库内的副本为准**；
  两份如需同步维护，请改完本仓库后回写独立版，或直接删除独立版目录。
- 独立版 `install.sh` 可用于免 LuCI 的纯后端部署，但**不要在同一台设备上同时用 ipk 和 `install.sh` 安装**：
  两者都会写 `/etc/init.d/xiaomusic-mini`，以后装者为准。
- 数据目录完全兼容：`/opt/xiaomusic-mini/config.json`、`mi.token`、日志无需迁移。

## 集成时对后端做的改动

| 文件 | 改动 |
|---|---|
| `main.py` | 新增 `__version__` / `--version`；启动日志打印版本；插件目录改为「显式配置 > 配置目录/plugins > 程序自带 plugins」两级加载；token 只回退 `token_path`/`.bak`（去掉硬编码旧路径）；认证失效时强制重登；`http_host=auto` 改为优先探测 LAN 地址（`ifstatus lan`/`br-lan`），避免双 NAT 时把播放 URL 指向上级路由；启动后第一次轮询只记录基线、不重放历史口令；登录失败时给出二次验证/token 提示 |
| `init.d/xiaomusic-mini` | 程序路径改到 `/usr/lib`；启动前校验 python3 与账号密码，缺失则跳过（不进入重启循环） |
| `xiaomusic-mini-check` | 增加后端存在性/版本、pip、标准库组件、配置文件、账号、音乐目录检查；输出 `ok`（全部通过）与 `env_ok`（仅运行环境）两个标志 |
| `overview.js` | 移除任意命令执行；改为调用白名单安装器；环境检测改为可折叠（有未通过项自动展开，折叠状态记忆在浏览器）；新增后端版本、安装状态轮询、安装日志、运行日志、自定义口令编辑；配置缺失时补齐后端必需项 |
| ACL | 只放行 `/opt/xiaomusic-mini/config.json` 读写和三个 helper 执行，删除原来的 `pip3 install *` / `opkg install *` 任意命令权限 |

## 安全说明

- 小米账号密码明文保存在 `/opt/xiaomusic-mini/config.json`（`600`）；页面保存时固定写 `600`。
  默认配置模板不含任何账号信息，请勿把填好账号的配置提交到公开仓库。
- 强烈不建议把该账号绑定摄像头等敏感设备；不要把 8091 端口和 LuCI 暴露到公网。
- 安装器只接受固定依赖 ID（`python3`/`pip`/`aiohttp`/`mutagen`/`miservice`/`fake_useragent`/`stdlib`），
  不接受页面传入任意命令。
- 8091 端口的 HTTP API 没有鉴权（与原版一致），仅应在内网使用；不要把该端口映射到公网。

## 排错

| 现象 | 处理 |
|---|---|
| 页面提示「接口不可用」 | 看「服务 → 查看日志」；确认配置已保存、账号密码正确、服务已启动 |
| 依赖装不上 | 「环境检测 → 安装日志」查看 pip/opkg 输出；也可 SSH 执行「查看安装命令」里的命令 |
| 登录失败 / 日志出现 `70016`、`captchaUrl`、`KeyError: 'userId'` | 账号触发了手机验证/验证码：从可正常登录的环境复制 `mi.token` 到 `/opt/xiaomusic-mini/mi.token`（`600`）后重启；或删除失效 token 重新用密码登录 |
| 播放地址是上级路由（如 192.168.6.1） | 双 NAT 环境：`http_host` 改成路由 LAN 地址（如 `192.168.5.1`），或保持 `auto` 并确认新版已用 `ifstatus lan` 探测 |
| 说话没反应 | `tail -f /opt/xiaomusic-mini/xiaomusic-mini.log`，确认有「收到语音口令」；检查路由器与音箱同网段 |
| 收到口令但不播放 | 日志看「播放指令返回」；确认音乐目录可读、`http_host` 正确、`http://路由器IP:8091/music/...` 能打开 |
| LuCI 用 https 打开时无法控制 | 浏览器会拦截 http 接口（混合内容），改用 `http://` 打开 LuCI |
| 升级后配置丢失 | 配置在 `/opt`，确认该目录在升级/重启后仍存在（U 盘挂载、sysupgrade 备份） |
| 服务启动失败 | `/etc/init.d/xiaomusic-mini start` 查看输出；确认 `python3` 与依赖齐全 |

## 验证（本仓库内已做）

- `python3 -m py_compile` 通过；`node --check` 通过；所有 shell `sh -n` 通过；JSON 均可解析。
- 用 stub 依赖跑了后端集成测试：音乐库扫描/歌单、插件加载、`/api/status`、`/api/songs`、`/api/devices`、
  CORS 预检、未知命令 400、Range 206、目录穿越 404。
- `xiaomusic-mini-check` 正常/缺依赖/标准库缺失/无 python3（降级 JSON + `--deps`）均验证。
- `xiaomusic-mini-install` 验证了白名单、后台执行、状态轮询、并发拒绝、残留锁恢复、`stdlib` 安装。
