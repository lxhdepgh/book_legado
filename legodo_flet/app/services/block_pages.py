"""Interception pages must not be mistaken for empty rule results.

Two different layers answer a request with something that is not content: local
security software replaces the body with its own notice page, and remote sites
hide behind WAF/anti-bot interstitials (Cloudflare, Imperva, 阿里云盾...).
Parsing either one yields an empty book list, which is indistinguishable from a
broken rule set, so a blocked source used to look "search only usable" with
every field blank.  Both cases are recognized here and reported as interception.
"""
from __future__ import annotations

import re

# Local security software answers 200 with its notice page, so these markers are
# matched on every status.
_LOCAL_MARKERS = (
    "火绒", "安全响应中心", "腾讯电脑管家", "360安全卫士", "360手机卫士",
    "smartscreen", "访问被拦截", "该网站已被拦截", "已阻止你访问该网站",
    "危险网站", "恶意网站", "钓鱼网站", "拦截提示",
)

# Remote interstitials are only meaningful on an error status: a novel page that
# happens to contain one of the phrases must not be misread as interception.
_REMOTE_MARKERS = (
    ("attention required! | cloudflare", "Cloudflare"),
    ("you have been blocked", "Cloudflare"),
    ("cf-error-details", "Cloudflare"),
    ("error 1020", "Cloudflare"),
    ("checking your browser before accessing", "Cloudflare 质询页"),
    ("just a moment...", "Cloudflare 质询页"),
    ("enable javascript and cookies to continue", "Cloudflare 质询页"),
    ("incapsula incident id", "Imperva"),
    ("sucuri website firewall", "Sucuri"),
    ("errors.aliyun.com", "阿里云盾"),
    ("yunjiasu", "百度云加速"),
    ("safedog", "安全狗"),
    ("网站防火墙", "站点防火墙"),
)

# Real content is never this small *and* an interception page; the cap keeps a
# long page that quotes a marker from being misread.
_MAX_BODY = 20000


class SiteBlockedError(RuntimeError):
    """Raised when a rule stage fetched an interception page instead of content."""


def block_reason(text, status=None) -> str | None:
    """Return a Chinese explanation when the body is an interception page."""
    body = str(text or "")
    if not body or len(body) > _MAX_BODY:
        return None
    lowered = body.casefold()
    marker = next((item for item in _LOCAL_MARKERS if item in lowered), None)
    if marker:
        return f"本机安全软件已拦截该站点（{marker}）"
    try:
        code = int(status) if status is not None else 0
    except (TypeError, ValueError):
        code = 0
    if code and code < 400:
        return None
    hit = next(((phrase, label) for phrase, label in _REMOTE_MARKERS
                if phrase in lowered), None)
    if hit is None:
        return None
    suffix = f"，HTTP {code}" if code else ""
    return f"目标站点拦截了本次访问（{hit[1]}{suffix}）"


def looks_like_gate(html: str) -> bool:
    """True when a rendered page is still a wall instead of the article.

    A login form is the one gate a page keeps after rendering: the challenge
    interstitials are already recognised by the shared block-page detector.
    Living here (not in the WebView renderer) is what lets the UI-free browser
    path ask the same question without importing flet.
    """
    body = str(html or "")
    if not body.strip():
        return True
    if block_reason(body):
        return True
    return bool(re.search(r"""(?i)<input[^>]+type\s*=\s*["']?password""", body))
