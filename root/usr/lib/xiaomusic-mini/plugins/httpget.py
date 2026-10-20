"""发起 GET 请求并记日志（对应完整版 plugins/httpget.py，改用 aiohttp 异步）。

口令配置示例： "测试链接": "exec#httpget(\"https://github.com/hanxi/xiaomusic\")"
"""

import aiohttp


async def httpget(app, args, query):
    url = args[0] if args else ""
    if not url:
        app.log.warning("httpget 缺少 url 参数")
        return None
    timeout = aiohttp.ClientTimeout(total=5)
    async with app.session.get(url, timeout=timeout) as r:
        text = await r.text()
        app.log.info(f"httpget url:{url} status:{r.status} len:{len(text)}")
        if r.status >= 400:
            await app.tts(f"请求失败，状态码 {r.status}")
            return None
        await app.tts(f"请求成功，返回 {len(text)} 字节")
        return text
