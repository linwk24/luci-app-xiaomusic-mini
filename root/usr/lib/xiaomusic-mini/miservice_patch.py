# -*- coding: utf-8 -*-
"""miservice 兼容补丁

miservice 2.9.3 的 `MiAccount.login()` 有两个问题：

1. 用 passToken 续期时，小米 `serviceLogin` 的响应里**不包含** passToken 字段，
   原代码执行 `self.token["passToken"] = resp["passToken"]` 会抛 KeyError；
2. 登录失败时原代码会调用 `token_store.save_token()`（无参数）**删除 token 文件**，
   导致 passToken 丢失，只能重新扫码登录。

本补丁替换 `MiAccount.login`：
  - 响应里没有 passToken 时保留现有值；
  - 登录失败不删除 token 文件（只记录日志）；
  - 其余流程（auth2、securityTokenService、保存 token）与原版一致。
"""

import hashlib
import logging
import random
import string

_LOGGER = logging.getLogger("miservice")


def _random_id(length):
    return "".join(random.sample(string.ascii_letters + string.digits, length))


async def patched_login(self, sid):
    if not self.token:
        self.token = {"deviceId": _random_id(16)}

    try:
        resp = await self._serviceLogin(f"serviceLogin?sid={sid}&_json=true")
        if resp.get("code") != 0:
            data = {
                "_json": "true",
                "qs": resp.get("qs", ""),
                "sid": resp.get("sid", ""),
                "_sign": resp.get("_sign", ""),
                "callback": resp.get("callback", ""),
                "user": self.username,
                "hash": hashlib.md5((self.password or "").encode()).hexdigest().upper(),
            }
            resp = await self._serviceLogin("serviceLoginAuth2", data)
            if resp.get("code") != 0:
                desc = resp.get("desc") or resp.get("description") or resp.get("code")
                raise RuntimeError(f"小米登录失败: {desc}")

        if not resp.get("userId"):
            raise RuntimeError("小米登录响应缺少 userId（账号可能触发了二次验证）")

        self.token["userId"] = str(resp["userId"])

        # 关键修复：passToken 续期时响应可能不带 passToken，保留原来的值
        if resp.get("passToken"):
            self.token["passToken"] = resp["passToken"]
        if not self.token.get("passToken"):
            raise RuntimeError("登录响应没有 passToken，且本地也没有可复用的 passToken")

        missing = [k for k in ("location", "nonce", "ssecurity") if not resp.get(k)]
        if missing:
            raise RuntimeError("登录响应缺少字段: " + ",".join(missing))

        service_token = await self._securityTokenService(
            resp["location"], resp["nonce"], resp["ssecurity"]
        )
        self.token[sid] = (resp["ssecurity"], service_token)

        if self.token_store:
            self.token_store.save_token(self.token)
        return True

    except Exception as e:
        # 不删除 token 文件：保留 passToken，避免用户被迫重新扫码
        _LOGGER.warning("登录失败（保留现有 token）: %s", e)
        return False


def install():
    """全局替换 MiAccount.login；重复调用安全。"""
    try:
        from miservice import MiAccount
    except Exception:
        return False

    if getattr(getattr(MiAccount, "login", None), "_xiaomusic_patched", False):
        return True

    patched_login._xiaomusic_patched = True
    MiAccount.login = patched_login
    return True
