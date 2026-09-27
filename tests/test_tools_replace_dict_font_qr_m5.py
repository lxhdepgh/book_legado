# -*- coding: utf-8 -*-
"""M5 工具域测试：替换规则 / 词典 / 字体 / 二维码（契约 §15）。

覆盖（≥12 条，全程不联网）：
- ReplaceRuleService：CRUD / set_enabled / ReplaceRulesChangedEvent 发布；
- 正文应用两级叠加：书源内嵌规则 ∪ 用户全局规则（书源内嵌先、全局后），
  且未注入 provider 时书源内嵌行为零回归；
- DictService：CRUD + lookup（注入 fetcher stub，复用 legado 规则解析）；
- FontService：系统字体扫描（构造临时字体目录 + 真实 ttf）/ 用户字体添加删除 /
  选中字体入 SettingsService；
- QrCodeService：generate→decode 往返（生成 PNG 再用 opencv 解码回原文）。

数据目录经 LEGADO_BOOKSOURCE_DATA_DIR/CACHE_DIR 重定向到 tmp_path（app_paths 支持）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from app.core.bus import EventBus
from app.core.engine_worker import engine_hub
from app.core.events import ReplaceRulesChangedEvent
from app.core.storage import StorageService
from app.services.engine.reading_engine import (
    chapter_display_title,
    process_book_content,
    set_global_replace_rules_provider,
)
from app.services.engine.rule_engine import BookItem, ChapterItem
from app.services.tools.dict_service import DictService
from app.services.tools.font_service import FontService
from app.services.tools.qr_code_service import QrCodeService
from app.services.tools.replace_rule_service import ReplaceRuleService


# --------------------------------------------------------------------- #
# 公共：tmp 存储 + 事件总线
# --------------------------------------------------------------------- #
@pytest.fixture()
def tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGADO_BOOKSOURCE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_CACHE_DIR", str(tmp_path / "cache"))
    storage = StorageService()
    asyncio.run(storage.start())
    try:
        yield storage
    finally:
        asyncio.run(storage.stop())


@pytest.fixture()
def bus():
    engine = engine_hub()
    return EventBus(engine=engine)


def _run(coro):
    return asyncio.run(coro)


def _drain(futures) -> None:
    for f in futures or []:
        f.result(timeout=5)


# --------------------------------------------------------------------- #
# 1. ReplaceRuleService CRUD + 事件
# --------------------------------------------------------------------- #
def test_replace_rule_crud(tmp_storage, bus):
    svc = ReplaceRuleService(storage=tmp_storage)
    svc.bus = bus
    _run(svc.on_start())
    try:
        rid = _run(svc.save_rule({
            "name": "去广告", "pattern": "[广告]", "replacement": "",
            "scope": "content", "is_regex": False, "enabled": True}))
        assert rid > 0
        rules = _run(svc.list_rules())
        assert len(rules) == 1
        assert rules[0]["pattern"] == "[广告]"
        assert rules[0]["scope"] == "content"
        assert rules[0]["enabled"] is True

        # 更新
        _run(svc.save_rule({"id": rid, "name": "去广告v2",
                            "pattern": "[广告]", "replacement": "",
                            "scope": "title", "is_regex": True,
                            "enabled": False}))
        rules = _run(svc.list_rules())
        assert rules[0]["name"] == "去广告v2"
        assert rules[0]["scope"] == "title"
        assert rules[0]["enabled"] is False

        # set_enabled
        assert _run(svc.set_enabled(rid, True)) is True
        rules = _run(svc.list_rules())
        assert rules[0]["enabled"] is True

        # delete
        assert _run(svc.delete_rule(rid)) is True
        assert _run(svc.list_rules()) == []
        assert _run(svc.delete_rule(rid)) is False
    finally:
        _run(svc.on_stop())


def test_replace_rule_publishes_event(tmp_storage, bus):
    received: list[ReplaceRulesChangedEvent] = []
    engine = engine_hub()
    pending: list = []
    orig_publish = bus.publish

    def spy(ev):
        futs = orig_publish(ev)
        pending.extend(futs)
        return futs

    bus.publish = spy  # type: ignore[method-assign]

    async def collector(ev):
        received.append(ev)

    bus.subscribe(ReplaceRulesChangedEvent, collector)

    svc = ReplaceRuleService(storage=tmp_storage)
    svc.bus = bus
    _run(svc.on_start())
    try:
        rid = _run(svc.save_rule({"pattern": "x", "replacement": "y"}))
        _run(svc.set_enabled(rid, False))
        _run(svc.delete_rule(rid))
    finally:
        _run(svc.on_stop())
    _drain(pending)
    ids = [e.ids[0] for e in received if e.ids]
    assert rid in ids
    assert len(received) == 3  # save + set_enabled + delete 各一条


# --------------------------------------------------------------------- #
# 2. 正文应用两级叠加（书源内嵌先、全局后；未注入零回归）
# --------------------------------------------------------------------- #
def _make_book_chapter():
    chapter = ChapterItem("第一章", "https://source/ch/1")
    book = BookItem("书", read_config={
        "useReplaceRule": True,
        "contentReplaceRules": [
            {"pattern": "[广告]", "replacement": "",
             "isRegex": False, "isEnabled": True}],
    })
    return book, chapter


def test_content_book_source_no_regression_without_provider(tmp_storage):
    """未注入全局 provider 时，书源内嵌规则行为与重构前逐字节一致（守门）。"""
    set_global_replace_rules_provider(None)
    book, chapter = _make_book_chapter()
    content, title = process_book_content(
        "第一章\n[广告] 正文", book, chapter)
    assert title == "第一章"
    assert content == "正文"  # 书源内嵌规则去掉了 [广告]


def test_content_two_level_stacking_order(tmp_storage, bus):
    """书源内嵌先于全局：内嵌先删 [广告]，全局再处理时已无'广告'二字。"""
    svc = ReplaceRuleService(storage=tmp_storage)
    svc.bus = bus
    _run(svc.on_start())  # 注入 provider
    try:
        # 全局规则：把"广告"替换为 SPAM。
        _run(svc.save_rule({
            "name": "全局广告", "pattern": "广告", "replacement": "SPAM",
            "scope": "content", "is_regex": False, "enabled": True}))
        book, chapter = _make_book_chapter()
        content, title = process_book_content(
            "第一章\n[广告] 正文", book, chapter)
        # 书源内嵌先删掉 [广告] 整体 → 全局"广告"已无命中 → 不出现 SPAM。
        assert title == "第一章"
        assert content == "正文"
        assert "SPAM" not in content
    finally:
        _run(svc.on_stop())  # 注销 provider


def test_content_global_rule_actually_applies(tmp_storage, bus):
    """有全局规则时，对书源未处理到的内容生效（两级叠加确实叠加）。"""
    svc = ReplaceRuleService(storage=tmp_storage)
    svc.bus = bus
    _run(svc.on_start())
    try:
        _run(svc.save_rule({
            "pattern": "TODO", "replacement": "DONE",
            "scope": "content", "is_regex": False, "enabled": True}))
        # 书源内嵌规则不动 TODO；全局规则把 TODO 改成 DONE。
        book = BookItem("书", read_config={
            "useReplaceRule": True,
            "contentReplaceRules": [
                {"pattern": "[广告]", "replacement": "",
                 "isRegex": False, "isEnabled": True}],
        })
        chapter = ChapterItem("第一章", "https://source/ch/1")
        content, _ = process_book_content(
            "第一章\n[广告] 正文TODO", book, chapter)
        assert content == "正文DONE"
    finally:
        _run(svc.on_stop())


def test_title_global_rule_scope(tmp_storage, bus):
    """scope=title 的全局规则作用于章节标题，不作用于正文。"""
    svc = ReplaceRuleService(storage=tmp_storage)
    svc.bus = bus
    _run(svc.on_start())
    try:
        _run(svc.save_rule({
            "pattern": "草稿", "replacement": "终稿",
            "scope": "title", "is_regex": False, "enabled": True}))
        chapter = ChapterItem("草稿 第一章", "https://source/ch/1")
        book = BookItem("书", read_config={"useReplaceRule": True})
        title = chapter_display_title(chapter, [], True)
        assert title == "终稿 第一章"
        # 正文不受 title 作用域规则影响
        content, _ = process_book_content("草稿 第一章\n草稿正文", book, chapter)
        assert content == "草稿正文"
    finally:
        _run(svc.on_stop())


def test_global_toggle_off_via_settings(tmp_storage, bus):
    """SettingsService 键 replace.global.enabled=False 时全局规则不叠加。"""
    from app.core.settings import SettingsService
    settings = SettingsService(storage=tmp_storage, engine=engine_hub())
    _run(settings.on_start())
    settings.set("replace.global.enabled", False)

    svc = ReplaceRuleService(storage=tmp_storage, settings=settings)
    svc.bus = bus
    _run(svc.on_start())
    try:
        _run(svc.save_rule({
            "pattern": "TODO", "replacement": "DONE",
            "scope": "content", "is_regex": False, "enabled": True}))
        # 开关关闭 → provider 返回空 → 全局规则不生效
        book, chapter = _make_book_chapter()
        content, _ = process_book_content("第一章\n正文TODO", book, chapter)
        assert content == "正文TODO"
    finally:
        _run(svc.on_stop())
        _run(settings.on_stop())


# --------------------------------------------------------------------- #
# 3. DictService CRUD + lookup（stub fetcher，不联网）
# --------------------------------------------------------------------- #
def test_dict_rule_crud(tmp_storage):
    svc = DictService(storage=tmp_storage)
    _run(svc.on_start())
    try:
        rid = _run(svc.save_rule({
            "name": "简明词典", "url": "https://dict.test/q?w={{key}}",
            "rules": {"ruleContent": ".def"}, "enabled": True}))
        assert rid > 0
        rules = _run(svc.list_rules())
        assert len(rules) == 1
        assert rules[0]["name"] == "简明词典"
        assert rules[0]["rules"] == {"ruleContent": ".def"}
        assert _run(svc.delete_rule(rid)) is True
        assert _run(svc.list_rules()) == []
    finally:
        _run(svc.on_stop())


def test_dict_lookup_with_stub_fetcher(tmp_storage):
    html_page = (
        "<html><body><h1>hello</h1>"
        '<div class="def">释义：你好，用于问候。</div></body></html>'
    )

    async def fake_fetch(url: str) -> str:
        # 校验 URL 模板填词生效
        assert "hello" in url
        return html_page

    svc = DictService(storage=tmp_storage, fetcher=fake_fetch)
    _run(svc.on_start())
    try:
        _run(svc.save_rule({
            "name": "词典", "url": "https://dict.test/q?w={{key}}",
            "rules": {"ruleContent": ".def@text"}, "enabled": True}))
        result = _run(svc.lookup("hello"))
        assert "你好" in result
        assert "释义" in result
    finally:
        _run(svc.on_stop())


def test_dict_lookup_no_rule_returns_empty(tmp_storage):
    svc = DictService(storage=tmp_storage)
    _run(svc.on_start())
    try:
        assert _run(svc.lookup("word")) == ""
        assert _run(svc.lookup("")) == ""
    finally:
        _run(svc.on_stop())


# --------------------------------------------------------------------- #
# 4. FontService（构造临时字体目录；真实 ttf 缺失则跳过）
# --------------------------------------------------------------------- #
def _find_real_font() -> Path | None:
    candidates = []
    if sys.platform.startswith("win"):
        candidates += [
            Path(r"C:\Windows\Fonts\arial.ttf"),
            Path(r"C:\Windows\Fonts\segoeui.ttf"),
        ]
    candidates += list(Path("/usr/share/fonts").glob("**/*.ttf"))
    candidates += list((Path.home() / ".fonts").glob("*.ttf"))
    for c in candidates:
        if c.is_file():
            return c
    if Path(r"C:\Windows\Fonts").is_dir():
        for f in Path(r"C:\Windows\Fonts").glob("*.ttf"):
            return f
    return None


def test_font_scan_system_dir(tmp_storage, monkeypatch):
    real = _find_real_font()
    if real is None:
        pytest.skip("未找到可用的 ttf 字体文件")
    svc = FontService(storage=tmp_storage)
    _run(svc.on_start())
    try:
        # 构造临时字体目录，只放一个字体文件，避免枚举整盘系统字体。
        tmp_font_dir = tmp_storage.paths.cache / "fake_sys_fonts"
        tmp_font_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        staged = tmp_font_dir / real.name
        shutil.copyfile(real, staged)
        monkeypatch.setattr(svc, "_system_font_dirs", lambda: [tmp_font_dir])
        found = _run(svc.scan_system_fonts())
        assert len(found) == 1
        assert found[0]["source"] == "system"
        assert found[0]["path"] == str(staged)
        assert found[0]["family"]  # 解析出非空族名
    finally:
        _run(svc.on_stop())


def test_font_add_list_remove_user(tmp_storage):
    real = _find_real_font()
    if real is None:
        pytest.skip("未找到可用的 ttf 字体文件")
    svc = FontService(storage=tmp_storage)
    _run(svc.on_start())
    try:
        rid = _run(svc.add_font_file(str(real)))
        assert rid > 0
        fonts = _run(svc.list_user_fonts())
        assert len(fonts) == 1
        assert fonts[0]["source"] == "user"
        assert fonts[0]["family"]
        # 文件确实拷进了数据目录 fonts/
        assert Path(fonts[0]["path"]).is_file()
        assert "fonts" in str(fonts[0]["path"])
        # 删除
        assert _run(svc.remove_font(rid)) is True
        assert _run(svc.list_user_fonts()) == []
    finally:
        _run(svc.on_stop())


def test_font_selected_family_settings(tmp_storage):
    from app.core.settings import SettingsService
    settings = SettingsService(storage=tmp_storage, engine=engine_hub())
    _run(settings.on_start())
    svc = FontService(storage=tmp_storage, settings=settings)
    _run(svc.on_start())
    try:
        assert svc.get_selected_family() == ""
        svc.set_selected_family("Arial")
        assert svc.get_selected_family() == "Arial"
        # 落 SettingsService 库（重启后仍在）；键必须是阅读器排版偏好那一个，
        # 否则字体页选完，正文永远不变字。
        from app.services.tools.font_service import SELECTED_FAMILY_KEY
        assert SELECTED_FAMILY_KEY == "reader.font_family"
        assert settings.get(SELECTED_FAMILY_KEY) == "Arial"
    finally:
        _run(svc.on_stop())
        _run(settings.on_stop())


# --------------------------------------------------------------------- #
# 5. QrCodeService：generate→decode 往返
# --------------------------------------------------------------------- #
def test_qr_generate_decode_roundtrip():
    svc = QrCodeService()
    _run(svc.on_start())
    try:
        payload = '{"name":"测试","pattern":"x","replacement":"y"}'
        png = _run(svc.generate(payload))
        assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
        text = _run(svc.decode_image(png))
        assert text == payload
    finally:
        _run(svc.on_stop())


def test_qr_decode_garbage_returns_none():
    import struct
    import zlib
    svc = QrCodeService()
    _run(svc.on_start())
    try:
        # 构造一张全黑 PNG（无二维码），解码应返回 None 而非抛错。
        width = height = 20
        row = b"\x00" + b"\x00" * width  # filter byte + black pixels
        raw = row * height
        def chunk(typ, data):
            return (struct.pack(">I", len(data)) + typ + data
                    + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))
        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw))
               + chunk(b"IEND", b""))
        assert _run(svc.decode_image(png)) is None
        # 非图片字节也返回 None
        assert _run(svc.decode_image(b"not an image at all")) is None
    finally:
        _run(svc.on_stop())
