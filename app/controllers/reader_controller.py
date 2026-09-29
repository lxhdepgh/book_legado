"""ReaderController：阅读器视图的事件订阅与服务编排（§7.4）。

订阅 :class:`BookmarkChangedEvent`（书签增删后刷新面板）与
:class:`ReadingSessionChangedEvent`（会话切换后重建目录/正文）。目录与正文经
``ReadingService.load_chapters/load_content``，进度经 ``save_progress``，书签面板
:class:`BookmarkPanel` 接 ``BookmarkService``、书内搜索条 :class:`ContentSearchBar`
接 ``ContentSearchService``——两者编排并入本 Controller（§7.4 不单开文件）。
阅读偏好（字号/行距/版心/背景…）同样经本控制器搬运：视图给值、``ReadingService``
存值，设置弹层由视图搭建、由这里 show/pop（视图拿不到 page 的生命周期）。
划词查词典是这条链的另一个入口：正文 selectable → 视图亮出「查词典」条 →
``lookup_word`` 经 ``DictService.lookup`` 抓释义 → 回填视图搭好的释义弹窗
（选中过程一次网络都不发，见 :meth:`lookup_word`）。
视频书同理走"条 → 面板"：正文里的视频块渲染成「在本页播放」，点了才经
``MediaPlayService`` 起播放会话（见 :meth:`play_video`）。
退出阅读按 Legado 的「返回时提示放入书架」收尾：暂存的试读书先问一声，放入=原地
转正、不放入=记录/进度/缓存一起删（见 docs/找书与换源逻辑对齐.md §1.5）。

本文件允许 import app.services.reading.* 类型（仅作 hub.get() 的类型参数）。
排版偏好的键表取自视图类属性 ``ReaderView.PREFERENCE_KEYS``——控制器 import
``app.views.reading`` 会绕回本包（views/__init__ 反向 import 控制器），是个循环。
"""
from __future__ import annotations

import asyncio
import logging

import flet as ft

from app.core.events import (BookmarkChangedEvent, MediaPlayStateEvent,
                             ReadAloudStateEvent, ReadingSessionChangedEvent)
from app.controllers.base import BaseController
from app.services.reading.reading_service import ReadingService
from app.services.reading.bookmark_service import BookmarkService
from app.services.reading.content_search import ContentSearchService
from app.services.content.media_play import FletVideoBackend, MediaPlayService
from app.services.content.read_aloud import ReadAloudService
from app.services.content.local_book_service import is_local_source_url
from app.services.sources.rule_repair_service import RuleRepairService
from app.services.engine.rule_engine import display_intro, html_to_text
from app.services.tools.dict_service import DictService

logger = logging.getLogger(__name__)


#: 阅读器快捷键默认绑定（对齐旧 ReadingView._load_reader_preferences）。
#: 键名以 Flet KeyboardEvent.key 的规范化形式为准（arrow left → left）。
DEFAULT_SHORTCUTS: dict[str, str] = {
    "previous": "left", "next": "right",
    "page_up": "pageup", "page_down": "pagedown",
    "directory": "f", "settings": "comma", "close": "escape",
}

#: Flet 事件键名 → 规范键名（对齐旧 _normalize_reader_key 的别名表）。
_KEY_ALIASES: dict[str, str] = {
    "arrow left": "left", "arrowleft": "left",
    "arrow right": "right", "arrowright": "right",
    "arrow up": "up", "arrowup": "up",
    "arrow down": "down", "arrowdown": "down",
    "page up": "pageup", "page down": "pagedown",
    " ": "space", ",": "comma", "keyf": "f",
    "keycomma": "comma", "esc": "escape",
    "control left": "ctrl", "control right": "ctrl",
    "controlleft": "ctrl", "controlright": "ctrl",
    "ctrl left": "ctrl", "ctrl right": "ctrl",
    "shift left": "shift", "shift right": "shift",
    "shiftleft": "shift", "shiftright": "shift",
    "alt left": "alt", "alt right": "alt",
    "altleft": "alt", "altright": "alt",
}


#: 自动续读的迟滞区间（px，对齐旧 ReadingView._on_reader_scroll）：距文档底部
#: 不足 180 才追加下一章，退回 500 以上才重新武装。一条阈值线会在边界上反复
#: 触发，两章之间来回跳。
_APPEND_TRIGGER_DISTANCE = 180.0
_APPEND_REARM_DISTANCE = 500.0


def _spoken_text(lines) -> str:
    """正文行 → 朗读稿：段首缩进的全角空格必须去掉，不然 TTS 每段先喘一口。"""
    return "\n".join(s for s in (str(line).strip("　 \t") for line in lines) if s)


def _definition_text(value) -> str:
    """释义载荷 → 可显示文本（渲染契约：带标记的原文一律不能照抄上屏）。

    ``ruleContent`` 抽回来的常常是节点原文（整段 HTML），直接贴给用户就是给
    一屏 ``<div class=...>``。``<md>`` 例外：Markdown 不渲染也读得下去，比丢掉强。
    """
    kind, payload = display_intro(value)
    if kind == "text":
        return payload
    if kind == "md":
        return payload
    return html_to_text(payload)


class ReaderController(BaseController):
    """阅读器：目录 / 正文 / 续读 / 偏好 / 沉浸 / 快捷键 / 上下章 / 书签 / 书内搜索 /
    朗读 / 视频书 / 划词查词典 / 退出阅读（试读暂存询问入架）。"""

    destination = "reading.reader"

    #: 跨视图导航动作（动作名 → 目的地）：NavigationHost.show 装配时绑定回调槽。
    #: 试读的书还没进书架，落回书架页等于扑空，所以退出后回找书页。
    #: 章节读不出来时的"自动修复该书源"要落到自愈确认页（§3.2：只给候选，用户点确认才写回）。
    nav_actions: dict[str, str] = {
        "on_back_to_bookshelf": "reading.bookshelf",
        "on_back_to_find_books": "reading.find_books",
        "on_open_repair": "sources.repair",
    }

    def on_attach(self) -> None:
        self._reading = self.service(ReadingService)
        self._bookmarks = self.service(BookmarkService)
        self._content_search = self.service(ContentSearchService)
        # 朗读是可选形态：替身表/精简装配里没有 ReadAloudService 时播放条整条不出现。
        self._tts = self._safe_service(ReadAloudService)
        # 自愈同样是可选形态：没有 RuleRepairService 时失败条上不给"自动修复"动作。
        self._repair = self._safe_service(RuleRepairService)
        # 词典同样是可选形态：没有 DictService 时「查词典」给一句明确的话，不静默。
        self._dict = self._safe_service(DictService)
        # 视频书也是可选形态：没有 MediaPlayService 时面板直接报"服务未就绪"，
        # 而不是悄悄把视频丢给系统默认播放器（凭空开一个外部窗口）。
        self._media = self._safe_service(MediaPlayService)

        self._chapters: list[dict] = []
        self._readable: list[dict] = []   # 去掉卷标题后的可读书章
        self._chapter_index = -1
        self._book_url = ""
        #: 底部状态栏的书名行：《书名》 · 作者（阅读界面里唯一说得出"这是哪本书"的地方）。
        self._book_label = ""
        #: 在途/已开的书（书源|书名）：连点两次「开始阅读」不该再跑一遍目录加载
        self._opening = ""
        # 跨视图导航槽：NavigationHost.show 装配时绑定（视图间零互引）
        self.on_back_to_bookshelf = None    # () -> None
        self.on_back_to_find_books = None  # () -> None
        self.on_open_repair = None         # () -> None（跳自愈确认页）
        #: 屏幕上最近一次读不出来的章节对象：失败条上的「自动修复」就拿它诊断
        self._failed_chapter = None

        # 连续续读状态（对齐旧 ReadingView 的同名字段，见 _on_reader_scroll）
        self._continuous_loaded: set[str] = set()
        self._pending_append_starts: dict[str, float] = {}
        self._auto_scroll_armed = True
        self._reading_loading = False
        self._restoring_scroll = False
        self._pending_document_offset = 0.0

        # 朗读状态：一次只允许一条循环在跑，换号（generation）让在途那条自己收工
        self._tts_playing = False
        self._tts_generation = 0
        self._tts_follow = True
        # 当前章的朗读稿：正文挂出来时顺手留一份纯文本，播放条按章取用
        self._tts_text = ""
        self._tts_text_url = ""
        # 音色（tts_profiles 表）：清单缓存在本地，选中的那条决定 speak 走哪个引擎
        self._tts_profiles: list[dict] = []
        self._tts_profile: dict | None = None

        # 视频书面板：播放列表按正文出现的顺序累积（url 去重），面板只在真播时亮
        self._video_items: list[dict] = []
        self._video_index = -1
        self._video_state = "idle"
        # 播放列表弹层：控件视图搭、show/pop 这里管（同设置弹层的分工）
        self._video_dialog = None

        # 视图回调 → 业务方法
        self._view.on_open_chapter = self.open_chapter
        self._view.on_prev_chapter = self.prev_chapter
        self._view.on_next_chapter = self.next_chapter
        self._view.on_toggle_immersive = self.toggle_immersive
        self._view.on_change_font_size = self.change_font_size
        self._view.on_save_scroll = self._on_reader_scroll
        self._view.on_open_directory = self.open_directory
        self._view.on_close_directory = lambda: self._view.show_directory(False)
        self._view.on_open_bookmarks = self.open_bookmark_panel
        self._view.on_open_search = self.open_content_search
        self._view.on_exit_reader = self.exit_reader
        self._view.on_repair_source = self.repair_failed_chapter
        # 划词条上的"查词典"
        self._view.on_lookup_selection = self.lookup_word
        # 正文视频条目上的「在本页播放」
        self._view.on_play_video = self.play_video

        # 侧面板回调
        self._view.bookmark_panel.on_add = self.add_bookmark
        self._view.bookmark_panel.on_jump = self.jump_to_bookmark
        self._view.bookmark_panel.on_remove = self.remove_bookmark
        self._view.bookmark_panel.on_close = lambda: self._view.show_side_panel("")
        self._view.content_search_bar.on_search = self.search_in_book
        self._view.content_search_bar.on_jump = self.jump_to_hit
        self._view.content_search_bar.on_close = lambda: self._view.show_side_panel("")
        bar = getattr(self._view, "read_aloud", None)
        if bar is not None:
            bar.on_toggle = self.toggle_read_aloud
            bar.on_stop = self.stop_read_aloud
            bar.on_follow_changed = self.set_read_aloud_follow
            bar.on_rate_changed = self.set_read_aloud_rate
            bar.on_voice_changed = self.set_read_aloud_voice
        video = getattr(self._view, "video_play", None)
        if video is not None:
            video.on_play_index = self._pick_video      # (index) 播放列表里选一条
            video.on_toggle = self.toggle_video
            video.on_stop = self.stop_video
            video.on_prev = self.prev_video
            video.on_next = self.next_video
            video.on_close = self.close_video
            video.on_open_playlist = self.open_video_playlist
            # 播放器自己上报的两条（客户端事件，与按钮走同一条派发路径）
            video.on_ended = self.video_ended
            video.on_failed = self.video_failed

        # 事件订阅
        self.subscribe(BookmarkChangedEvent, self._on_bookmark_changed)
        self.subscribe(ReadingSessionChangedEvent, self._on_session_changed)
        self.subscribe(ReadAloudStateEvent, self._on_read_aloud_state)
        self.subscribe(MediaPlayStateEvent, self._on_media_play_state)
        # 滚动保存 0.5s 节流（§7.4：对齐旧 _save_scroll_position_after_delay）
        self._save_position = self.throttle(self._persist_position, 0.5)

        # 快捷键 + 阅读设置弹层（旧 ReadingView 方向键/翻页/目录能力，M6 漏接的
        # page.on_keyboard_event 在 navigation.build_app_shell 处接线分发到视图 on_key）
        self._shortcuts: dict[str, str] = dict(DEFAULT_SHORTCUTS)
        self._settings_dialog = None
        # 释义弹窗：_lookup_word 是弹窗当前认领的那个词，关掉即清空——
        # 在途结果回来时对不上号就丢弃，晚到的 A 词释义不能贴到 B 词弹窗上。
        self._definition_dialog = None
        self._lookup_word = ""
        self._view.on_key = self._on_key
        self._view.on_open_settings = self.open_settings
        self.ui_call(self._load_preferences())

    def on_detach(self) -> None:
        """离开阅读器就叫停后台预取与朗读。

        切页不等于退出阅读——会话还留在服务里，副本也留着（回来还能接上），但
        引擎不该在用户已经去看书源页的时候继续替那本书拉后面十章。

        朗读随视图一起停是**有意的**：设计 §1.1 设想过全局播放覆盖层，M6 拆层后
        播放条挂在阅读器这一块整页视图上，切页即随视图拆走——声音继续放着却没
        地方按停，比安静更糟。视频面板同一处境（画面跟着视图走了声音还在放）。
        """
        self._reading.stop_prefetch()
        if self._tts is not None and self._tts_playing:
            self._tts_playing = False
            self._tts_generation += 1
            self.ui_call(self._stop_backend("已离开阅读器"))
        self._stop_video_playback()

    def _safe_service(self, stype):
        """取可选服务：没注册时返回 None，相关形态就地降级而不是把阅读器拖挂。"""
        try:
            return self.service(stype)
        except Exception:
            return None

    async def _load_preferences(self) -> None:
        """读回阅读偏好（排版参数 + 快捷键 + 语速）；没存过就用视图自带的默认值。"""
        view = self._view
        if view is None:
            return
        prefs = {}
        for key in getattr(view, "PREFERENCE_KEYS", ()):
            value = await self._reading.get_reader_preference(key, None)
            if value is not None:
                prefs[key] = value
        if prefs:
            if self._view is None:
                return    # 一项一项 await，页随时可能先走掉
            self._view.apply_preferences(prefs)
        saved = await self._reading.get_reader_preference("shortcuts", None)
        if isinstance(saved, dict):
            merged = dict(DEFAULT_SHORTCUTS)
            for name, binding in saved.items():
                if name in merged:
                    merged[name] = str(binding or "").strip().lower()
            self._shortcuts = merged
        # 语速不属于排版（不参与重排），但和快捷键一样是"进程级读一次"的偏好
        rate = await self._reading.get_reader_preference("tts_rate", None)
        bar = getattr(self._view, "read_aloud", None) if self._view else None
        if rate is not None and bar is not None:
            bar.set_rate(str(rate))
        await self._load_voice_profiles(bar)

    async def _load_voice_profiles(self, bar) -> None:
        """音色下拉的选项来自 tts_profiles：进阅读器时拉一次。

        表是空的（还没在 配置 → 朗读 里登记过）也要拉——不拉就分不清"没有语音档"
        和"这条语音档已被删掉"，下拉会一直空着，用户以为功能没做。
        """
        if bar is None or self._tts is None:
            return
        try:
            profiles = await self._tts.list_profiles()
        except Exception as exc:    # 读表失败只是没有音色可选，不该拖垮整页偏好
            logger.warning("朗读语音档载入失败：%s", exc)
            return
        profile_id = str(await self._reading.get_reader_preference(
            "tts_profile", "") or "")
        if self._view is None:    # 两次 await 之间页可能已经切走
            return
        self._tts_profiles = list(profiles or [])
        bar.set_voice_options(self._tts_profiles, current=profile_id)
        self._tts_profile = self._find_profile(bar.voice)

    # ------------------------------------------------------------------ #
    # 快捷键（M6 补回：键位规范化 + 动作分发，行为对齐旧 _on_reader_key）
    # ------------------------------------------------------------------ #
    @classmethod
    def _normalize_key(cls, value) -> str:
        key = str(value or "").strip().lower().replace("_", " ")
        if key.startswith("key ") and len(key) == 5:
            key = key[-1]
        return _KEY_ALIASES.get(key, key)

    def _on_key(self, event) -> None:
        """阅读器快捷键分发（仅正文页生效，由导航层在 on_keyboard_event 中路由）。"""
        binding = self._normalize_key(getattr(event, "key", ""))
        if binding in {"ctrl", "shift", "alt"}:
            return
        shortcuts = self._shortcuts
        if binding == shortcuts["directory"]:
            self.open_directory()
        elif binding == shortcuts["previous"]:
            self.prev_chapter()
        elif binding == shortcuts["next"]:
            self.next_chapter()
        elif binding == shortcuts["page_up"]:
            self._view.scroll_by(-560)
        elif binding == shortcuts["page_down"]:
            self._view.scroll_by(560)
        elif binding == shortcuts["settings"]:
            self.open_settings()
        elif binding == shortcuts["close"]:
            self._view.show_directory(False)
            self._view.show_side_panel("")
            self.close_settings()

    # ------------------------------------------------------------------ #
    # 会话 / 目录
    # ------------------------------------------------------------------ #
    async def open_book(self, source, book, shelve: bool = True) -> None:
        """打开书：ReadingService.open_book 加载目录并恢复进度。

        ``shelve=False`` 即找书试读（Legado notShelf，只暂存不入架）。
        """
        from app.services.reading.reading_service import as_book_item
        record = as_book_item(book)
        key = f"{getattr(source, 'bookSourceUrl', '')}|{getattr(record, 'url', '')}"
        if key == self._opening:
            # 连点两次「开始阅读」：第二次不该再跑一遍目录加载，两趟并发只会
            # 互相把对方的正文换掉。
            return
        self._opening = key
        try:
            session = await self._reading.open_book(source, book, shelve=shelve)
        except Exception as exc:
            # 目录都拿不到就没有会话可言。这条一定要接住：调用方是
            # ``ui_call`` 投递的（没人 await 这个 future），漏在这里
            # 用户看到的就是"点了书没反应"的白屏——本地书缓存丢失
            # (LocalBookCacheMissing) 正是走这条。
            logger.warning("打开书籍失败：%s", exc)
            self._opening = ""
            if self._view is not None:
                self._view.set_progress(f"打开失败：{exc}")
                self.flush()
            return
        if self._view is None:
            # 目录读回来的路上用户切了页：视图已经摘掉，这一趟没有地方可挂
            logger.debug("阅读器已离开，放弃打开 %s", getattr(record, "name", ""))
            return
        self._book_url = getattr(session, "book_url", "") or ""
        book_name = str(getattr(record, "name", "") or "")
        book_author = str(getattr(record, "author", "") or "")
        self._book_label = " · ".join(p for p in (
            f"《{book_name}》" if book_name else "", book_author) if p)
        chapters = getattr(session, "chapters", []) or []
        self._set_chapters([
            {"index": int(getattr(c, "index", i) or i),
             "name": getattr(c, "name", "") or "",
             "url": getattr(c, "url", "") or "",
             "is_volume": bool(getattr(c, "is_volume", False))}
            for i, c in enumerate(chapters)
        ])
        self._view.set_title(getattr(record, "name", "") or "正文")
        # 排版偏好是进程级的，attach 时已经读回来（见 _load_preferences），
        # 每开一本书再读一遍只会让首屏多等两次设置表查询。
        # 新书也从第一章开读；有进度则回到上次章节 + 章内偏移。
        pos = getattr(session, "position", None)
        index = int(getattr(pos, "chapter_index", 0) or 0) if pos is not None else 0
        offset = float(getattr(pos, "chapter_offset", 0.0) or 0.0) \
            if pos is not None else 0.0
        await self._open_index(index, restore_offset=offset)

    async def _open_index(self, index: int, restore_offset: float = 0.0) -> None:
        """整篇换成第 index 章（点目录 / 上下章 / 跳书签 / 跳搜索结果）。"""
        if not self._readable:
            return
        index = max(0, min(index, len(self._readable) - 1))
        chapter = self._readable[index]
        # 服务层 ChapterItem 对象由 open_book 会话持有；这里按 url 找回。
        await self._run_chapter(chapter, self._chapter_by_url(chapter["url"]),
                                append=False, restore_offset=restore_offset)

    async def _run_chapter(self, chapter: dict, raw, append: bool,
                           restore_offset: float = 0.0) -> None:
        """加载并挂出一章：``append`` 决定是接到文末还是整篇替换。

        两条路径的分工照旧（旧 ``ReadingView._run_content``）：

        - 替换 = 连续阅读重新起头：撤掉已挂章节的视口、正文从章首（或上次
          偏移）开始、自动续读重新布防；
        - 追加 = 读到底接下一章：只在文末补"分隔线 + 章名 + 正文"，登记该章
          在整篇文档里的起始偏移，滚动位置一点都不动——动了一下就是"跳回顶部"。
        """
        index = self._readable_index(chapter["url"])
        if index < 0 or self._view is None:
            return
        self._reading_loading = True
        # 换整篇文档时挂载瞬间会先发一次"偏移 0"的滚动事件：那是布局噪音不是
        # 用户读到了章首，让它落盘就把上次进度抹成了 0。
        self._restoring_scroll = not append
        if not append:
            # 整篇替换要清空正文，先把"正在加载"推出去：这次 await 之后才补
            # 控件，慢源在屏上是好几秒的空白，用户会以为点没生效。
            # （ui_call 只在收尾 flush 一次，所以这里必须自己 flush。）
            self._view.set_progress(f"正在加载 {chapter['name']}…")
            self.flush()
        try:
            document = await self._reading.load_content(raw, append=append)
            plan = await asyncio.to_thread(self._content_plan, document)
            if self._view is None:
                # 正文读回来的路上切了页：控件没地方挂，进度也不该记
                return
            self._view.render_plan(plan, append=append,
                                   chapter_name=chapter["name"])
            if not append:
                # 这一章读出来了：上一条失败原因和它的补救条一起收掉，
                # 不然屏幕上会同时挂着"正文"和"这一章读不出来"。
                self._failed_chapter = None
                self._view.hide_repair_offer()
            lines = [line for kind, payload, _ in plan if kind == "text"
                     for line, _heading in payload]
            text_length = sum(len(line) for line in lines)
            if self._tts is not None:
                # 顺手留一份朗读稿：正文已经在这里切成段了，朗读不必再走一遍取文
                self._tts_text_url = chapter["url"]
                self._tts_text = _spoken_text(lines)
            # 这一章正文里的视频块 → 播放列表条目（连读追加时列表会一直长）
            self._collect_videos(plan, chapter)
            start = 0.0
            if append:
                # 触发加载的那次滚动事件里记下的"追加前文档总高"就是本章起点；
                # 拿不到（例如手动续读）就用当前偏移兜底。
                start = self._pending_append_starts.pop(
                    chapter["url"], self._pending_document_offset)
            self._mount_chapter(raw, start, text_length, reset=not append,
                                font_size=self._current_font_size(),
                                offset=0.0 if append else restore_offset)
            self._continuous_loaded.add(chapter["url"])
            if not append:
                self._chapter_index = index
                self._view.set_title(chapter["name"])
                self._update_chapter_navigation()
                self._auto_scroll_armed = True
                if restore_offset > 0:
                    # 桌面端可能先确认控件更新、后才测量新段落：只滚一次会被当时
                    # 还是 0 的滚动高度夹回顶部（表现为"续读位置丢了"）。补两帧。
                    for delay in (0.05, 0.15):
                        await asyncio.sleep(delay)
                        if self._view is None:
                            return    # 补帧的路上切了页：控件已经不在了
                        self._view.restore_scroll(restore_offset)
            # 这一章已经挂上屏幕：后台把后面几章拉回来（不等网络，只起一轮）。
            # 读到底才现取下一章，在弱网上就是一次可感知的停顿。
            if raw is not None:
                self.ui_call(self._reading.prefetch_chapters(raw))
        except Exception as exc:
            logger.warning("章节加载失败 %s：%s", chapter.get("url"), exc)
            if not append and self._view is not None:
                self._view.set_progress(f"加载失败：{exc}")
                self._offer_repair(raw, exc)
        finally:
            self._reading_loading = False
            self._restoring_scroll = False
            self.flush()

    # ------------------------------------------------------------------ #
    # 章节读不出来 → 正文规则自愈入口（§3.2：只给候选，写回要用户确认）
    # ------------------------------------------------------------------ #
    def _offer_repair(self, raw, exc: Exception) -> None:
        """记下失败的章节对象，并按"这源到底能不能自愈"决定亮不亮补救条。

        本地书没有规则源、没注册 RuleRepairService 的精简装配没有诊断能力——那时
        只留一行失败原因，亮一个按了只会跳到空白页的按钮比不亮更糟。
        """
        self._failed_chapter = raw
        source = getattr(self._reading, "current_source", None)
        if (raw is None or self._repair is None or source is None
                or is_local_source_url(getattr(source, "bookSourceUrl", ""))):
            self._view.hide_repair_offer()
            return
        reason = str(exc) or type(exc).__name__
        self._view.show_repair_offer(f"这一章读不出来：{reason[:60]}")

    def repair_failed_chapter(self) -> None:
        """补救条上的「自动修复该书源」：起一轮诊断，随即跳到自愈确认页。

        先跳页再等结果：诊断要真抓 1-3 页样本、再把几十条候选在本地回放（秒级到
        十几秒），留在阅读器里用户只看得到一句"加载失败"。跳过去之后，自愈页的
        控制器订阅 :class:`RepairProgressEvent` 逐阶段刷状态、
        :class:`RepairReportReadyEvent` 一到就渲染报告卡。
        """
        raw = self._failed_chapter
        source = getattr(self._reading, "current_source", None)
        if self._view is None or raw is None or self._repair is None:
            return
        self._failed_chapter = None
        self._view.hide_repair_offer()
        self.ui_call(self._repair.repair(
            source, raw, extra_chapters=self._repair_extras(raw),
            book=getattr(self._reading, "current_book", None)))
        # 诊断在引擎循环上跑，不等它：这一跳把用户送到能看到进度的地方。
        if self.on_open_repair is not None:
            self.on_open_repair()

    def _repair_extras(self, chapter) -> list:
        """复核样本 = 紧跟失败章后面的 1-2 章（同书同规则，最能验出候选真伪）。

        卷标题没有 url：把它递给诊断只会白抓一次，所以按可读书章筛（与续读那条
        ``_adjacent_chapter`` 同一个口径）。
        """
        session = getattr(self._reading, "current_session", None)
        chapters = list(getattr(session, "chapters", []) or [])
        urls = [getattr(c, "url", "") for c in chapters]
        try:
            at = urls.index(getattr(chapter, "url", ""))
        except ValueError:
            return []
        picked: list = []
        for item in chapters[at + 1:]:
            if not getattr(item, "url", "") or getattr(item, "is_volume", False):
                continue
            picked.append(item)
            if len(picked) >= 2:
                break
        return picked

    def _mount_chapter(self, raw, document_start: float, text_length: int,
                       reset: bool, font_size: int, offset: float) -> None:
        """把一章登记进阅读会话的视口表（滚动 track 全靠它）。

        ``raw`` 可能为 None（服务层还没建会话，例如离线替身）：那时只能显示
        正文，定位与续读自动降级，不为它造半个假章节对象。
        """
        session = getattr(self._reading, "current_session", None)
        if raw is None or session is None or not hasattr(session, "mount"):
            return
        if reset:
            # 换整篇文档：上一次续读挂上的章节视口必须一起撤掉，否则 track()
            # 还会报出早就不在屏幕上的章节，进度会跳到别的书章上去。
            resetter = getattr(session, "reset", None)
            if callable(resetter):
                resetter(getattr(session, "source_url", ""),
                         getattr(session, "book_url", ""),
                         getattr(session, "chapters", []),
                         getattr(session, "position", None))
        session.mount(raw, document_start, text_length)
        session.enter(raw, font_size, offset=offset)

    def _readable_index(self, url: str) -> int:
        for i, c in enumerate(self._readable):
            if c["url"] == url:
                return i
        return -1

    def _current_chapter(self) -> dict | None:
        """当前章条目：屏幕上在读的那一章（没开或越界返回 None）。"""
        index = self._chapter_index
        if 0 <= index < len(self._readable):
            return self._readable[index]
        return None

    def _adjacent_chapter(self, direction: int) -> dict | None:
        """按方向找最近一章可读（跳过卷标题与空 URL）；到边返回 None。"""
        if not self._readable or self._chapter_index < 0:
            return None
        index = self._chapter_index + direction
        while 0 <= index < len(self._readable):
            candidate = self._readable[index]
            if candidate["url"]:
                return candidate
            index += direction
        return None

    def _update_chapter_navigation(self) -> None:
        """工具栏「序号/总数 · 章名」+ 状态栏「《书名》 · 作者 · 共 N 章」+ 上下章可用性 + 朗读可用性。"""
        total = len(self._readable)
        index = self._chapter_index
        # 朗读的**可见性**现在归视图（工具栏那颗能收起），控制器只报"这会儿能不能念"：
        # 没有朗读服务（替身装配/精简启动）或没有可念的章时，入口整颗禁用。
        self._view.set_read_aloud_available(
            self._tts is not None and 0 <= index < total)
        if not total or not 0 <= index < total:
            self._view.set_progress("未选择章节")
            self._view.set_status_bar("")
            self._view.set_nav_enabled(prev=False, next_=False)
            return
        name = self._readable[index]["name"]
        self._view.set_progress(f"{index + 1}/{total} · {name}")
        # 底部状态栏说"这是哪本书"，顶部进度标签说"读到第几章"——以前两条写的是
        # 同一句话（章名 + 序号 + 百分比），而整屏没有一个地方显示书名。
        self._view.set_status_bar(" · ".join(
            p for p in (self._book_label, f"共 {total} 章") if p))
        self._view.set_nav_enabled(prev=self._adjacent_chapter(-1) is not None,
                                   next_=self._adjacent_chapter(1) is not None)

    def _current_font_size(self) -> int:
        return int(getattr(self._view, "_font_size", 16) or 16)

    def _display_document(self, document):
        """正文块 → 可显示形态：``<usehtml>`` 段交渲染器，视图不碰服务。

        红线：视图不许 import app.services.*；清洗放控制器，喂给视图的仍是鸭子
        类型 ``.blocks``（kind/value/title/headers）。媒体块的 URL 原样传下去——
        对着一串地址跑 HTML 清洗，等于给图片路径埋一条未知改写的路。
        """
        from types import SimpleNamespace

        from app.services.engine.reading_engine import render_use_html_spans
        blocks = getattr(document, "blocks", None) or []
        return SimpleNamespace(
            image_style=str(getattr(document, "image_style", "") or ""),
            blocks=[SimpleNamespace(
                kind=getattr(b, "kind", ""),
                value=(render_use_html_spans(getattr(b, "value", "") or "")
                       if getattr(b, "kind", "") == "text"
                       else getattr(b, "value", "") or ""),
                title=getattr(b, "title", "") or "",
                headers=getattr(b, "headers", None) or {})
                for b in blocks])

    def _content_plan(self, document):
        """清洗 + 切段一起放到后台线程：视图只拿计划建控件。

        这两步都是纯计算（不碰控件、不碰服务），一章几千行时是几十上百毫秒，
        压在界面循环上就是一次可感知的卡顿（旧实现同样丢给线程跑）。
        """
        return self._view.content_plan(self._display_document(document))

    def _chapter_by_url(self, url: str):
        session = getattr(self._reading, "current_session", None)
        for c in getattr(session, "chapters", []) or []:
            if getattr(c, "url", "") == url:
                return c
        return None

    def open_directory(self) -> None:
        """开目录并定位到当前章：只把页面翻出来不算"找得到在读哪"。

        当前章由控制器知道（``_chapter_index`` 是屏幕上的那一章），视图只认 url：
        过滤、倒序都会挪行号，url 不会。
        """
        self._view.show_directory(True)
        chapter = self._current_chapter()
        self._view.focus_chapter(str((chapter or {}).get("url") or ""))

    def open_chapter(self, chapter: dict, _=None) -> None:
        self._view.show_directory(False)
        self._interrupt_read_aloud()
        index = self._readable_index(str(chapter.get("url") or ""))
        if index >= 0:
            self.ui_call(self._open_index(index))

    def prev_chapter(self, _=None) -> None:
        self._jump_relative(-1)

    def next_chapter(self, _=None) -> None:
        self._jump_relative(1)

    def _jump_relative(self, direction: int) -> None:
        """手动翻章：整篇替换（不是追加），卷标题由 _adjacent_chapter 跳掉。"""
        self._interrupt_read_aloud()
        target = self._adjacent_chapter(direction)
        if target is None:
            return
        self.ui_call(self._open_index(self._readable_index(target["url"])))

    # ------------------------------------------------------------------ #
    # 偏好 / 沉浸 / 阅读设置弹层
    # ------------------------------------------------------------------ #
    def set_preference(self, key: str, value) -> None:
        """改一项排版偏好：就地重排已挂出的正文，再把生效值落库。

        落的是视图夹过区间之后的值（滑块能给出版本里没有的怪数），否则设置表
        里存着 900 宽、下次开图又变回 760，用户看到的是"设置没保存"。
        """
        if self._view is None:
            return
        applied = self._view.apply_preferences({key: value}).get(key)
        self.ui_call(self._reading.save_reader_preference(key, applied))
        self.flush()

    def change_font_size(self, delta: int, _=None) -> None:
        """字号增删：区间 13-28（旧阅读器同界），只重排、不重新拉正文。

        重新渲染会把自动续读接上的多章文档换回"只有当前章"，用户滚到第二章
        改个字号就发现自己被扔回了第一章开头。
        """
        self.set_preference("font_size",
                            int(getattr(self._view, "_font_size", 16)) + int(delta))

    def open_settings(self, _=None) -> None:
        """阅读设置：工具栏按钮 / 逗号快捷键进（弹层由视图搭、这里管开关）。"""
        page = self._page()
        if page is None:
            return
        self._settings_dialog = self._view.build_settings_dialog(
            self.set_preference, self.close_settings)
        page.show_dialog(self._settings_dialog)
        self.flush()

    def close_settings(self, _=None) -> None:
        if self._settings_dialog is None:
            return
        page = self._page()
        self._settings_dialog = None
        if page is not None:
            page.pop_dialog()
        self.flush()

    def toggle_immersive(self, _=None) -> None:
        self._view.set_immersive(not self._view.immersive)

    # ------------------------------------------------------------------ #
    # 划词查词典（选中 → 划词条 → 点「查词典」→ DictService.lookup → 释义弹窗）
    # ------------------------------------------------------------------ #
    def lookup_word(self, word: str, _=None) -> None:
        """查划词条上那个词：一次点击一次请求，选中过程一条网络都不发。

        弹窗先亮"查询中…"：``lookup`` 要抓释义页（几秒），点了没反应会被当成
        按钮坏了、连点第二下——所以同一词在途时直接忽略。
        """
        word = str(word or "").strip()
        if not word:
            return
        if self._lookup_word == word and self._definition_dialog is not None:
            return
        if self._dict is None:
            self._open_definition(word, status="词典服务未就绪，查不了词")
            return
        self._open_definition(word, status="查询中…")
        future = self.ui_call(self._dict.lookup(word))
        future.add_done_callback(self.on_done(lambda f: self._after_lookup(word, f)))

    def _open_definition(self, word: str, *, text: str = "",
                         status: str = "") -> None:
        """开（或换到）释义弹窗：只有真开了弹窗才认领 ``_lookup_word``。"""
        page = self._page()
        if page is None:
            return
        self._lookup_word = word
        self._definition_dialog = self._view.build_definition_dialog(
            word, text, status, self.close_definition)
        page.show_dialog(self._definition_dialog)
        self.flush()

    def _after_lookup(self, word: str, future) -> None:
        if self._view is None:    # 在途时用户已切页
            return
        if self._lookup_word != word:
            # 弹窗已经换成别的词（或已被关掉）：这条迟到结果贴上去就是假释义。
            return
        try:
            raw = future.result()
        except Exception as exc:
            logger.warning("划词查询失败 %s: %s", word, exc)
            self._view.update_definition(status=f"查询失败：{exc}")
            return
        text = _definition_text(raw)
        if text.strip():
            self._view.update_definition(text=text)
            return
        # 服务侧"没有启用规则 / 抓取失败"都只回空串：这里必须说清是哪种不见得，
        # 但绝不能只显示一个空弹窗。
        self._view.update_definition(status=(
            f"没查到「{word}」的释义：词典规则可能未启用或释义页抓取失败\n"
            "规则在「更多工具 → 词典」里维护"))

    def close_definition(self, _=None) -> None:
        self._lookup_word = ""
        if self._definition_dialog is None:
            return
        page = self._page()
        self._definition_dialog = None
        if page is not None:
            try:
                page.pop_dialog()
            except Exception:
                pass
        self.flush()

    # ------------------------------------------------------------------ #
    # 滚动：定位落库 + 读到底自动续下一章（0.5s 节流落库）
    # ------------------------------------------------------------------ #
    def _set_chapters(self, chapters: list[dict]) -> None:
        """目录落位：一次换书要把派生状态一起重置（漏一项就是上一本的残留）。"""
        self._interrupt_read_aloud()
        self._interrupt_video()
        self._chapters = list(chapters or [])
        self._readable = [c for c in self._chapters if not c["is_volume"]]
        self._chapter_index = -1
        self._continuous_loaded.clear()
        self._pending_append_starts.clear()
        self._auto_scroll_armed = True
        self._reading_loading = False
        self._restoring_scroll = False
        self._pending_document_offset = 0.0
        self._tts_text = ""
        self._tts_text_url = ""
        self._view.render_chapters(self._chapters)

    def _on_reader_scroll(self, event=None) -> None:
        """滚动事件三件事：会话定位、0.5s 节流落库、到底了自己接下一章。

        判定照旧（old_code ``ReadingView._on_reader_scroll``）：离文档底部还有
        500px 以上就把"自动续读"重新武装，不足 180px 且已武装才追加下一章。
        迟滞区间是必要的——贴着一条阈值线判定会在边界上反复触发，一章能被接两遍。

        加载中标记 ``_reading_loading`` 只挡"再要一章"，绝不挡定位与落库：
        文档在取下一章时仍然可滚，一起挡住进度就再也不更新了。
        """
        session = getattr(self._reading, "current_session", None)
        if self._restoring_scroll or event is None:
            return
        try:
            pixels = float(getattr(event, "pixels", 0.0) or 0.0)
            maximum = float(getattr(event, "max_scroll_extent", 0.0) or 0.0)
        except (TypeError, ValueError):
            return
        if self._chapter_index < 0:
            return
        self._pending_document_offset = max(0.0, pixels)
        if session is not None and hasattr(session, "track"):
            position, visible = session.track(
                self._pending_document_offset, max(0.0, maximum),
                self._current_font_size())
            self._follow_visible_chapter(visible)
        # _save_position 是 async wrapper（throttle 感知 fn 签名）：必须经
        # ui_call 派发，直接同步调用只会得到永不执行的协程。
        self.ui_call(self._save_position())
        if not maximum or self._reading_loading:
            return
        distance = maximum - pixels
        if distance > _APPEND_REARM_DISTANCE:
            self._auto_scroll_armed = True
            return
        if distance >= _APPEND_TRIGGER_DISTANCE or not self._auto_scroll_armed:
            return
        chapter = self._adjacent_chapter(1)
        if chapter is None or chapter["url"] in self._continuous_loaded:
            return
        self._auto_scroll_armed = False
        # 本章在整篇文档里的起点 = 触发时刻的文档总高；先记下，加载回来再挂视口。
        self._pending_append_starts[chapter["url"]] = maximum
        self.ui_call(self._run_chapter(
            chapter, self._chapter_by_url(chapter["url"]), append=True))

    def _follow_visible_chapter(self, visible) -> None:
        """滚动到已追加的下一章：标题/计数跟着走，但正文一篇都不动。"""
        if visible is None:
            return
        index = self._readable_index(str(getattr(visible, "url", "") or ""))
        if index < 0 or index == self._chapter_index:
            return
        self._chapter_index = index
        self._view.set_title(self._readable[index]["name"])
        self._update_chapter_navigation()

    async def _persist_position(self) -> None:
        session = getattr(self._reading, "current_session", None)
        position = getattr(session, "position", None) if session is not None else None
        if position is None:
            return
        await self._reading.save_progress(self._chapter_index, position)

    # ------------------------------------------------------------------ #
    # 书签面板（接 BookmarkService）
    # ------------------------------------------------------------------ #
    def open_bookmark_panel(self, _=None) -> None:
        self._view.show_side_panel("bookmark")
        self.ui_call(self._refresh_bookmarks())

    async def _refresh_bookmarks(self) -> None:
        marks = await self._bookmarks.list_bookmarks(self._book_url)
        if self._view is None:
            return    # 列书签的路上切了页：面板没地方画
        self._view.bookmark_panel.render_bookmarks(list(marks or []))

    def add_bookmark(self, _=None) -> None:
        session = getattr(self._reading, "current_session", None)
        position = getattr(session, "position", None) if session else None
        ratio = float(getattr(position, "chapter_ratio", 0.0) or 0.0)
        name = (self._readable[self._chapter_index]["name"]
                if 0 <= self._chapter_index < len(self._readable) else
                f"第 {self._chapter_index + 1} 章")
        self.ui_call(self._bookmarks.add_bookmark(
            self._book_url, self._chapter_index, ratio, name))

    def remove_bookmark(self, bookmark_id: int, _=None) -> None:
        self.ui_call(self._bookmarks.remove_bookmark(
            self._book_url, int(bookmark_id)))

    def jump_to_bookmark(self, mark: dict, _=None) -> None:
        self._view.show_side_panel("")
        self._interrupt_read_aloud()
        self.ui_call(self._open_index(
            int((mark or {}).get("chapter_index", 0) or 0)))

    async def _on_bookmark_changed(self, event: BookmarkChangedEvent) -> None:
        if event.book_url == self._book_url:
            await self._refresh_bookmarks()

    # ------------------------------------------------------------------ #
    # 书内搜索条（接 ContentSearchService）
    # ------------------------------------------------------------------ #
    def open_content_search(self, _=None) -> None:
        self._view.show_side_panel("search")

    def search_in_book(self, keyword: str, _=None) -> None:
        keyword = str(keyword or "").strip()
        if not keyword:
            return
        self._view.content_search_bar.set_status("搜索中…")
        self.ui_call(self._run_content_search(keyword))

    async def _run_content_search(self, keyword: str) -> None:
        # 索引由书内搜索第一次用时建：ContentSearchService.rebuild_index 在生产里
        # 原本没有任何调用方（服务面齐全、界面永远"无命中"）。ensure_index 按
        # "已缓存章数 > 索引覆盖章数"补齐，所以整书缓存收尾不必再挂一个钩子。
        session = getattr(self._reading, "current_session", None)
        source_url = str(getattr(session, "source_url", "") or "")
        await self._content_search.ensure_index(self._book_url, source_url)
        hits = await self._content_search.search(self._book_url, keyword)
        if self._view is None:
            return    # 全书扫一遍要几秒，这期间切页是常规操作不是意外
        self._view.content_search_bar.render_results(list(hits or []))

    def jump_to_hit(self, hit: dict, _=None) -> None:
        self._interrupt_read_aloud()
        self.ui_call(self._open_index(int(hit.get("chapter_index", 0))))

    # ------------------------------------------------------------------ #
    # 朗读（接 ReadAloudService，播放条见 views/reading/read_aloud_bar.py）
    # ------------------------------------------------------------------ #
    def toggle_read_aloud(self, _=None) -> None:
        """播放条主按钮：停着就读当前章，读着就停。

        按**章**为粒度：系统 TTS 后端不上报朗读位置（事件里 position 恒为 0），
        所以没有句级进度，只有"这一章念完没念完"。念完之后要不要接下一章由
        ``_tts_follow`` 决定。
        """
        if self._tts is None:
            return
        if self._tts_playing:
            self.stop_read_aloud()
            return
        if self._chapter_index < 0:
            self._show_read_aloud("idle", detail="先打开一章再朗读")
            return
        self._tts_playing = True
        self._tts_generation += 1
        self.ui_call(self._read_aloud_loop(self._tts_generation))

    def stop_read_aloud(self, _=None) -> None:
        """停止朗读：先换号让循环收工，再叫停后端掐断当前语音。"""
        if self._tts is None:
            return
        was_playing = self._tts_playing
        self._tts_playing = False
        self._tts_generation += 1
        if was_playing:
            self.ui_call(self._stop_backend("已停止朗读"))

    def set_read_aloud_follow(self, value) -> None:
        self._tts_follow = bool(value)

    def set_read_aloud_rate(self, rate) -> None:
        """语速档位落库（键 ``reader.tts_rate``，与排版偏好同一命名空间）。

        语速不参与重排，所以和快捷键一样是"进程级读一次"的偏好，不进排版口径。
        """
        self.ui_call(self._reading.save_reader_preference("tts_rate", str(rate or "")))

    def set_read_aloud_voice(self, value) -> None:
        """选音色：落库（键 ``reader.tts_profile``）+ 立刻把后端切过去。

        正在念的时候不打断当前这句——opts 是**每章**算一次的，所以新音色从下一章
        生效，这句得说给用户听，否则又是"选了没反应"。
        """
        from app.views.reading.read_aloud_bar import BUILTIN_VOICE
        profile_id = ("" if str(value or "") in ("", BUILTIN_VOICE)
                      else str(value))
        self._tts_profile = self._find_profile(profile_id)
        if self._tts is not None:
            # set_voice 只切后端（engine_type 决定走哪条链），url/headers 随 opts 走
            self._tts.set_voice(self._tts_profile or {})
        if self._reading is not None:
            self.ui_call(self._reading.save_reader_preference(
                "tts_profile", profile_id))
        name = (self._tts_profile or {}).get("name", "")
        if self._tts_playing:
            self._show_read_aloud("speaking", detail=f"音色「{name}」从下一章生效")
        else:
            self._show_read_aloud("idle", detail=(
                f"已切换到音色「{name}」" if name else "已改用内置语音"))

    def _find_profile(self, profile_id: str) -> dict | None:
        for profile in self._tts_profiles:
            if str(profile.get("id", "")) == str(profile_id or ""):
                return profile
        return None

    def _read_aloud_opts(self, index: int) -> dict:
        """一次朗读的参数：章号（事件按它过滤）+ 语速 + 选中语音档的接法。"""
        opts: dict[str, object] = {"chapter": index}
        bar = getattr(self._view, "read_aloud", None) if self._view is not None else None
        rate = getattr(bar, "rate_value", None)
        if rate:
            opts["rate"] = int(rate)
        profile = self._tts_profile or {}
        engine_type = str(profile.get("engine_type") or "")
        if engine_type:
            opts["engine_type"] = engine_type
        url = str(profile.get("url") or "")
        if url:
            opts["url"] = url
        headers = profile.get("headers")
        if headers:
            opts["headers"] = headers
        return opts

    def _tts_backend_name(self) -> str:
        """实际在用的后端名：服务自己会回退（system→edge→http），界面只能问它。"""
        backend = getattr(self._tts, "active_backend", None) if self._tts else None
        return str(getattr(backend, "name", "") or "")

    async def _read_aloud_loop(self, generation: int) -> None:
        """一章接一章念下去：任何一处停不下来都算 bug，所以每步都先看 generation。"""
        while generation == self._tts_generation and self._tts_playing:
            chapter = self._current_chapter()
            if chapter is None:
                break
            name = chapter["name"] or f"第 {self._chapter_index + 1} 章"
            if self._tts_text_url != chapter["url"]:
                self._tts_playing = False
                self._show_read_aloud("idle", chapter=name,
                                      detail="本章正文还没加载好")
                return
            if not self._tts_text:
                self._tts_playing = False
                self._show_read_aloud("idle", chapter=name,
                                      detail="本章没有可朗读的正文")
                return
            # 点亮要跟着循环走：一章能念十几分钟，而 ui_call 只在协程收尾时 flush
            # 一次——不自己补这一下，连读时播放条会停在第一章。
            self._show_read_aloud("speaking", chapter=name)
            self.flush()
            try:
                await self._tts.speak(self._book_url, self._tts_text,
                                      self._read_aloud_opts(self._chapter_index))
            except Exception as exc:
                logger.warning("朗读失败 %s：%s", chapter.get("url"), exc)
                self._tts_playing = False
                self._show_read_aloud("error", chapter=name,
                                      detail=f"朗读失败：{exc}")
                return
            if generation != self._tts_generation or not self._tts_playing:
                return    # 念这一章的路上用户已经按了停止
            if not self._tts_follow:
                self._tts_playing = False
                self._show_read_aloud("idle", chapter=name, detail="本章读完")
                return
            following = self._adjacent_chapter(1)
            if following is None:
                self._tts_playing = False
                self._show_read_aloud("idle", chapter=name,
                                      detail="已经读到最后一章")
                return
            # 换章失败（正文没挂上）不在这里报：下一轮的"还没加载好"分支会说。
            await self._open_index(self._readable_index(following["url"]))
        self._tts_playing = False

    async def _stop_backend(self, detail: str = "") -> None:
        try:
            await self._tts.stop_speaking()
        except Exception as exc:
            logger.warning("停止朗读失败：%s", exc)
        self._show_read_aloud("idle", detail=detail)

    def _show_read_aloud(self, state: str, chapter: str = "",
                         detail: str = "") -> None:
        if self._view is None:
            return
        bar = getattr(self._view, "read_aloud", None)
        if bar is not None:
            # 把后端名一起传过去：语速只对系统 TTS 有意义，用户得看得见是谁在念，
            # 否则"调了语速没反应"又是一次没人解释得了的静默退化。
            bar.set_state(state, chapter=chapter, detail=detail,
                          backend=self._tts_backend_name())
            # 态同时回流到工具栏那颗入口：念着的时候它要禁用收起（见视图注释）。
            self._view.sync_read_aloud()

    async def _on_read_aloud_state(self, event: ReadAloudStateEvent) -> None:
        """服务态镜像到播放条。

        只认当前书 + 当前章的事件：连读翻下一章时上一章的 idle 可能后到，
        照单渲染会让播放条在"正在朗读"和"未开始"之间来回跳。
        """
        if str(getattr(event, "book_url", "") or "") != self._book_url:
            return
        try:
            chapter = int(getattr(event, "chapter", None))
        except (TypeError, ValueError):
            return
        if chapter != self._chapter_index:
            return
        state = str(getattr(event, "state", "") or "idle")
        name = (self._current_chapter() or {}).get("name", "")
        self._show_read_aloud(
            state, chapter=name,
            detail="朗读后端出错" if state == "error" else "")

    def _interrupt_read_aloud(self) -> None:
        """手动换章先停朗读：人在看别的章，还念着上一章是干扰不是陪伴。"""
        if self._tts_playing:
            self.stop_read_aloud()

    # ------------------------------------------------------------------ #
    # 视频书（接 MediaPlayService，面板见 views/reading/video_play_bar.py）
    # ------------------------------------------------------------------ #
    def _video_bar(self):
        return getattr(self._view, "video_play", None) if self._view is not None else None

    def _collect_videos(self, plan, chapter: dict) -> None:
        """把这一章正文里的视频块并进播放列表（按 url 去重，跨章累积）。

        列表就是"这本书目前读到的视频"：视频书（每章一条）连读几章就有几条，
        正文里嵌视频的小说则只有那几条。不内嵌播放器、只在点条目时才起会话，
        所以这里只登记地址，一次网络都不发。
        """
        fresh = False
        for kind, payload, _ in plan:
            if kind != "video":
                continue
            url = str(getattr(payload, "value", "") or "")
            if not url or any(item.get("url") == url for item in self._video_items):
                continue
            self._video_items.append({
                "url": url,
                "title": (str(getattr(payload, "title", "") or "")
                          or chapter.get("name") or "视频"),
                "headers": dict(getattr(payload, "headers", None) or {}),
                "chapter_url": str(chapter.get("url") or ""),
            })
            fresh = True
        if not fresh:
            return
        bar = self._video_bar()
        # 面板已经开着时才回填：列表长了一条，播放列表弹层里就该多一条。
        if bar is not None and getattr(bar, "visible", False):
            bar.set_playlist(self._video_items, self._video_index)

    def play_video(self, url, _=None) -> None:
        """正文条目上的「在本页播放」→ 起一次播放会话。

        听书和看视频不能同时出声：起播先把朗读叫停。
        """
        url = str(url or "")
        if not url:
            return
        self._interrupt_read_aloud()
        self.ui_call(self._start_video(url))

    async def _start_video(self, url: str, *, resume: bool | None = None) -> None:
        """点亮面板 → 装播放后端 → ``open_book`` + ``play_chapter``。

        ``resume`` 三态：``None`` 认"点的正是上次看到那条"（接着看），``False``
        强制从片头放（面板上的重播/切条走这条）。
        """
        bar = self._video_bar()
        if bar is None:
            return
        items = self._video_items
        index = next((n for n, item in enumerate(items)
                      if item.get("url") == url), -1)
        if index < 0:
            # 正文刚出现、还没登记进来的地址（切章途中点的）：当场补进列表
            items.append({"url": url, "title": "视频", "headers": {}})
            index = len(items) - 1
        bar.visible = True
        bar.set_playlist(items, index)
        if self._media is None:
            self._show_video("error", index, detail="视频播放服务未就绪")
            return
        if not bar.ensure_player():
            # 建不出来就明说没装组件：悄悄丢给系统默认播放器等于凭空开一个外部窗口
            self._show_video("error", index, detail=bar.player_error)
            return
        # 控件先上屏再驱动播放器：flet_video 的方法要求控件"已经挂到页面上"，
        # 而 ui_call 的 flush 排在协程收尾——不补这一下，第一次按播放就报错。
        self.flush()
        self._media.set_backend(FletVideoBackend(
            bar.player_control, media_factory=bar.make_video_media,
            duration_factory=bar.video_duration, ui_marshal=self.ui_await))
        try:
            restored_index, restored_position = await self._media.open_book(
                self._book_url, items)
            if resume is None:
                # "接着上次看"只对上次那一条生效：点了别的条目就从片头放
                resume = (index == restored_index and restored_position > 0)
            await self._media.play_chapter(index, resume=bool(resume))
        except Exception as exc:
            logger.warning("视频播放失败 %s：%s", url, exc)
            if self._view is None:    # 起播路上切了页
                return
            self._show_video("error", index, detail=f"播放失败：{exc}")

    def toggle_video(self, _=None) -> None:
        """面板主按钮：放着就暂停，暂停着就继续，其余（结束/出错/没起过）重播当前条。"""
        if self._media is None:
            return
        if self._video_state == "playing":
            self.ui_call(self._media.pause())
            return
        if self._video_state == "paused":
            self.ui_call(self._media.resume_playback())
            return
        if 0 <= self._video_index < len(self._video_items):
            self.ui_call(self._start_video(
                self._video_items[self._video_index]["url"], resume=False))

    def prev_video(self, _=None) -> None:
        self._step_video(-1)

    def next_video(self, _=None) -> None:
        self._step_video(1)

    def _step_video(self, direction: int) -> None:
        target = self._video_index + direction
        if 0 <= target < len(self._video_items):
            self.play_video(self._video_items[target]["url"])

    def video_ended(self, _=None) -> None:
        """播放器报「这一条放完了」：状态先落服务，再按列表顺序连播下一条。

        只认**正在放**时的上报：按了停止/收起面板之后客户端还可能补一发完成事件
        （缓冲边界、切后台回来），照单连播会把用户刚收掉的播放器又点亮。
        """
        if self._media is None or self._video_state != "playing":
            return
        self.ui_call(self._finish_video_entry())

    async def _finish_video_entry(self) -> None:
        index = self._video_index
        await self._media.mark_ended()
        if self._view is None or index != self._video_index:
            return   # 等状态落库的工夫里用户已经换条/切页
        target = index + 1
        if target < len(self._video_items):
            self.play_video(self._video_items[target]["url"])

    def video_failed(self, detail: str = "", _=None) -> None:
        """播放器报错：面板上把原因说清楚。

        **不**自动跳下一条——书源里的视频常常是整本都坏（防盗链失效、地址过期），
        连播就是一路错到底，比停在一条上更糟。
        """
        if self._video_state not in {"playing", "paused"}:
            return
        self._show_video("error", detail=str(detail or "").strip() or "播放器报错")
        self.flush()

    def stop_video(self, _=None) -> None:
        """停止播放但留着面板（还要按播放回来）。位置在叫停前读，见服务侧。"""
        self._stop_video_playback()

    def close_video(self, _=None) -> None:
        """收起面板：一并叫停播放器——画面收走了还在放，比不关更糟。"""
        bar = self._video_bar()
        if bar is not None:
            bar.visible = False
        self._stop_video_playback()

    def _stop_video_playback(self) -> None:
        if self._media is None or self._video_state not in {"playing", "paused"}:
            return
        self._video_state = "idle"
        self.ui_call(self._media.stop_playback())

    def _interrupt_video(self) -> None:
        """换书：上一本的播放列表、面板与播放器一起收掉。"""
        self._video_items = []
        self._video_index = -1
        bar = self._video_bar()
        if bar is not None:
            bar.visible = False
            bar.set_playlist([], -1)
        self._stop_video_playback()

    def open_video_playlist(self, _=None) -> None:
        """播放列表弹层：列表与行由视图搭，show/pop 归控制器（同设置弹层）。"""
        page = self._page()
        bar = self._video_bar()
        if page is None or bar is None:
            return
        self._video_dialog = bar.build_playlist_dialog(
            self._pick_video, self.close_video_playlist)
        page.show_dialog(self._video_dialog)
        self.flush()

    def close_video_playlist(self, _=None) -> None:
        if self._video_dialog is None:
            return
        page = self._page()
        self._video_dialog = None
        if page is not None:
            try:
                page.pop_dialog()
            except Exception:
                pass
        self.flush()

    def _pick_video(self, index) -> None:
        """弹层里点一条：先收弹层再播（弹层跟着面板走，别留在屏幕上）。"""
        self.close_video_playlist()
        try:
            index = int(index)
        except (TypeError, ValueError):
            return
        if 0 <= index < len(self._video_items):
            self.play_video(self._video_items[index]["url"])

    def _show_video(self, state: str, index: int = -1, detail: str = "") -> None:
        """把服务态写回面板：状态、当前条标题、上下条可用性一起跟着走。"""
        bar = self._video_bar()
        if bar is None:
            return
        if index < 0:
            index = self._video_index
        items = self._video_items
        if 0 <= index < len(items):
            bar.set_playlist(items, index)
            title = str(items[index].get("title") or f"视频 {index + 1}")
        else:
            title = ""
        self._video_state = state
        self._video_index = index
        bar.set_state(state, chapter=title, detail=detail)

    async def _on_media_play_state(self, event: MediaPlayStateEvent) -> None:
        """服务态镜像到面板：只认当前这本书的事件。

        和朗读同一处境——换书时上一条会话的 idle 可能后到，照单渲染会让面板
        在"正在播放"和"未开始"之间来回跳。
        """
        if str(getattr(event, "book_url", "") or "") != self._book_url:
            return
        try:
            index = int(getattr(event, "chapter", 0) or 0)
        except (TypeError, ValueError):
            return
        self._show_video(str(getattr(event, "state", "") or "idle"), index)

    # ------------------------------------------------------------------ #
    # 退出阅读（试读收尾：Legado「返回时提示放入书架」）
    # ------------------------------------------------------------------ #
    def exit_reader(self, _=None) -> None:
        """退出阅读：试读暂存的书先问要不要入架，其余直接收尾。"""
        self.close_settings()   # 设置弹层不能跟着离开阅读器还挂在页面上
        record = self._reading.staged_record()
        if record is None:
            self.ui_call(self._finish_session(None))
            return
        self._ask_about_staged_book(record)

    def _ask_about_staged_book(self, record: dict) -> None:
        """试读退场询问：放入=原地转正（进度保留），不放入=记录/进度/缓存一起删。"""
        page = self._page()
        if page is None:  # 问不出来就别乱删：暂存记录原样留着，交给启动清理
            self.ui_call(self._finish_session(None))
            return
        name = str(record.get("name") or "这本书")
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("放入书架"),
            content=ft.Column([ft.Text(
                f"是否将《{name}》放入书架？\n"
                "不放入会删除这本书的阅读进度与缓存。")], tight=True),
            actions=[
                ft.TextButton("不放入",
                              on_click=lambda e: self._resolve_staged(False)),
                ft.Button("放入书架",
                          on_click=lambda e: self._resolve_staged(True)),
            ],
        )
        page.show_dialog(dialog)

    def _resolve_staged(self, shelve: bool) -> None:
        page = self._page()
        if page is None:  # 询问框还开着就切了页
            return
        try:
            page.pop_dialog()
        except Exception:
            pass
        self.ui_call(self._finish_session(shelve))

    async def _finish_session(self, shelve: bool | None) -> None:
        """收尾会话并离开阅读器。

        ``shelve`` 三态：``None`` 普通书、``True`` 试读转正、``False`` 试读丢弃。
        转正/丢弃必须排在 :meth:`close_book` **之前**——关会话会把当前书源与书
        清空，之后服务再也认不出那条暂存记录。
        """
        if self._view is None:
            return
        try:
            if shelve is True:
                await self._reading.promote_staged_book()
            elif shelve is False:
                await self._reading.discard_staged_book()
            await self._reading.close_book(save_progress=shelve is not False)
        except Exception as exc:
            logger.warning("退出阅读收尾失败：%s", exc)
        if self._view is None:  # 在途时用户已切页
            return
        # 试读（不管放没放）落回找书页，其余回书架
        leave = (self.on_back_to_find_books if shelve is not None
                 else self.on_back_to_bookshelf)
        if leave is not None:
            leave()

    def _page(self):
        if self._view is None:
            return None
        try:
            return self._view.page
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    async def _on_session_changed(self, event: ReadingSessionChangedEvent) -> None:
        self._book_url = event.book_url
