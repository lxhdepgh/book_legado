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


def main():
    resource_dir = _ROOT / "resource"
    app = BookSourceApp(resource_dir)
    try:
        ft.run(app.build, view=ft.AppView.FLET_APP)
    except Exception as e:
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
