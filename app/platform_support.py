"""What the platform currently hosting the app is able to do.

Several components need the same answer — "does this target own a desktop
window", "can a WebView embed a page in it" — and each used to spell out
its own ``PagePlatform`` set.  Keeping the sets here means a new target is
classified once instead of in every caller.

This module must **not** import flet at module level: it runs in --serve
(headless) mode where no UI framework is present.  Platform classification
uses string comparison against the canonical names from ft.PagePlatform.
"""
from __future__ import annotations


# Canonical platform names (matching ft.PagePlatform enum values).
_DESKTOP_PAGES = frozenset({"WINDOWS", "MACOS", "LINUX"})

# In-app WebView support: Android/iOS/macOS only.
# Note: flet_webview has no usable control on desktop Windows.
_IN_APP_WEBVIEW_PAGES = frozenset({"ANDROID", "ANDROID_TV", "IOS", "MACOS"})


def _page_platform_name(page) -> str | None:
    """Extract the platform name as a string, without importing flet."""
    try:
        plat = page.platform
        # If it's already an enum, get its value; if it's a string, use directly.
        return getattr(plat, "value", str(plat))
    except (AssertionError, AttributeError, RuntimeError):
        return None


def attached_page(control) -> object | None:
    """A control's page, or None while the control is not on a page yet."""
    try:
        return control.page
    except (AssertionError, AttributeError, RuntimeError):
        return None


def page_platform(page) -> str | None:
    """The page's platform name as a string, or None when there is no usable page."""
    return _page_platform_name(page)


def is_desktop_page(page) -> bool:
    """True when the platform owns a real desktop window."""
    return _page_platform_name(page) in _DESKTOP_PAGES


def supports_in_app_webview(page) -> bool:
    """True when an in-app WebView can render a page inside the app window."""
    return _page_platform_name(page) in _IN_APP_WEBVIEW_PAGES
