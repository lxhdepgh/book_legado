"""What the platform currently hosting the Flet page is able to do.

Several components need the same answer — "does this target own a desktop
window", "can ``flet_webview`` embed a page in it" — and each used to spell out
its own ``PagePlatform`` set.  Keeping the sets here means a new target is
classified once instead of in every caller.
"""
from __future__ import annotations

import flet as ft


_DESKTOP_PAGES = frozenset({
    ft.PagePlatform.WINDOWS, ft.PagePlatform.MACOS, ft.PagePlatform.LINUX,
})

# flet_webview supports iOS/Android/macOS/Web only; on a desktop Windows build
# the package exposes no usable control.
_IN_APP_WEBVIEW_PAGES = frozenset({
    ft.PagePlatform.ANDROID, ft.PagePlatform.ANDROID_TV, ft.PagePlatform.IOS,
    ft.PagePlatform.MACOS,
})


def attached_page(control) -> ft.Page | None:
    """A control's page, or None while the control is not on a page yet."""
    try:
        return control.page
    except (AssertionError, AttributeError, RuntimeError):
        return None


def page_platform(page) -> ft.PagePlatform | None:
    """The page's platform, or None when there is no usable page."""
    try:
        return page.platform
    except (AssertionError, AttributeError, RuntimeError):
        return None


def is_desktop_page(page) -> bool:
    """True when the platform owns a real desktop window."""
    return page_platform(page) in _DESKTOP_PAGES


def supports_in_app_webview(page) -> bool:
    """True when ``flet_webview`` can render a page inside the app window."""
    return page_platform(page) in _IN_APP_WEBVIEW_PAGES
