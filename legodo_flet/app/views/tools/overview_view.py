"""ToolsOverviewView：更多工具入口页（M7 后导航精简：工具组折叠为单一入口）。

工具域十个页面（浏览器/词典/替换规则/字体/二维码/导出/备份/更新/网络桥/朗读）不在
侧栏直接展示，统一收进本页 10 张卡片；点击经 ``on_open_destination`` 回调槽跳转
（由 NavigationHost.show 在装配时绑定——视图不 import services，也不持有导航）。

本视图只做 Flet 布局；无服务依赖（纯入口页）。
"""
from __future__ import annotations

import flet as ft

from app.core.registry import (DEST_TOOLS_BROWSER, DEST_TOOLS_DICT,
                               DEST_TOOLS_EXPORT, DEST_TOOLS_FONT,
                               DEST_TOOLS_QR, DEST_TOOLS_REPLACE,
                               DEST_TOOLS_UPDATE, DEST_TOOLS_WEBDAV,
                               DEST_TOOLS_WEB_BRIDGE)

#: 工具卡片清单：(目的地, 标题, 图标, 说明)。
_TOOLS: tuple[tuple[str, str, str, str], ...] = (
    (DEST_TOOLS_BROWSER, "浏览器", ft.Icons.PUBLIC, "通用内置浏览器（交互 / 无头 / 渲染）"),
    (DEST_TOOLS_DICT, "词典", ft.Icons.LANGUAGE, "划词取词，查词结果弹窗"),
    (DEST_TOOLS_REPLACE, "替换规则", ft.Icons.FIND_REPLACE, "全局正文替换规则管理"),
    (DEST_TOOLS_FONT, "字体", ft.Icons.TEXT_FIELDS, "系统字体枚举 / 用户字体"),
    (DEST_TOOLS_QR, "二维码", ft.Icons.QR_CODE_2, "二维码生成 / 识别导入"),
    (DEST_TOOLS_EXPORT, "导出", ft.Icons.DOWNLOAD_FOR_OFFLINE, "书籍导出 epub / txt"),
    (DEST_TOOLS_WEBDAV, "备份", ft.Icons.UPLOAD_FILE, "WebDAV 备份 / 恢复"),
    (DEST_TOOLS_UPDATE, "更新", ft.Icons.SYSTEM_UPDATE, "检查更新 / 下载安装包"),
    (DEST_TOOLS_WEB_BRIDGE, "网络桥", ft.Icons.LAN, "本机 HTTP/WebSocket 书源管理 / 调试"),
)


class ToolsOverviewView(ft.Column):
    """更多工具：10 张工具卡片入口。"""

    def __init__(self) -> None:
        super().__init__(expand=True, scroll=ft.ScrollMode.AUTO, spacing=0)

        #: 入口跳转回调槽（NavigationHost 装配时绑定 self.show）。
        self.on_open_destination = None  # (destination: str) -> None

        cards = [
            ft.Container(
                width=280, padding=16, border_radius=12,
                bgcolor=ft.Colors.GREY_200,
                on_click=lambda e, d=dest: self._fire(self.on_open_destination, d),
                content=ft.Column([
                    ft.Icon(icon, size=26),
                    ft.Text(title, size=15, weight=ft.FontWeight.W_600),
                    ft.Text(desc, size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                ], spacing=6),
            )
            for dest, title, icon, desc in _TOOLS
        ]

        self.controls = [
            ft.Container(padding=24, content=ft.Column([
                ft.Text("更多工具", size=28, weight=ft.FontWeight.BOLD),
                ft.Text("浏览器、词典、替换规则等工具集中在这里，点卡片进入。"),
                ft.Divider(),
                ft.Row(wrap=True, spacing=12, run_spacing=12,
                       controls=cards),
            ])),
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)
