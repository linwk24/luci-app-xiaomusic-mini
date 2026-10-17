"""调试口令插件（对应完整版 plugins/code1.py）。

口令配置示例： "测试自定义口令": "exec#code1(\"hello\")"

函数签名说明见 main.py 的 load_plugins。
"""


async def code1(app, args, query):
    arg1 = args[0] if args else ""
    app.log.info(f"code1:{arg1}")
    await app.tts("你好，我是自定义的测试口令")
    await app.tts(f"你说的是: {query}")
    return arg1
