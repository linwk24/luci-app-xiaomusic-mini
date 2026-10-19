#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xiaomusic-mini —— 小爱音箱「语音点播本地音乐」精简插件
=====================================================
目标：在 OpenWrt 路由器上以极轻依赖实现 xiaomusic 的核心功能（语音控制播放本地音乐），
砍掉 yt-dlp 下载 / FastAPI Web 框架 / ffmpeg 转码 / 定时任务等，只保留播放闭环。

适配设备：OH2P（XIAOMI 智能音箱 Pro）
  - 播放走 player_play_music 兼容接口（play_by_music_url，默认 audio_id=1582971365183456177）
  - TTS 语音反馈走 miot action：siid=7, aiid=3
  - 对话轮询走 userprofile.mina.mi.com 对话接口（OH2P 不需要 mina 轮询）

运行依赖（仅 4 个）：
  miservice-fork  （自带 aiohttp / mutagen / rich）
  fake-useragent>=2.0（MiAccount 内部使用，2.x 内置数据无需联网）

安装位置（随 LuCI 应用打包）：
  程序     /usr/lib/xiaomusic-mini/main.py
  插件     /usr/lib/xiaomusic-mini/plugins/（用户插件还可放 <配置目录>/plugins/）
  配置     /opt/xiaomusic-mini/config.json

用法：
  xiaomusic-mini --config /opt/xiaomusic-mini/config.json     # 正常启动
  xiaomusic-mini --config ... --selftest                       # 本地自检（不联网）
  xiaomusic-mini --version                                     # 打印版本
"""

__version__ = "1.0.4"

import argparse
import ast
import asyncio
import difflib
import importlib.util
import inspect
import json
import logging
import mimetypes
import os
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import aiohttp
import mutagen
from miservice import MiAccount, MiIOService, MiNAService, MiTokenStore, miio_command

try:
    from miservice_patch import install as install_miservice_patch
except Exception:  # 补丁缺失时退回 miservice 原行为
    install_miservice_patch = None

# ---------------------------------------------------------------- 常量
LATEST_ASK_API = (
    "https://userprofile.mina.mi.com/device_profile/v2/conversation"
    "?source=dialogu&hardware={hardware}&timestamp={timestamp}&limit=2"
)
SUPPORTED_EXT = (".mp3", ".flac", ".wav", ".ape", ".ogg", ".m4a", ".wma")
DEFAULT_AUDIO_ID = "1582971365183456177"

# OH2P 说话指令：miot action (siid=7, aiid=3)
TTS_SIID, TTS_AIID = 7, 3

# 各型号播放/TTS 能力（取自完整版 xiaomusic const.py，用于多设备切换）
NEED_USE_PLAY_MUSIC_API = {
    "X08C", "X08E", "X8F", "X4B", "LX05", "OH2", "OH2P", "X6A",
}
TTS_COMMAND = {
    "OH2": "5-3", "OH2P": "7-3", "LX06": "5-1", "S12": "5-1", "L15A": "7-3",
    "LX5A": "5-1", "LX01": "5-1", "LX05": "5-1", "X10A": "7-3", "L17A": "7-3",
    "ASX4B": "5-3", "L06A": "5-1", "L05B": "5-3", "L05C": "5-3", "X6A": "7-3",
    "X08E": "7-3", "L09A": "3-1", "LX04": "5-1",
}

# 需要重新登录的错误特征
AUTH_ERR_HINTS = ("70016", "401", "Login failed", "登录验证失败")

# 播放模式（与 xiaomusic const.py 一致）
PLAY_ONE, PLAY_ALL, PLAY_RND, PLAY_SIN, PLAY_SEQ = 0, 1, 2, 3, 4
MODE_TTS = {
    PLAY_ONE: "已经设置为单曲循环",
    PLAY_ALL: "已经设置为全部循环",
    PLAY_RND: "已经设置为随机播放",
    PLAY_SIN: "已经设置为单曲播放",
    PLAY_SEQ: "已经设置为顺序播放",
}
MODE_NAME = {
    PLAY_ONE: "单曲循环", PLAY_ALL: "全部循环", PLAY_RND: "随机播放",
    PLAY_SIN: "单曲播放", PLAY_SEQ: "顺序播放",
}
MODE_KEY = {
    "one": PLAY_ONE, "all": PLAY_ALL, "rnd": PLAY_RND,
    "sin": PLAY_SIN, "seq": PLAY_SEQ,
}

# 语音口令匹配表（长词在前，避免短词抢先命中）
KEYWORDS = [
    ("分钟后关机", "sleep_stop"),
    ("下一首", "next"),
    ("上一首", "prev"),
    ("单曲循环", "mode_one"),
    ("全部循环", "mode_all"),
    ("随机播放", "mode_rnd"),
    ("单曲播放", "mode_sin"),
    ("顺序播放", "mode_seq"),
    ("刷新列表", "rescan"),
    ("播放列表第", "list_index"),
    ("播放列表", "play_list"),
    ("播放歌单", "play_list"),
    ("播放本地歌曲", "play"),
    ("本地播放歌曲", "play"),
    ("播放歌曲", "play"),
    ("放歌曲", "play"),
    ("在线播放", "online"),
    ("播放歌手", "online"),
    ("关机", "stop"),
    ("暂停", "stop"),
    ("停止播放", "stop"),
    ("停止", "stop"),
]


# ---------------------------------------------------------------- 自定义口令 (exec#)
# 参照完整版 xiaomusic 的插件机制精简实现：
#   配置中把口令映射为 "exec#函数名(字面量参数)"，如 exec#httpget("https://...")
#   运行时用 AST 解析出函数名与字面量参数，再查 PLUGIN_FUNCS 调用。
#   插件函数放在 plugins/ 目录，文件名与函数名相同，签名 def name(app, args, query)。
PLUGIN_FUNCS = {}


def register_plugin(name):
    def deco(fn):
        PLUGIN_FUNCS[name] = fn
        return fn
    return deco


def parse_plugin_call(code):
    """解析形如 'func("a", 1)' 的调用，仅允许字面量参数。返回 (函数名, [参数])"""
    try:
        expr = ast.parse(code, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"自定义口令语法错误: {code}") from e
    call = expr.body
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        raise ValueError("自定义口令必须是直接函数调用，如 func(\"arg\")")
    if call.keywords:
        raise ValueError("自定义口令不支持关键字参数")
    return call.func.id, [_parse_literal_arg(a) for a in call.args]


def _parse_literal_arg(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (str, int, float, bool, type(None))):
            return node.value
    elif isinstance(node, (ast.List, ast.Tuple)):
        return [_parse_literal_arg(e) for e in node.elts]
    elif isinstance(node, ast.Dict):
        keys = [None if k is None else _parse_literal_arg(k) for k in node.keys]
        vals = [_parse_literal_arg(v) for v in node.values]
        return dict(zip(keys, vals))
    raise ValueError("自定义口令仅支持字面量参数")


def load_plugins(directory, log=None):
    """扫描 directory 下的 *.py，把与文件名同名的函数注册为 exec# 函数。

    插件函数签名：def name(app, args, query)（同步或 async 均可）
      app   —— XiaomiMini 实例（可用 app.log / app.tts / app.session 等）
      args  —— exec# 里的字面量参数列表，如 exec#func("a", 1) -> ["a", 1]
      query —— 触发它的完整语音文本
    """
    if not directory or not os.path.isdir(directory):
        # 默认插件目录可能不存在（未放用户插件），静默跳过
        return []
    loaded = []
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        path = os.path.join(directory, fn)
        func_name = fn[:-3]
        mod_name = "xiaomusic_plugin_" + func_name
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            if log:
                log.error(f"加载插件失败 {fn}: {e}")
            continue
        func = getattr(module, func_name, None)
        if callable(func):
            PLUGIN_FUNCS[func_name] = func
            loaded.append(func_name)
        elif log:
            log.warning(f"插件 {fn} 中未找到同名函数 {func_name}()")
    if log:
        log.info(f"已加载插件: {', '.join(loaded) if loaded else '(无)'}")
    return loaded


# ---------------------------------------------------------------- 工具
def detect_gateway():
    """从 /proc/net/route 探测默认网关（路由器自身 IP），用于拼接播放 URL"""
    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    gw_hex = parts[2]
                    if len(gw_hex) == 8:
                        return socket.inet_ntoa(bytes.fromhex(gw_hex)[::-1])
    except Exception:
        pass
    return None


def detect_lan_ip():
    """探测本机局域网 IPv4（音箱需通过它回连本机取音乐）。

    双 NAT 或旁路部署时默认网关常是上级路由（如 192.168.6.1），
    而音箱在 LAN 网段，必须用 br-lan 的地址，否则播放 URL 不可达。
    """
    # 1) OpenWrt：lan 接口地址最准确
    try:
        out = subprocess.run(
            ["ifstatus", "lan"], capture_output=True, text=True, timeout=3
        ).stdout
        data = json.loads(out or "{}")
        for addr in data.get("ipv4-address") or []:
            ip = str(addr.get("address") or "").strip()
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass

    # 2) 常见 LAN 网桥/接口
    for dev in ("br-lan", "br0", "lan"):
        try:
            out = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", "dev", dev],
                capture_output=True, text=True, timeout=3,
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if "inet" in parts:
                    ip = parts[parts.index("inet") + 1].split("/")[0]
                    if ip and not ip.startswith("127."):
                        return ip
        except Exception:
            pass

    return None


def setup_logging(cfg):
    handlers = [logging.StreamHandler()]
    log_file = cfg.get("log_file")
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except Exception:
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    logging.getLogger("miservice").setLevel(logging.WARNING)  # 压 miservice 日志
    return logging.getLogger("xiaomusic-mini")


# ---------------------------------------------------------------- 音乐库
class MusicLibrary:
    """扫描音乐目录，建立 显示名->相对路径 索引与按目录的歌单"""

    def __init__(self, root):
        self.root = root
        self.index = {}   # 显示名（去扩展名） -> 相对路径
        self.lists = {}   # 歌单名（目录名/全部） -> [显示名...]
        self.rescan()

    def rescan(self):
        index, sub_lists = {}, {}
        all_names = []
        if not os.path.isdir(self.root):
            self.index, self.lists = index, {"全部": []}
            return
        for dirpath, _dirnames, filenames in os.walk(self.root):
            rel = os.path.relpath(dirpath, self.root)
            list_name = None if rel == "." else rel.replace(os.sep, "/")
            for fn in sorted(filenames):
                if not fn.lower().endswith(SUPPORTED_EXT):
                    continue
                relpath = os.path.relpath(os.path.join(dirpath, fn), self.root)
                relpath = relpath.replace(os.sep, "/")
                name = os.path.splitext(fn)[0]
                index[name] = relpath
                all_names.append(name)                 # 「全部」= 所有子目录歌曲合集
                if list_name:
                    sub_lists.setdefault(list_name, []).append(name)
        lists = {"全部": all_names}
        lists.update(sub_lists)
        self.index, self.lists = index, lists

    def find(self, keyword, cutoff=0.6):
        """查找歌曲：先精确 → 再包含 → 最后模糊匹配，返回显示名"""
        if not keyword:
            return None
        if keyword in self.index:
            return keyword
        for name in self.index:
            if keyword in name:
                return name
        best, best_ratio = None, cutoff
        for name in self.index:
            ratio = difflib.SequenceMatcher(None, keyword, name).ratio()
            if ratio > best_ratio:
                best, best_ratio = name, ratio
        return best

    def get_path(self, name):
        rel = self.index.get(name)
        return os.path.join(self.root, rel) if rel else None

    def duration(self, name):
        path = self.get_path(name)
        if not path:
            return None
        try:
            info = mutagen.File(path)
            if info and getattr(info, "info", None) and info.info.length:
                return float(info.info.length)
        except Exception:
            pass
        return None


# ---------------------------------------------------------------- HTTP 文件服务 + 轻量控制台
class ControlContext:
    """把 HTTP 线程里的同步请求桥接到主事件循环（零依赖 Web 控制台用）"""

    def __init__(self, app, loop):
        self.app = app
        self.loop = loop

    def submit(self, coro, timeout=25):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout)


PANEL_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0d14">
<title>xiaomusic-mini 控制台</title>
<style>
:root{
  --bg:#0b0d14;--bg2:#12151f;--card:rgba(255,255,255,.06);--card-b:rgba(255,255,255,.09);
  --fg:#eef0f6;--mut:#8b91a3;--acc:#7c8cff;--acc2:#b06cff;--ok:#3ddc97;--line:rgba(255,255,255,.08);
  --r:18px;
}
@media (prefers-color-scheme:light){
  :root{--bg:#f2f4fa;--bg2:#e8ebf5;--card:rgba(255,255,255,.75);--card-b:rgba(20,24,40,.08);
    --fg:#171a26;--mut:#6b7186;--line:rgba(20,24,40,.08)}
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Roboto,sans-serif;
  color:var(--fg);min-height:100vh;
  background:radial-gradient(900px 500px at 10% -10%,rgba(124,140,255,.22),transparent 60%),
             radial-gradient(800px 500px at 100% 0%,rgba(176,108,255,.18),transparent 55%),
             linear-gradient(180deg,var(--bg),var(--bg2));background-attachment:fixed}
.wrap{max-width:680px;margin:0 auto;padding:max(18px,env(safe-area-inset-top)) 16px 40px}
header{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:4px 0 16px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:18px;letter-spacing:.2px}
.logo{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--acc),var(--acc2));box-shadow:0 6px 18px rgba(124,140,255,.4)}
.logo svg{width:18px;height:18px;fill:#fff}
.dev{font-size:12px;color:var(--mut);display:flex;align-items:center;gap:6px;max-width:55%;text-align:right}
.dot{width:8px;height:8px;border-radius:50%;background:var(--mut);flex:none}
.dot.on{background:var(--ok);box-shadow:0 0 0 4px rgba(61,220,151,.18)}
.dev span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.card{background:var(--card);border:1px solid var(--card-b);border-radius:var(--r);padding:16px;margin-bottom:14px;
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px)}

.seg{display:flex;background:var(--card);border:1px solid var(--card-b);border-radius:14px;padding:4px;margin-bottom:14px;position:relative}
.seg button{flex:1;border:0;background:transparent;color:var(--mut);padding:10px 8px;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;transition:.2s}
.seg button.on{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;box-shadow:0 4px 14px rgba(124,140,255,.35)}

select,input[type=text],input:not([type]){width:100%;padding:11px 12px;border-radius:12px;border:1px solid var(--line);
  background:rgba(127,127,127,.08);color:var(--fg);font-size:14px;outline:none;transition:.2s;min-width:0}
select:focus,input:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(124,140,255,.2)}
select option{background:#1a1d28;color:#eee}
.devsel{margin-bottom:14px}

/* now playing */
.player{display:flex;gap:16px;align-items:center}
.art{width:84px;height:84px;border-radius:18px;flex:none;display:grid;place-items:center;position:relative;overflow:hidden;
  background:linear-gradient(135deg,var(--acc),var(--acc2));box-shadow:0 10px 26px rgba(124,140,255,.35)}
.art svg{width:36px;height:36px;fill:rgba(255,255,255,.9)}
.eq{position:absolute;inset:0;display:none;align-items:flex-end;justify-content:center;gap:5px;padding:0 0 16px;background:rgba(0,0,0,.35)}
.eq i{width:6px;height:10px;border-radius:3px;background:#fff;animation:eq 1s ease-in-out infinite}
.eq i:nth-child(2){animation-delay:.2s}.eq i:nth-child(3){animation-delay:.4s}.eq i:nth-child(4){animation-delay:.1s}
@keyframes eq{0%,100%{height:8px}50%{height:34px}}
.art.playing .eq{display:flex}.art.playing>svg{display:none}
.meta{min-width:0;flex:1}
.now{font-size:18px;font-weight:700;line-height:1.3;word-break:break-all;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{font-size:12px;color:var(--mut);background:rgba(127,127,127,.12);border-radius:999px;padding:3px 10px}
.chip b{color:var(--fg);font-weight:600}

.ctrl{display:flex;align-items:center;justify-content:center;gap:14px;margin:18px 0 4px}
.ib{width:48px;height:48px;border-radius:50%;border:1px solid var(--line);background:rgba(127,127,127,.1);color:var(--fg);
  display:grid;place-items:center;cursor:pointer;transition:.15s;padding:0}
.ib svg{width:22px;height:22px;fill:currentColor}
.ib:hover{background:rgba(127,127,127,.2)}.ib:active{transform:scale(.92)}
.ib.big{width:64px;height:64px;border:0;color:#fff;background:linear-gradient(135deg,var(--acc),var(--acc2));box-shadow:0 8px 22px rgba(124,140,255,.45)}
.ib.big svg{width:28px;height:28px}
.ib.danger:hover{color:#ff6b6b}

.row{display:flex;gap:10px;align-items:center;margin-top:14px}
.btn{flex:none;border:1px solid var(--line);background:rgba(127,127,127,.1);color:var(--fg);padding:11px 14px;border-radius:12px;
  font-size:14px;cursor:pointer;transition:.15s;white-space:nowrap}
.btn:hover{background:rgba(127,127,127,.2)}.btn:active{transform:scale(.97)}

/* volume */
.vol{display:flex;align-items:center;gap:12px}
.vol svg{width:20px;height:20px;fill:var(--mut);flex:none;cursor:pointer}
.vol .v{width:40px;text-align:right;font-size:13px;color:var(--mut);font-variant-numeric:tabular-nums}
input[type=range]{-webkit-appearance:none;appearance:none;flex:1;height:6px;border-radius:99px;padding:0;border:0;outline:none;
  background:linear-gradient(90deg,var(--acc) var(--p,0%),rgba(127,127,127,.25) var(--p,0%));box-shadow:none}
input[type=range]:focus{box-shadow:none}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:#fff;border:0;box-shadow:0 2px 8px rgba(0,0,0,.4)}
input[type=range]::-moz-range-thumb{width:18px;height:18px;border-radius:50%;background:#fff;border:0;box-shadow:0 2px 8px rgba(0,0,0,.4)}

/* list */
.lhead{display:flex;gap:10px}
.lhead select{flex:0 0 40%}
ul{list-style:none;margin:12px 0 0;padding:0;max-height:380px;overflow:auto;scrollbar-width:thin}
li{display:flex;align-items:center;gap:10px;padding:10px;border-radius:12px;cursor:pointer;font-size:14px;transition:.15s;word-break:break-all}
li:hover{background:rgba(127,127,127,.12)}
li .n{width:26px;text-align:right;color:var(--mut);font-size:12px;flex:none;font-variant-numeric:tabular-nums}
li .t{flex:1}
li.cur{background:linear-gradient(90deg,rgba(124,140,255,.2),transparent);color:var(--ok);font-weight:600}
li.cur .n{color:var(--ok)}
li.empty{color:var(--mut);cursor:default;justify-content:center}li.empty:hover{background:none}

.toast{position:fixed;left:50%;bottom:max(24px,env(safe-area-inset-bottom));transform:translate(-50%,20px);
  background:rgba(30,33,45,.92);color:#fff;padding:10px 18px;border-radius:99px;font-size:14px;opacity:0;pointer-events:none;
  transition:.3s;backdrop-filter:blur(10px);z-index:9}
.toast.show{opacity:1;transform:translate(-50%,0)}
</style></head><body><div class="wrap">

<header>
  <div class="brand"><div class="logo"><svg viewBox="0 0 24 24"><path d="M12 3v10.55A4 4 0 1 0 14 17V7h4V3z"/></svg></div>xiaomusic-mini</div>
  <div class="dev"><i class="dot" id="dot"></i><span id="dev">连接中…</span></div>
</header>

<div class="devsel" id="devrow" style="display:none">
  <select id="dsel" onchange="switchDevice(this.value)"></select>
</div>

<div class="seg" id="seg">
  <button data-m="speaker" class="on">音响播放</button>
  <button data-m="local">本机播放</button>
</div>

<div class="card">
  <div class="player">
    <div class="art" id="art">
      <svg viewBox="0 0 24 24"><path d="M12 3v10.55A4 4 0 1 0 14 17V7h4V3z"/></svg>
      <div class="eq"><i></i><i></i><i></i><i></i></div>
    </div>
    <div class="meta">
      <div class="now" id="cur">未在播放</div>
      <div class="chips">
        <span class="chip">歌单 <b id="clist">-</b></span>
        <span class="chip">模式 <b id="mode">-</b></span>
        <span class="chip">共 <b id="total">0</b> 首</span>
      </div>
    </div>
  </div>

  <div class="ctrl">
    <button class="ib" onclick="prev()" title="上一首"><svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/></svg></button>
    <button class="ib big" onclick="toggle()" title="播放/继续"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></button>
    <button class="ib" onclick="next()" title="下一首"><svg viewBox="0 0 24 24"><path d="M6 18l8.5-6L6 6zM16 6v12h2V6z"/></svg></button>
    <button class="ib danger" onclick="stop()" title="停止"><svg viewBox="0 0 24 24"><path d="M6 6h12v12H6z"/></svg></button>
  </div>

  <div class="row">
    <select id="msel" onchange="setModeSel(this.value)">
      <option value="one">🔂 单曲循环</option><option value="all" selected>🔁 全部循环</option>
      <option value="rnd">🔀 随机播放</option><option value="sin">▶ 单曲播放</option><option value="seq">➡ 顺序播放</option>
    </select>
    <button class="btn" onclick="c('rescan')">↻ 刷新歌单</button>
  </div>
</div>

<div class="card">
  <div class="vol">
    <button class="ib" style="width:36px;height:36px" onclick="volStep(-5)" title="减小音量"><svg viewBox="0 0 24 24" style="width:18px;height:18px;fill:currentColor"><path d="M5 11h14v2H5z"/></svg></button>
    <input type="range" id="vol" min="0" max="100" step="1" oninput="onVol(this.value)" onchange="volCommit(this.value)">
    <button class="ib" style="width:36px;height:36px" onclick="volStep(5)" title="增大音量"><svg viewBox="0 0 24 24" style="width:18px;height:18px;fill:currentColor"><path d="M19 11h-6V5h-2v6H5v2h6v6h2v-6h6z"/></svg></button>
    <span class="v"><span id="volv">-</span>%</span>
  </div>
</div>

<div class="card">
  <div class="lhead">
    <select id="lsel" onchange="loadSongs()"></select>
    <input id="search" placeholder="搜索歌名，回车播放" oninput="renderSongs()" onkeydown="if(event.key==='Enter')search(this.value)">
  </div>
  <ul id="songs"></ul>
</div>

<div class="toast" id="toast"></div>
<script>
const $=id=>document.getElementById(id);
let toastTimer;
function toast(t){const e=$('toast');e.textContent=t;e.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>e.classList.remove('show'),1800)}
async function api(u,o){const r=await fetch(u,o);if(!r.ok)throw new Error((await r.json().catch(()=>({}))).error||r.status);return r.json()}
let inited=false, volDrag=false;
let mode=localStorage.getItem('p_mode')||'speaker';
let curItems=[], localName=null, localIdx=-1, speakerName=null;
const audio=new Audio();audio.preload='none';
function setSegUI(){[...$('seg').children].forEach(b=>b.classList.toggle('on',b.dataset.m===mode))}
function setMode(m){mode=m;localStorage.setItem('p_mode',m);setSegUI();toast('切换到'+(m==='local'?'本机播放':'音响播放'));status()}
function paintVol(v){const e=$('vol');e.style.setProperty('--p',v+'%')}
audio.addEventListener('ended',()=>localAdvance());
audio.addEventListener('pause',()=>{if(mode==='local')status()});
audio.addEventListener('play',()=>{if(mode==='local')status()});
function highlight(){
  const cur=mode==='local'?localName:speakerName;
  [...$('songs').querySelectorAll('li[data-i]')].forEach(li=>{
    const it=curItems[+li.dataset.i];li.classList.toggle('cur',!!cur&&it&&it.name===cur)});
}
async function status(){
  try{const d=await api('/api/status');
    $('dot').classList.add('on');
    $('dev').textContent=(mode==='local'?'本机':'音响')+(d.device_name?' · '+d.device_name:'');
    $('total').textContent=d.total;
    const ls=$('lsel');
    if(ls.options.length!==d.lists.length||[...ls.options].map(o=>o.value).join()!==d.lists.join()){
      ls.innerHTML=d.lists.map(n=>`<option>${n}</option>`).join('');
      ls.value=d.cur_list&&d.lists.includes(d.cur_list)?d.cur_list:ls.options[0]?.value;
    }
    speakerName=d.cur_name||null;
    let playing;
    if(mode==='speaker'){
      $('cur').textContent=d.cur_name||'未在播放';
      $('clist').textContent=d.cur_list||'-';
      $('mode').textContent=d.mode||'-';
      $('msel').value=d.mode_key||'all';
      if(d.volume!=null&&!volDrag){$('vol').value=d.volume;$('volv').textContent=d.volume;paintVol(d.volume)}
      playing=!!d.playing;
    }else{
      $('cur').textContent=localName||'（本机未播放）';
      $('clist').textContent=$('lsel').value||'-';
      $('mode').textContent=$('msel').selectedOptions[0]?.textContent.replace(/^\S+\s/,'')||'-';
      if(!volDrag){const v=Math.round(audio.volume*100);$('vol').value=v;$('volv').textContent=v;paintVol(v)}
      playing=!!localName&&!audio.paused;
    }
    $('art').classList.toggle('playing',playing);
    highlight();
    if(d.device){const o=[...$('dsel').options].find(x=>x.value===d.device);if(o)$('dsel').value=d.device}
    if(!inited){inited=true;loadDevices();loadSongs()}
  }catch(e){$('dot').classList.remove('on');$('dev').textContent='连接失败: '+e.message}
}
async function loadSongs(){
  try{const d=await api('/api/songs?list='+encodeURIComponent($('lsel').value));
    curItems=d.items||[];
    if(!curItems.some(it=>it.name===localName))localIdx=-1;else localIdx=curItems.findIndex(it=>it.name===localName);
    renderSongs();
  }catch(e){toast(e.message)}
}
function esc(s){return String(s).replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]))}
function renderSongs(){
  const k=($('search').value||'').trim().toLowerCase();
  const html=curItems.map((it,i)=>({it,i})).filter(x=>!k||x.it.name.toLowerCase().includes(k))
    .map(x=>`<li data-i="${x.i}"><span class="n">${x.i+1}</span><span class="t">${esc(x.it.name)}</span></li>`).join('');
  $('songs').innerHTML=html||'<li class="empty">（空）</li>';
  [...$('songs').querySelectorAll('li[data-i]')].forEach(li=>li.onclick=()=>playIndex(+li.dataset.i));
  highlight();
}
function playIndex(i){
  const it=curItems[i];if(!it){toast('无效歌曲');return}
  if(mode==='local'){localIdx=i;localName=it.name;audio.src=it.url;audio.play().then(status).catch(e=>toast('播放失败: '+e.message))}
  else c('play',it.name,$('lsel').value);
}
function c(cmd,arg,list){
  return api('/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd,arg,list:list||''})})
    .then(()=>{toast('已发送: '+cmd);setTimeout(status,600)}).catch(e=>toast('失败: '+e.message));
}
function prev(){mode==='local'?localStep(-1):c('prev')}
function next(){mode==='local'?localStep(1):c('next')}
function toggle(){if(mode==='speaker'){c('toggle');return}
  if(!localName&&curItems.length){playIndex(0);return}
  audio.paused?audio.play().then(status).catch(e=>toast(e.message)):audio.pause()}
function stop(){if(mode==='speaker'){c('stop');return}
  audio.pause();audio.currentTime=0;localName=null;localIdx=-1;status()}
function localStep(d){
  if(!curItems.length){toast('歌单为空');return}
  const m=$('msel').value;let i;
  if(m==='rnd')i=Math.floor(Math.random()*curItems.length);
  else{const base=localIdx<0?(d>0?0:curItems.length-1):localIdx;i=(base+d+curItems.length)%curItems.length}
  playIndex(i);
}
function localAdvance(){
  const m=$('msel').value;
  if(m==='sin'){localName=null;localIdx=-1;status();return}
  if(m==='one'&&localIdx>=0){audio.currentTime=0;audio.play().catch(()=>{});return}
  if(m==='seq'&&localIdx>=curItems.length-1){localName=null;localIdx=-1;status();return}
  localStep(1);
}
function setModeSel(v){if(mode==='speaker')c('mode',v);else{$('mode').textContent=$('msel').selectedOptions[0].textContent.replace(/^\S+\s/,'')}}
function search(v){v=(v||'').trim();if(!v)return;
  if(mode==='speaker'){c('play',v,$('lsel').value);return}
  const i=curItems.findIndex(it=>it.name.includes(v));
  i>=0?playIndex(i):toast('本机歌单里没有匹配')}
function onVol(v){$('volv').textContent=v;volDrag=true;paintVol(v)}
function volCommit(v){volDrag=false;mode==='local'?(audio.volume=v/100):c('volume',v)}
function volStep(dv){const v=Math.max(0,Math.min(100,(+$('vol').value||0)+dv));$('vol').value=v;$('volv').textContent=v;paintVol(v);mode==='local'?(audio.volume=v/100):c('volume',String(v))}
async function loadDevices(){
  try{const d=await api('/api/devices');const devs=d.devices||[];
    const sel=$('dsel');
    if(devs.length>1){
      $('devrow').style.display='';
      sel.innerHTML=devs.map(x=>`<option value="${x.did||x.device_id}">${x.name}（${x.hardware}）</option>`).join('');
      const cur=devs.find(x=>x.current);if(cur)sel.value=cur.did||cur.device_id;
    }else{$('devrow').style.display='none'}
  }catch(e){}
}
function switchDevice(v){localName=null;localIdx=-1;
  c('device',v).then(()=>{setTimeout(()=>{loadDevices();status()},600)});}
$('seg').onclick=e=>{const b=e.target.closest('button[data-m]');if(b)setMode(b.dataset.m)};
setSegUI();paintVol(0);status();setInterval(status,2000);
</script></div></body></html>
"""


class MusicHandler(SimpleHTTPRequestHandler):
    """同端口提供：/music/<相对路径> 文件服务 + / 控制台 + /api/* 控制接口（纯 stdlib）"""

    def log_message(self, *args):
        pass

    # ---- 路由 ----
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/console", "/index.html"):
            return self._serve_panel()
        if path == "/api/status":
            return self._api_status()
        if path == "/api/songs":
            return self._api_songs()
        if path == "/api/devices":
            return self._api_devices()
        if path == "/api/cmd":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._api_cmd((q.get("cmd") or [""])[0], (q.get("arg") or [""])[0], (q.get("list") or [""])[0])
        if path.startswith("/music/"):
            return self._serve_music()
        return self.send_error(404)

    # ---- 音乐文件服务（支持 Range，便于浏览器拖动进度）----
    def _serve_music(self):
        music_dir = getattr(self.server, "music_dir", None) or os.getcwd()
        raw = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if raw.startswith("/music/"):
            raw = raw[len("/music/"):]
        root = os.path.realpath(music_dir)
        full = os.path.realpath(os.path.join(root, raw))
        if not (full == root or full.startswith(root + os.sep)) or not os.path.isfile(full):
            return self.send_error(404)
        try:
            size = os.path.getsize(full)
            mtime = int(os.path.getmtime(full))
        except OSError:
            return self.send_error(404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                s, _, e = rng[len("bytes="):].split(",")[0].strip().partition("-")
                if s == "":
                    start = max(0, size - int(e))
                else:
                    start = int(s)
                    end = int(e) if e else size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                code = 206
            except ValueError:
                start, end, code = 0, size - 1, 200
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self.date_time_string(mtime))
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(full, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/cmd":
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}
        return self._api_cmd(str(body.get("cmd") or ""), str(body.get("arg") or ""), str(body.get("list") or ""))

    # ---- 基础响应 ----
    def _ctx(self):
        return getattr(self.server, "control", None)

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # 允许 LuCI 页面（同机不同端口）跨源访问 API
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    # ---- CORS 预检（浏览器对 JSON POST 的 preflight）----
    def do_OPTIONS(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/"):
            return self.send_error(404)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_panel(self):
        data = PANEL_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- 接口 ----
    def _api_status(self):
        ctx = self._ctx()
        if not ctx:
            return self._json({"ok": False, "error": "服务未初始化"}, 503)
        app = ctx.app
        cname = next((d["name"] for d in app.devices if d["device_id"] == app.device_id), "")
        return self._json({
            "ok": True,
            "device": app.did,
            "device_name": cname,
            "cur_name": app.cur_name,
            "playing": bool(app.playing),
            "cur_list": app.cur_list,
            "mode": MODE_NAME.get(app.play_type, ""),
            "mode_key": next((k for k, v in MODE_KEY.items() if v == app.play_type), "all"),
            "play_type": app.play_type,
            "volume": app.volume,
            "total": len(app.library.index),
            "lists": list(app.library.lists.keys()),
        })

    def _api_songs(self):
        ctx = self._ctx()
        if not ctx:
            return self._json({"ok": False, "error": "服务未初始化"}, 503)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = (q.get("list") or ["全部"])[0]
        names = ctx.app.library.lists.get(name, [])
        items = []
        for n in names:
            rel = ctx.app.library.index.get(n)
            items.append({"name": n, "url": f"/music/{urllib.parse.quote(rel)}" if rel else ""})
        return self._json({"ok": True, "list": name, "songs": names, "items": items})

    def _api_devices(self):
        ctx = self._ctx()
        if not ctx:
            return self._json({"ok": False, "error": "服务未初始化"}, 503)
        app = ctx.app
        devs = [
            {
                "name": d["name"],
                "did": d["did"],
                "device_id": d["device_id"],
                "hardware": d["hardware"],
                "current": d["device_id"] == app.device_id,
            }
            for d in app.devices
        ]
        return self._json({"ok": True, "devices": devs})

    def _api_cmd(self, cmd, arg, lst=""):
        ctx = self._ctx()
        if not ctx:
            return self._json({"ok": False, "error": "服务未初始化"}, 503)
        app = ctx.app
        if not cmd:
            return self._json({"ok": False, "error": "缺少 cmd"}, 400)
        if app.session is None or app.mina is None:
            return self._json({"ok": False, "error": "尚未登录/初始化，请稍后重试"}, 503)
        coro = _build_cmd_coro(app, cmd, arg, lst)
        if coro is None:
            return self._json({"ok": False, "error": f"未知命令: {cmd}"}, 400)
        try:
            ctx.submit(coro)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)
        return self._json({"ok": True, "cmd": cmd})


def _build_cmd_coro(app, cmd, arg, lst=""):
    """把控制台命令映射到 app 的异步方法，返回协程（未知命令返回 None）"""
    arg = (arg or "").strip()
    if cmd == "play":
        return app.cmd_play(arg, (lst or "").strip() or None)
    if cmd == "toggle":
        # 无独立暂停：有当前曲则重播当前曲，否则续播/播第一首
        return app.cmd_play(app.cur_name or "")
    if cmd == "next":
        return app.cmd_next()
    if cmd == "prev":
        return app.cmd_prev()
    if cmd == "stop":
        return app.cmd_stop()
    if cmd == "rescan":
        return app.cmd_rescan()
    if cmd == "mode":
        key = MODE_KEY.get(arg)
        return app.cmd_mode("mode_" + arg) if key is not None else None
    if cmd == "play_list":
        return app.cmd_play_list(arg)
    if cmd == "volume":
        return app.cmd_volume(arg)
    if cmd == "device":
        return app.select_device(arg)
    return None


def start_http_server(port, music_dir, control=None):
    handler = lambda *a, **kw: MusicHandler(*a, directory=music_dir, **kw)
    srv = ThreadingHTTPServer(("0.0.0.0", port), handler)
    srv.control = control
    srv.music_dir = music_dir
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- 主应用
class XiaomiMini:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.session = None
        self.account = None
        self.mina = None       # MiNAService（播放/暂停/状态）
        self.miio = None       # MiIOService（TTS 7-3）
        self.device_id = None  # mina deviceID（播放/轮询用）
        self.did = str(cfg.get("mi_did") or "")   # miotDID（TTS 用）
        self.hardware = None   # 如 OH2P
        self.devices = []      # 账号下所有音箱

        self.library = MusicLibrary(cfg.get("music_dir", "music"))
        self.base_url = self._build_base_url()
        self.log.info(f"音乐库: {len(self.library.index)} 首, 播放地址前缀: {self.base_url}")

        self.play_type = PLAY_ALL
        self.cur_list = "全部"
        self.cur_name = None
        self.playing = False  # 是否正在播放（停止/单曲播放结束后为 False）
        self.volume = None    # 最近一次设置的音量（0-100，未知为 None）
        self._next_task = None
        self._sleep_task = None
        self._last_ts = 0
        self._skip_history = True   # 启动后第一次轮询只记录时间戳，不执行历史口令
        self._reinit_cooldown = 0.0
        self._force_login = False   # 认证失效时置位，强制用账号密码重新登录
        self._running = True

        # 自定义口令（config.key_word_dict）优先于内置，长词在前避免短词抢先命中
        custom = []
        kw_dict = cfg.get("key_word_dict")
        if isinstance(kw_dict, dict):
            custom = sorted(kw_dict.items(), key=lambda kv: len(kv[0]), reverse=True)
        self.keywords = custom + list(KEYWORDS)

    # ---- 服务初始化 / 重登录 ----
    async def init_services(self):
        if install_miservice_patch:
            install_miservice_patch()
        if self.session is not None:
            try:
                await self.session.close()
            except Exception:
                pass
        self.session = aiohttp.ClientSession()
        token_path = self.cfg.get("token_path") or ""
        self.account = MiAccount(
            self.session,
            self.cfg.get("account") or "",
            self.cfg.get("password") or "",
            MiTokenStore(token_path),
        )
        ok = True
        tok = None
        if not self._force_login:
            # 优先复用已保存的 token；token 失效时由 _handle_error 置位 _force_login 重试
            for tp in (token_path, token_path + ".bak"):
                try:
                    if tp and os.path.exists(tp):
                        with open(tp, encoding="utf-8") as f:
                            tok = json.load(f)
                        if tok:
                            break
                except Exception:
                    pass
        if tok:
            self.account.token = tok
        else:
            ok = await self.account.login("xiaomiio")
        if not ok:
            raise RuntimeError(
                "小米账号登录失败：密码错误，或账号触发了手机验证/验证码（Xiaomi 返回 captchaUrl/notificationUrl）。"
                "若账号已开启二次验证，请把能正常登录的环境生成的 mi.token 放到 "
                f"{token_path or '<token_path>'} 后重启服务（token 有效时不需要密码）。"
            )
        self.mina = MiNAService(self.account)
        self.miio = MiIOService(self.account)
        await self.discover_device()
        self._force_login = False
        self.log.info(f"登录成功. 设备 deviceId={self.device_id} hardware={self.hardware} did={self.did}")

    async def discover_device(self):
        devices = await self.mina.device_list() or []
        self.devices = [
            {
                "name": d.get("name") or d.get("hardware") or "未知设备",
                "device_id": d.get("deviceID") or "",
                "hardware": d.get("hardware") or "",
                "did": str(d.get("miotDID") or ""),
            }
            for d in devices
        ]
        target = None
        if self.did:
            for d in devices:
                if str(d.get("miotDID") or "") == self.did:
                    target = d
                    break
        if target is None:
            # 未指定或未匹配：优先找 OH2P，否则取第一个音箱
            for d in devices:
                if (d.get("hardware") or "").upper().startswith("OH2"):
                    target = d
                    break
            if target is None and devices:
                target = devices[0]
        if target is None:
            raise RuntimeError("设备列表为空，请确认账号下已绑定小爱音箱")
        self.device_id = target.get("deviceID") or ""
        self.hardware = target.get("hardware") or ""
        self.did = str(target.get("miotDID") or self.did)
        if not self.device_id:
            raise RuntimeError("未取得 deviceID，登录可能已失效")

    async def select_device(self, key):
        """按 miotDID 或 mina deviceID 切换当前控制的音箱"""
        key = str(key or "").strip()
        if not key:
            return False
        for d in self.devices:
            if key in (d["did"], d["device_id"]):
                self.device_id = d["device_id"]
                self.hardware = d["hardware"]
                self.did = d["did"] or self.did
                self.cur_name = None
                self.playing = False
                self.log.info(f"切换设备: {d['name']} (hardware={self.hardware}, did={self.did})")
                return True
        self.log.warning(f"未找到设备: {key}")
        return False

    def _build_base_url(self):
        host = str(self.cfg.get("http_host") or "auto").strip()
        if not host or host.lower() == "auto":
            # 优先 LAN 地址：默认网关在双 NAT/旁路场景下常常是上级路由，音箱回连不到
            host = detect_lan_ip() or detect_gateway() or "192.168.1.1"
            self.log.info(f"http_host=auto -> 使用本机地址 {host}")
        return f"http://{host}:{int(self.cfg.get('http_port', 8091))}"

    # ---- 轮询对话 ----
    async def poll_loop(self):
        while self._running:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._handle_error(e)
            await asyncio.sleep(max(0.5, float(self.cfg.get("poll_sec", 1.5))))

    def _poll_cookies(self):
        # 接口要求 userId / deviceId(大写I) / serviceToken，缺失会 400
        cookies = {"deviceid": self.device_id, "deviceId": self.device_id}
        tok = self.account.token if isinstance(getattr(self.account, "token", None), dict) else {}
        uid = str(tok.get("userId") or "")
        if uid:
            cookies["userId"] = uid
        mico = tok.get("micoapi")
        if isinstance(mico, (list, tuple)) and len(mico) > 1 and mico[1]:
            cookies["serviceToken"] = mico[1]
        return cookies

    async def poll_once(self):
        if not self.device_id:
            await self.init_services()
            return
        cookies = self._poll_cookies()
        url = LATEST_ASK_API.format(
            hardware=self.hardware,
            timestamp=int(time.time() * 1000),
        )
        timeout = aiohttp.ClientTimeout(total=15)
        async with self.session.get(url, cookies=cookies, timeout=timeout) as r:
            if r.status != 200:
                raise RuntimeError(f"轮询接口返回 {r.status}")
            data = await r.json(content_type=None)
        records = (json.loads(data.get("data") or "{}").get("records")) or []
        if not records:
            return
        rec = records[0]
        ts = int(rec.get("time") or 0)
        query = (rec.get("query") or "").strip()
        if self._skip_history:
            # 启动时把已有最新记录当作基线，避免重放旧口令（重登后同样适用）
            self._skip_history = False
            self._last_ts = ts
            if query:
                self.log.info(f"跳过启动前的历史口令: {query}")
            return
        if ts > self._last_ts and query:
            self._last_ts = ts
            self.log.info(f"收到语音口令: {query}")
            await self.handle_command(query)

    # ---- 命令处理 ----
    async def handle_command(self, query):
        for kw, cmd in self.keywords:
            if kw in query:
                payload = query.split(kw, 1)[1].strip()
                self.log.info(f"命令 [{cmd}] 参数 [{payload}]")
                try:
                    if isinstance(cmd, str) and cmd.startswith("exec#"):
                        await self.exec_code(cmd.split("#", 1)[1], query)
                    else:
                        await self.dispatch(cmd, payload, query)
                except Exception as e:
                    self.log.exception(f"执行命令异常: {e}")
                    await self._handle_error(e)
                return

    async def exec_code(self, code, query=""):
        """执行自定义口令 exec# 后的代码（仅字面量参数的函数调用）"""
        name, args = parse_plugin_call(code)
        fn = PLUGIN_FUNCS.get(name)
        if fn is None:
            self.log.warning(f"exec# 未知函数: {name}")
            await self.tts(f"未知自定义口令: {name}")
            return
        ret = fn(self, args, query)
        if inspect.isawaitable(ret):
            ret = await ret
        self.log.info(f"exec# {name}{tuple(args)} 完成")
        return ret

    async def dispatch(self, cmd, payload, query=""):
        if cmd == "play":
            await self.cmd_play(payload)
        elif cmd == "next":
            await self.cmd_next()
        elif cmd == "prev":
            await self.cmd_prev()
        elif cmd.startswith("mode_"):
            await self.cmd_mode(cmd)
        elif cmd == "stop":
            await self.cmd_stop()
        elif cmd == "rescan":
            await self.cmd_rescan()
        elif cmd == "sleep_stop":
            await self.cmd_sleep_stop(query)
        elif cmd == "list_index":
            await self.cmd_list_index(payload)
        elif cmd == "play_list":
            await self.cmd_play_list(payload)
        elif cmd == "online":
            await self.tts("精简版不支持在线播放，请先把歌曲放进本地音乐目录")

    async def cmd_play(self, name, list_name=None):
        name = (name or "").strip()
        if not name:
            # 空参数（如口令"播放本地歌曲"）：播当前歌单下一首/第一首
            names = self.library.lists.get(self.cur_list) or []
            if not names:
                await self.tts("本地歌曲列表为空")
                return
            name = names[(names.index(self.cur_name) + 1) % len(names)] if self.cur_name in names else names[0]
        found = self.library.find(name, float(self.cfg.get("fuzzy_cutoff", 0.6)))
        if not found:
            await self.tts(f"本地不存在歌曲{name}")
            return
        self.cur_name = found
        # 切歌范围跟随所选歌单；歌曲不在当前歌单内时回退到「全部」
        if list_name and found in (self.library.lists.get(list_name) or []):
            self.cur_list = list_name
        elif found not in (self.library.lists.get(self.cur_list) or []):
            self.cur_list = "全部"
        rel = self.library.index[found]
        url = f"{self.base_url}/music/{urllib.parse.quote(rel)}"
        self.log.info(f"播放: {found} -> {url}")
        await self._send_play(url, found)

    async def _send_play(self, url, name):
        await self._cancel_timers()
        # 先强制打断音箱当前状态，再下发兼容模式播放
        try:
            await self.mina.player_pause(self.device_id)
            await self.mina.player_stop(self.device_id)
        except Exception as e:
            self.log.warning(f"预停止失败(可忽略): {e}")
        audio_id = str(self.cfg.get("use_music_audio_id") or DEFAULT_AUDIO_ID)
        if (self.hardware or "").upper() in NEED_USE_PLAY_MUSIC_API:
            ret = await self.mina.play_by_music_url(self.device_id, url, audio_id=audio_id)
        else:
            ret = await self.mina.play_by_url(self.device_id, url)
        self.log.info(f"播放指令返回: {ret}")
        self.playing = True
        if self.cfg.get("play_tts"):
            await self.tts(f"好的，播放{name}")
        sec = self.library.duration(name)
        if sec and sec > 0:
            self.log.info(f"歌曲时长 {sec:.0f}s，{sec + 0.5}s 后按模式处理")
            self._next_task = asyncio.ensure_future(self._auto_next(sec + 0.5))

    async def _auto_next(self, sec):
        try:
            await asyncio.sleep(sec)
        except asyncio.CancelledError:
            return
        if not self._running:
            return
        if self.play_type == PLAY_SIN:
            await self.cmd_stop()
        elif self.play_type == PLAY_ONE and self.cur_name:
            await self.cmd_play(self.cur_name)
        else:
            await self.cmd_next()

    async def cmd_next(self):
        names = self.library.lists.get(self.cur_list) or []
        if not names:
            await self.tts("歌单里没有歌曲")
            return
        if self.cur_name in names:
            idx = names.index(self.cur_name)
        else:
            idx = -1
        if self.play_type == PLAY_RND:
            pool = [n for n in names if n != self.cur_name]
            target = random.choice(pool) if pool else self.cur_name
        elif self.play_type == PLAY_SEQ:
            if idx + 1 >= len(names):
                await self.tts("已经是最后一首")
                return
            target = names[idx + 1]
        else:  # ALL / ONE
            target = names[(idx + 1) % len(names)]
        await self.cmd_play(target)

    async def cmd_prev(self):
        names = self.library.lists.get(self.cur_list) or []
        if not names:
            await self.tts("歌单里没有歌曲")
            return
        if self.cur_name in names:
            idx = names.index(self.cur_name)
        else:
            idx = 0
        if self.play_type == PLAY_RND:
            pool = [n for n in names if n != self.cur_name]
            target = random.choice(pool) if pool else self.cur_name
        elif idx == 0 and self.play_type == PLAY_SEQ:
            await self.tts("已经是第一首")
            return
        else:
            target = names[(idx - 1) % len(names)]
        await self.cmd_play(target)

    async def cmd_mode(self, cmd):
        mode = {
            "mode_one": PLAY_ONE, "mode_all": PLAY_ALL, "mode_rnd": PLAY_RND,
            "mode_sin": PLAY_SIN, "mode_seq": PLAY_SEQ,
        }[cmd]
        self.play_type = mode
        await self.tts(MODE_TTS[mode])

    async def cmd_volume(self, volume):
        try:
            vol = int(float(str(volume).strip()))
        except (TypeError, ValueError):
            await self.tts("请说音量多少")
            return
        vol = max(0, min(100, vol))
        try:
            await self.mina.player_set_volume(self.device_id, vol)
            self.volume = vol
            self.log.info(f"音量设置为 {vol}")
        except Exception as e:
            self.log.warning(f"设置音量失败: {e}")

    async def cmd_stop(self):
        await self._cancel_timers()
        if self.cfg.get("tts_enable", True):
            await self.tts(self.cfg.get("stop_tts_msg", "收到，再见"))
            await asyncio.sleep(2.5)
        await self._force_stop()

    async def _force_stop(self):
        self.playing = False
        try:
            await self.mina.player_pause(self.device_id)
            await self.mina.player_stop(self.device_id)
        except Exception as e:
            self.log.warning(f"停止播放异常: {e}")

    async def cmd_rescan(self):
        self.library.rescan()
        await self.tts(f"刷新完成，当前有{len(self.library.index)}首歌曲")

    async def cmd_sleep_stop(self, query):
        m = re.search(r"\d+", query)
        minutes = int(m.group()) if m else 0
        if minutes <= 0:
            await self.tts("请说几分钟后关机")
            return
        if self._sleep_task:
            self._sleep_task.cancel()
        await self.tts(f"收到，{minutes}分钟后关机")

        async def _do():
            try:
                await asyncio.sleep(minutes * 60)
            except asyncio.CancelledError:
                return
            await self.cmd_stop()
        self._sleep_task = asyncio.ensure_future(_do())

    async def cmd_list_index(self, payload):
        m = re.match(r"(\d+)\s*(.*)", payload)
        if not m:
            await self.tts("请说播放列表第几个")
            return
        idx = int(m.group(1)) - 1
        list_name = m.group(2).strip() or self.cur_list
        names = self.library.lists.get(list_name) or []
        if 0 <= idx < len(names):
            self.cur_list = list_name
            await self.cmd_play(names[idx])
        else:
            await self.tts("歌单里没有这么多歌曲")

    async def cmd_play_list(self, payload):
        if not payload:
            await self.tts("请说明要播放哪个歌单")
            return
        for name in self.library.lists:
            if payload in name:
                self.cur_list = name
                first = self.library.lists[name][0] if self.library.lists[name] else None
                if first:
                    await self.tts(f"好的，播放歌单{name}")
                    await self.cmd_play(first)
                return
        await self.tts(f"没有找到歌单{payload}")

    # ---- TTS（按型号选 miot action，OH2P=7-3 / L05B=5-3 ...）----
    async def tts(self, text):
        if not self.cfg.get("tts_enable", True) or not text:
            return
        cmd = TTS_COMMAND.get((self.hardware or "").upper(), f"{TTS_SIID}-{TTS_AIID}")
        try:
            text = text.replace(" ", "，")  # miot 参数不能含空格
            ret = await miio_command(self.miio, self.did, f"{cmd} {text}")
            self.log.info(f"TTS [{text}] 返回: {ret}")
        except Exception as e:
            self.log.warning(f"TTS 失败: {e}")

    # ---- 定时器清理 / 异常处理 ----
    async def _cancel_timers(self):
        for t in (self._next_task, self._sleep_task):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._next_task = self._sleep_task = None

    async def _handle_error(self, e):
        msg = str(e)
        self.log.warning(f"请求异常: {msg}")
        if any(h in msg for h in AUTH_ERR_HINTS):
            now = time.time()
            if now - self._reinit_cooldown > 60:
                self._reinit_cooldown = now
                self.log.info("检测到认证失效，60s 冷却后使用账号密码重新登录...")
                self._force_login = True
                try:
                    await self.init_services()
                except Exception as ex:
                    self.log.warning(f"重新登录失败: {ex}")

    async def run(self):
        try:
            await self.init_services()
        except Exception as e:
            self.log.error(f"初始化失败: {e}")
            self.log.error("请检查 config.json 的账号密码/网络，或运行 --selftest 自检")
            return
        await self.poll_loop()

    async def shutdown(self):
        self._running = False
        await self._cancel_timers()
        if self.session:
            try:
                await self.session.close()
            except Exception:
                pass


# ---------------------------------------------------------------- 自检
def selftest(cfg_path):
    print("========== xiaomusic-mini 自检 ==========")
    print(f"[0] 版本: {__version__}")
    print("[1] Python:", sys.version.split()[0])
    print("[2] 依赖导入: miservice-fork / aiohttp / mutagen / fake_useragent -> ", end="")
    from miservice import MiAccount, MiIOService, MiNAService, MiTokenStore, miio_command  # noqa: F401
    import fake_useragent  # noqa: F401
    print("OK")
    cfg = load_config(cfg_path)
    print(f"[3] 配置读取 OK (account={'*' * min(len(str(cfg.get('account'))), 4) or '空'}, mi_did={cfg.get('mi_did')})")
    lib = MusicLibrary(cfg.get("music_dir", "music"))
    print(f"[4] 音乐目录: {cfg.get('music_dir')} -> {len(lib.index)} 首 / {len(lib.lists)} 个歌单")
    if lib.index:
        sample = next(iter(lib.index))
        print(f"    示例: 匹配 '播放歌曲{sample}' -> {lib.find(sample)}; URL -> http://x/music/{urllib.parse.quote(lib.index[sample])}")
    print("[5] 默认网关:", detect_gateway() or "未检测到（将回退 192.168.1.1 或配置 http_host）")
    print("[6] 口令表:", ", ".join(kw for kw, _ in KEYWORDS))
    print("[7] OH2P 播放接口: play_by_music_url(audio_id={}) / TTS 7-3 已启用".format(cfg.get("use_music_audio_id") or DEFAULT_AUDIO_ID))
    print("==========================================")


# ---------------------------------------------------------------- 入口
def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="xiaomusic-mini 小爱音箱语音点播精简插件 (OH2P)")
    ap.add_argument("--config", default="/opt/xiaomusic-mini/config.json", help="配置文件路径")
    ap.add_argument("--selftest", action="store_true", help="本地自检（不联网）")
    ap.add_argument("--version", action="version", version=f"xiaomusic-mini {__version__}")
    args = ap.parse_args()

    if args.selftest:
        selftest(args.config)
        return

    cfg = load_config(args.config)
    log = setup_logging(cfg)
    log.info(f"xiaomusic-mini v{__version__} 启动, 配置: {args.config}")
    if not cfg.get("account") or not cfg.get("password"):
        log.error("config.json 未填写 account/password，先编辑配置文件")
        return

    # 插件目录：显式 plugin_dir > 配置目录/plugins（用户可写）> 程序自带 plugins
    plugin_dirs = []
    if cfg.get("plugin_dir"):
        plugin_dirs.append(cfg["plugin_dir"])
    else:
        plugin_dirs.append(os.path.join(os.path.dirname(os.path.abspath(args.config)), "plugins"))
    plugin_dirs.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins"))
    for pdir in plugin_dirs:
        if os.path.isdir(pdir):
            load_plugins(pdir, log)

    app = XiaomiMini(cfg, log)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    port = int(cfg.get("http_port", 8091))
    start_http_server(port, cfg.get("music_dir", "music"), ControlContext(app, loop))
    log.info(f"HTTP 音乐服务 + 控制台已启动: 端口 {port}, 控制台 http://<设备IP>:{port}/ , 目录 {cfg.get('music_dir')}")

    stopped = False

    def _stop(*_a):
        nonlocal stopped
        if not stopped:
            stopped = True
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(app.shutdown()))

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                pass
        loop.run_until_complete(app.run())
    except KeyboardInterrupt:
        pass
    finally:
        try:
            loop.run_until_complete(app.shutdown())
        except Exception:
            pass
        loop.close()
        logging.info("xiaomusic-mini 已退出")


if __name__ == "__main__":
    main()
