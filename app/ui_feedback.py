"""Small Flet-version compatibility helpers for transient UI feedback."""
from __future__ import annotations

import flet as ft


def page_is_alive(page: ft.Page) -> bool:
    """页面是否还有可用的会话。

    关窗会销毁 Flet 会话，此后任何界面操作（`update()`、`show_dialog()`、
    `scroll_to()`）都会抛 RuntimeError。关窗前就在跑的后台任务必须先问一句，
    否则收尾时只会甩出一堆“exception calling callback for <Future>”。
    """
    if page is None:
        return False
    try:
        page.session
    except AttributeError:  # 不是 Flet 页面（测试替身）就当它还在
        return True
    except RuntimeError:
        return False
    return True


def show_snack_bar(page: ft.Page, snack_bar: ft.SnackBar) -> bool:
    """Display a SnackBar using Flet 0.86's dialog lifecycle API."""
    if not page_is_alive(page):
        return False
    try:
        page.show_dialog(snack_bar)
    except RuntimeError:
        return False
    return True
