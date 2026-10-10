"""通用 UI 组件库（对齐 Dart 的 core/widgets/）。

本模块提供跨屏幕复用的展示组件，禁止 import app.services.*（红线 §7.5）。
所有组件通过回调槽与 Controller 交互，数据由外部注入。
"""
from __future__ import annotations

import flet as ft


class FailureBanner(ft.Row):
    """统一错误横幅（对齐 Dart 的 failure_banner.dart）。

    用法：
        banner = FailureBanner()
        # Controller 中
        def on_error(msg: str):
            banner.show(msg)

        def on_dismiss():
            banner.hide()
    """

    def __init__(self, visible: bool = False) -> None:
        super().__init__()
        self._text = ft.Text("", color=ft.Colors.RED_700, expand=True)
        self.visible = visible
        self.controls = [
            ft.Icon(ft.Icons.ERROR_OUTLINE, color=ft.Colors.RED_700),
            self._text,
            ft.IconButton(
                ft.Icons.CLOSE,
                tooltip="关闭",
                on_click=lambda e: self.hide(),
            ),
        ]

    def show(self, message: str) -> None:
        """显示错误消息。"""
        self._text.value = message
        self.visible = True
        if hasattr(self, "update"):
            self.update()

    def hide(self) -> None:
        """隐藏横幅。"""
        self.visible = False
        self._text.value = ""
        if hasattr(self, "update"):
            self.update()


class LoadingSpinner(ft.Container):
    """加载旋转器（对齐 Dart 的 CircularProgressIndicator）。

    用法：
        spinner = LoadingSpinner()
        spinner.show()  # 开始加载
        spinner.hide()  # 加载完成
    """

    def __init__(self, size: float = 40.0) -> None:
        super().__init__()
        self.width = size
        self.height = size
        self.alignment = ft.Alignment.CENTER
        self.content = ft.ProgressRing(width=size, height=size)
        self.visible = False

    def show(self) -> None:
        self.visible = True
        if hasattr(self, "update"):
            self.update()

    def hide(self) -> None:
        self.visible = False
        if hasattr(self, "update"):
            self.update()


class EmptyState(ft.Column):
    """空状态提示（对齐 Dart 的 _EmptyShelf）。

    用法：
        empty = EmptyState(
            icon=ft.Icons.BOOK_OUTLINED,
            title="书架为空",
            subtitle="点击下方导入按钮添加书籍",
        )
    """

    def __init__(
        self,
        icon: ft.Icons = ft.Icons.INBOX_OUTLINED,
        title: str = "暂无数据",
        subtitle: str = "",
    ) -> None:
        super().__init__(
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=8,
        )
        self.controls = [
            ft.Icon(icon, size=64, color=ft.Colors.GREY_400),
            ft.Text(title, size=18, color=ft.Colors.GREY_600),
        ]
        if subtitle:
            self.controls.append(
                ft.Text(subtitle, size=14, color=ft.Colors.GREY_500)
            )


class StatusChip(ft.Chip):
    """状态标签（对齐 Material Design Chip）。

    用法：
        chip = StatusChip(label="启用", color="green")
        chip = StatusChip(label="禁用", color="grey")
    """

    def __init__(self, label: str, color: str = "blue") -> None:
        color_map = {
            "green": ft.Colors.GREEN_100,
            "red": ft.Colors.RED_100,
            "blue": ft.Colors.BLUE_100,
            "grey": ft.Colors.GREY_200,
            "orange": ft.Colors.ORANGE_100,
        }
        text_color_map = {
            "green": ft.Colors.GREEN_900,
            "red": ft.Colors.RED_900,
            "blue": ft.Colors.BLUE_900,
            "grey": ft.Colors.GREY_800,
            "orange": ft.Colors.ORANGE_900,
        }
        super().__init__(
            label=ft.Text(label, size=12, color=text_color_map.get(color, ft.Colors.GREY_800)),
            bgcolor=color_map.get(color, ft.Colors.GREY_200),
        )
