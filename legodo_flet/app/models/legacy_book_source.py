"""Migration of pre-3.0 Legado book-source JSON into the current schema."""
from __future__ import annotations

import json
import re
from typing import Any

_HEADER_RE = re.compile(r"@Header:\{.+?\}", re.I)
_JS_RE = re.compile(r"\{\{.+?\}\}", re.I)


def to_new_rule(old_rule: str | None) -> str | None:
    if old_rule is None or not str(old_rule).strip():
        return None
    old_rule = str(old_rule)
    new_rule = old_rule
    reverse = new_rule.startswith("-")
    if reverse:
        new_rule = new_rule[1:]
    all_in_one = new_rule.startswith("+")
    if all_in_one:
        new_rule = new_rule[1:]
    lower = new_rule.lower()
    if not any((lower.startswith("@css:"), lower.startswith("@xpath:"),
                new_rule.startswith("//"), new_rule.startswith("##"),
                new_rule.startswith(":"), "@js:" in lower, "<js>" in lower)):
        if "#" in new_rule and "##" not in new_rule:
            new_rule = old_rule.replace("#", "##")
        if "|" in new_rule and "||" not in new_rule:
            if "##" in new_rule:
                parts = new_rule.split("##")
                if "|" in parts[0]:
                    parts[0] = parts[0].replace("|", "||")
                    new_rule = "##".join(parts)
            else:
                new_rule = new_rule.replace("|", "||")
        if ("&" in new_rule and "&&" not in new_rule
                and "http" not in new_rule and not new_rule.startswith("/")):
            new_rule = new_rule.replace("&", "&&")
    if all_in_one:
        new_rule = "+" + new_rule
    if reverse:
        new_rule = "-" + new_rule
    return new_rule


def to_new_url(old_url: str | None) -> str | None:
    if old_url is None or not str(old_url).strip():
        return None
    url = str(old_url)
    if url.lower().startswith("<js>"):
        return url.replace("=searchKey", "={{key}}").replace("=searchPage", "={{page}}")
    options: dict[str, str] = {}
    match = _HEADER_RE.search(url)
    if match:
        header = match.group(0)
        url = url.replace(header, "")
        options["headers"] = header[8:]
    parts = url.split("|")
    url = parts[0]
    if len(parts) > 1:
        options["charset"] = parts[1].split("=")[1]
    scripts: list[str] = []
    while (match := _JS_RE.search(url)) is not None:
        scripts.append(match.group(0))
        url = url.replace(scripts[-1], f"${len(scripts) - 1}")
    url = url.replace("{", "<").replace("}", ">").replace("searchKey", "{{key}}")
    url = re.sub(r"<searchPage([-+]1)>", r"{{page\1}}", url)
    url = re.sub(r"searchPage([-+]1)", r"{{page\1}}", url).replace("searchPage", "{{page}}")
    for index, script in enumerate(scripts):
        url = url.replace(f"${index}", script.replace("searchKey", "key").replace("searchPage", "page"))
    parts = url.split("@")
    url = parts[0]
    if len(parts) > 1:
        options.update(method="POST", body=parts[1])
    if options:
        url += "," + json.dumps(options, ensure_ascii=False, separators=(",", ":"))
    return url


def to_new_urls(old_urls: str | None) -> str | None:
    if old_urls is None or not str(old_urls).strip():
        return None
    old_urls = str(old_urls)
    if old_urls.startswith("@js:") or old_urls.startswith("<js>"):
        return old_urls
    if "\n" not in old_urls and "&&" not in old_urls:
        return to_new_url(old_urls)
    return "\n".join(re.sub(r"\n\s*", "", to_new_url(item) or "")
                     for item in re.split(r"(?:&&|\r?\n)+", old_urls))


def normalize_old_book_source(data: dict[str, Any]) -> dict[str, Any]:
    if not data.get("bookSourceUrl"):
        raise ValueError("old book source is missing bookSourceUrl")
    rule = lambda key: to_new_rule(data.get(key))
    content = rule("ruleBookContent") or ""
    if content.startswith("$") and not content.startswith("$."):
        content = content[1:]
    explore_url = to_new_urls(data.get("ruleFindUrl"))
    ua = data.get("httpUserAgent")
    return {
        "bookSourceUrl": data["bookSourceUrl"], "bookSourceName": data.get("bookSourceName") or "",
        "bookSourceGroup": data.get("bookSourceGroup"), "loginUrl": data.get("loginUrl"),
        "loginUi": data.get("loginUi"), "loginCheckJs": data.get("loginCheckJs"),
        "coverDecodeJs": data.get("coverDecodeJs"), "bookSourceComment": data.get("bookSourceComment") or "",
        "bookUrlPattern": data.get("ruleBookUrlPattern"), "customOrder": data.get("serialNumber") or 0,
        "header": json.dumps({"User-Agent": ua}, ensure_ascii=False, separators=(",", ":")) if ua else None,
        "searchUrl": to_new_url(data.get("ruleSearchUrl")), "exploreUrl": explore_url,
        "bookSourceType": 1 if data.get("bookSourceType") == "AUDIO" else 0,
        "enabled": data.get("enable") if data.get("enable") is not None else True,
        "enabledExplore": bool(explore_url),
        "ruleSearch": _list_rule(data, rule, "ruleSearch"),
        "ruleExplore": _list_rule(data, rule, "ruleFind"),
        "ruleBookInfo": {"init": rule("ruleBookInfoInit"), "name": rule("ruleBookName"),
            "author": rule("ruleBookAuthor"), "intro": rule("ruleIntroduce"), "kind": rule("ruleBookKind"),
            "coverUrl": rule("ruleCoverUrl"), "lastChapter": rule("ruleBookLastChapter"), "tocUrl": rule("ruleChapterUrl")},
        "ruleToc": {"chapterList": rule("ruleChapterList"), "chapterName": rule("ruleChapterName"),
            "chapterUrl": rule("ruleContentUrl"), "nextTocUrl": rule("ruleChapterUrlNext")},
        "ruleContent": {"content": content, "replaceRegex": rule("ruleBookContentReplace"),
            "nextContentUrl": rule("ruleContentUrlNext")},
    }


def _list_rule(data: dict[str, Any], rule, prefix: str) -> dict[str, Any]:
    return {"bookList": rule(prefix + "List"), "name": rule(prefix + "Name"),
            "author": rule(prefix + "Author"), "intro": rule(prefix + "Introduce"),
            "kind": rule(prefix + "Kind"), "bookUrl": rule(prefix + "NoteUrl"),
            "coverUrl": rule(prefix + "CoverUrl"), "lastChapter": rule(prefix + "LastChapter")}
