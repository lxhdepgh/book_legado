#!/usr/bin/env python3
"""
Legado 书源管理工具 - Flet GUI 入口

启动方式:
    python main.py
"""
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import flet as ft
from app.app import BookSourceApp


def smoke_test():
    """Exercise dynamically imported rule-engine dependencies in a frozen app."""
    import io
    from types import SimpleNamespace

    from Crypto.Cipher import AES  # noqa: F401
    from PIL import Image
    from fontTools.ttLib import TTFont  # noqa: F401
    from opencc import OpenCC
    import elementpath  # noqa: F401
    import jsonpath_ng.ext  # noqa: F401
    import quickjs  # noqa: F401

    from app.services.legado_js import JsContext, LegadoJsRuntime

    source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://smoke"})
    runtime = LegadoJsRuntime()
    assert runtime.evaluate("@js:java.md5Encode('ok').length", JsContext(source)) == 32
    assert OpenCC("t2s").convert("閱讀") == "阅读"
    image = Image.new("RGB", (2, 2), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    assert runtime._bitmap_info(runtime._bitmap_decode(
        __import__("json").dumps(list(output.getvalue())))) == "[2, 2]"


def main():
    if "--smoke-test" in sys.argv:
        smoke_test()
        return
    resource_dir = _ROOT / "resource"
    app = BookSourceApp(resource_dir)
    try:
        ft.run(app.build, view=ft.AppView.FLET_APP)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
