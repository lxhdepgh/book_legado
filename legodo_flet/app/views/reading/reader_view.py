"""ReaderView：阅读器（目录 / 正文排版 / 滚动续读 / 偏好 / 沉浸模式 / 快捷键 / 上下章）。

自旧单体 ``ReadingView`` 的 reader 职责拆出（§7.2）：目录、正文渲染、滚动续读、
阅读器偏好、沉浸模式（收起本视图的工具栏与状态栏）、快捷键、上一章/下一章。面板（书签
:class:`BookmarkPanel`、书内搜索 :class:`ContentSearchBar`）作为覆盖层嵌入。

排版（切段/小标题/行距/缩进/版心/背景）是纯显示层的活：正文原文照旧进缓存，
只有挂到屏幕上的那一份被 :meth:`ReaderView.novel_paragraphs` 切过。

本视图只做 Flet 布局与渲染：目录列表、正文宿主、工具栏、状态栏；目录/正文加载、
进度保存、书签/书内搜索编排、偏好读写与设置弹层的开关全部在
:class:`~app.controllers.reader_controller.ReaderController`。
划词取词同理：正文段 selectable，选中后本视图只亮出「查词典」条（查询与释义
弹窗编排在控制器，见 :meth:`set_selection`）。

红线（§7.5）：本文件**不 import 任何 app.services.***。正文文档对象鸭子类型读取
``.blocks``（每项 ``.kind`` / ``.value``），不 import reading_engine 类型。
"""
from __future__ import annotations

import asyncio
import math
import re
from bisect import bisect_right

import flet as ft

from app.core.text_layout import paragraph_lines
from app.views.reading.bookmark_panel import BookmarkPanel
from app.views.reading.content_search_bar import ContentSearchBar
from app.views.reading.video_play_bar import VideoPlayBar

#: 正文段/小标题的标记：改字号行距要就地重排已挂出来的控件（见 apply_preferences）。
_TEXT_BLOCK = "reader-text"
_HEADING_BLOCK = "reader-heading"
#: 旧阅读器的字号可用区间（再小看不清、再大一行剩不了几个字）。
_MIN_FONT_SIZE = 13
_MAX_FONT_SIZE = 28

#: 排版默认值，对齐旧 ``ReadingView._load_reader_preferences``：书源正文密不透风，
#: 1.7 行距 + 段首缩进 + 760 版心就是"能看下去"和"一堵墙"的差别。
DEFAULT_PREFERENCES: dict = {
    "font_size": 16,
    # 字族取自「更多工具 → 字体」页选中的系统字体（空＝跟随主题默认）。
    # 同一个键 reader.font_family 也是 FontService 的读写键，两边不能各写各的。
    "font_family": "",
    "line_height": 1.7,
    "paragraph_spacing": 12,
    "max_width": 760.0,
    "indent": True,
    "text_align": "start",
    "background": "follow",
    "show_status": True,
    # 正文可否被选中（划词查词典的开关）。关掉是刚需：按住拖动选中与滚动
    # 在同一块正文上抢手势时，用户要能一句"别选了"把它拧下去。
    "text_selection": True,
}
#: 版心宽度只认这三档（旧实现同规则：手改设置表的怪值一律退回"宽"）。
_WIDTH_CHOICES = {620.0, 700.0, 760.0}
#: 书签/书内搜索那条抽屉的设计宽。**不需要**按窗宽再夹一道：它是 Stack 里的一位，
#: 客户端对 Stack 子项与对弹层一样夹到父框宽——320 塞进 304 那一档时实测面板宽 284
#: （= Stack 自己的宽），而且头部那行的 `+`/`✕` 跟着面板一起右移同样的像素，
#: 说明内容是**照窄框重排**、不是画在 320 上再切掉右边（第七十七轮两臂像素，
#: `.probe/probe_side_clamp77.py`）。
_SIDE_PANEL_WIDTH = 320
#: 会把已挂出的正文**重排**的偏好键：这些变了，裸像素就不再指向原来那一段（#87）。
#: 其余键（背景、状态栏、划词开关）只改颜色与可见性，像素位置原样不动。
_REFLOW_KEYS = ("font_size", "line_height", "paragraph_spacing", "max_width",
                "indent", "text_align", "font_family")
#: 阅读背景 → (正文底色, 正文颜色)。夜间/墨水屏不是好不好看，是室外能不能读。
_BACKGROUND_PALETTE: dict[str, tuple[str | None, str | None]] = {
    "follow": (None, None),
    "paper": ("#F5EEDC", "#332F27"),
    "eye": ("#DCE8D5", "#243025"),
    "night": ("#151515", "#D8D8D8"),
    "eink": ("#FFFFFF", "#000000"),
}

#: 小节标题（"第三百二十章 xxx"/"序章"/"后记"…）：识别出来才加粗、不加缩进。
_HEADING_PATTERN = re.compile(
    r"^(?:第[0-9０-９零〇一二三四五六七八九十百千万两]+"
    r"[章节卷回部篇集]|卷[0-9０-９零〇一二三四五六七八九十百千万两]+|"
    r"序章|序言|前言|楔子|引子|后记|尾声|番外)(?:\s|$|[：:、.-])")
#: 已经自带缩进/项目符号的行不再叠一层"　　"。
_INDENT_KEEP_PREFIXES = ("　　", "•", "·", "- ", "— ")
#: 项目符号行（切段时不缩进；重排时"　　"已被剥掉，所以单列一份）。
_BULLET_PREFIXES = ("•", "·", "- ", "— ")



async def _await_coroutine(coroutine):
    """把已经建好的协程交给 ``Page.run_task``（它只认协程函数）。"""
    return await coroutine


class ReaderView(ft.Column):
    """阅读器：目录页 + 正文页叠放；沉浸模式只收放本视图自己的工具栏与状态栏。"""

    #: 排版偏好的键名清单。挂在类上给 Controller 用（键表以视图为准），
    #: 控制器 import 本包会撞上 ``views/reading/__init__`` 的反向 import（循环）。
    PREFERENCE_KEYS: tuple[str, ...] = tuple(DEFAULT_PREFERENCES)

    #: 目录一次建多少行；离底多少像素就补下一批（几千章的书一次建完会冻住界面）。
    DIRECTORY_BATCH_SIZE = 50
    DIRECTORY_LOAD_AHEAD = 320
    #: 目录行高与行距（行距只能挂在 ListView 上：0.86 的 ListView 用 spacing 顶替
    #: divider）。定位按像素滚，``scroll_to`` 在这一版对 ListView 的 scroll_key
    #: 完全不动（见 docs/dev-logs/2026-09-26.md 第十轮探针），所以行高**必须钉死**：
    #: 行高一致时 第 n 行的 offset 才等于 n * (行高 + 行距)。
    DIRECTORY_ROW_HEIGHT = 44
    DIRECTORY_ROW_SPACING = 2

    @property
    def _directory_pitch(self) -> float:
        """一行的竖直周期 = 行高 + 行距。"""
        return float(self.DIRECTORY_ROW_HEIGHT + self.DIRECTORY_ROW_SPACING)

    def __init__(self) -> None:
        super().__init__(spacing=0, expand=True)

        # -- 回调槽 --
        self.on_open_chapter = None       # (chapter: dict) -> None
        self.on_prev_chapter = None       # () -> None
        self.on_next_chapter = None       # () -> None
        self.on_toggle_immersive = None   # () -> None
        self.on_change_font_size = None   # (delta: int) -> None
        self.on_save_scroll = None        # () -> None（Controller 内 0.5s 节流）
        self.on_open_directory = None    # () -> None
        self.on_close_directory = None   # () -> None
        self.on_key = None                # (event) -> None
        self.on_open_bookmarks = None    # () -> None
        self.on_open_search = None       # () -> None
        self.on_open_settings = None     # () -> None（阅读设置弹层：行距/背景/版心…）
        self.on_exit_reader = None      # () -> None（试读退出会先问是否入架）
        self.on_repair_source = None     # () -> None（章节加载失败后的自愈入口）
        self.on_lookup_selection = None  # (word: str) -> None（划词条上的「查词典」）
        self.on_play_video = None       # (url: str) -> None（正文视频条目上的「在本页播放」）

        # 排版偏好先建：控件按默认值起稿，Controller 读到存档偏好后再 apply（就地重排）
        self._preferences = dict(DEFAULT_PREFERENCES)
        self._font_size = int(self._preferences["font_size"])

        # ------------------------------------------------------------------ #
        # 目录页
        # ------------------------------------------------------------------ #
        self._directory_status = ft.Text("尚未打开书籍", size=12, expand=True)
        self._directory_query = ft.TextField(
            hint_text="搜索章节", prefix_icon=ft.Icons.SEARCH,
            dense=True, height=42, expand=True,
            on_change=lambda e: self._apply_directory_filter())
        # 目录**分批渲染**：网文源动辄几千章，一次建完 ListTile 会把界面冻住。
        # 滚到底附近再补一批（见 _on_directory_scroll），与漫画页的懒加载分块同理。
        self._directory_list = ft.ListView(
            expand=True, spacing=self.DIRECTORY_ROW_SPACING,
            on_scroll=self._on_directory_scroll)
        # 覆盖层必须不透明：Stack 里的目录页若透明，正文会直接从章节列表背后
        # 透出来（两层同时可见），看起来就像"点了章节，列表叠在正文上"。
        # 底色只能挂在 Container 上（0.86 的 Column 没有 bgcolor），与本视图
        # _side_host、manga_reader_view 的目录覆盖层同用 SURFACE。
        self._detail_page = ft.Container(expand=True, visible=False,
                                         bgcolor=ft.Colors.SURFACE,
                                         content=ft.Column(expand=True, spacing=0, controls=[
            ft.Container(padding=ft.Padding(4, 2, 8, 2), content=ft.Row([
                ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回阅读",
                              on_click=lambda e: self._fire(self.on_close_directory)),
                self._directory_status,
                ft.IconButton(ft.Icons.SWAP_VERT, tooltip="倒序/正序",
                              on_click=lambda e: self.toggle_directory_order()),
            ])),
            ft.Container(padding=ft.Padding(8, 0, 8, 4),
                         content=self._directory_query),
            ft.Divider(height=1),
            self._directory_list,
        ]))

        # ------------------------------------------------------------------ #
        # 正文页
        # ------------------------------------------------------------------ #
        self._title = ft.Text("正文", size=22, weight=ft.FontWeight.BOLD)
        self._content_host = ft.Column(
            spacing=float(self._preferences["paragraph_spacing"]))
        # 版心的渲染尺寸是段级定位唯一的锚（#86 取证四：挂完布局就自己报上来，
        # 不用先滚一次——先滚一次会把续读进度写歪）。
        self._content_size: tuple[float, float] = (0.0, 0.0)
        # 正文区当前像素与"重排后回位"的挂单：像素在两套装尺之间不是不变量，
        # 重排前先把它换算成段号，客户端报来新的版心高后再按新尺子滚回去
        # （#87 取证一：16→20 号字号原地重排，像素不动等于把用户扔退 49 段）。
        self._last_scroll_pixels = 0.0
        #: ``(段号, 重排前的版心高, 重排前的段数)``；高一变就回位一次并清空。
        self._relocate_pending: tuple[int, float, int] | None = None
        self._content_host.size_change_interval = 100
        self._content_host.on_size_change = self._on_content_size
        self._reader_document = ft.Container(
            width=float(self._preferences["max_width"]),
            padding=ft.Padding(16, 20, 16, 28),
            on_click=lambda e: self._fire(self.on_toggle_immersive),
            content=ft.Column([self._title, ft.Divider(), self._content_host],
                              spacing=12))
        self._reader_scroll = ft.Column(
            expand=True, scroll=ft.ScrollMode.AUTO, scroll_interval=200,
            on_scroll=self._on_document_scroll,
            controls=[ft.Row([self._reader_document],
                             alignment=ft.MainAxisAlignment.CENTER)])
        # 进度标签定宽 + 省略号，不用 ``expand``：工具条那一行是 ``wrap=True``，而
        # 换行的 Row 里任何子项带 ``expand``（**连 ``expand=False`` 都算**）会让整条
        # 行在 release 客户端画成一块灰（当场量的，见 `.probe/probe_wrap_expand76.py`
        # 的 ``wrap_expand_*`` 两臂）。定宽也顺手治了窄窗里"一个字一行"的那一拍。
        self._progress_label = ft.Text(
            "未选择章节", size=12, width=200, no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS, text_align=ft.TextAlign.CENTER)
        self._prev_btn = ft.IconButton(
            ft.Icons.CHEVRON_LEFT, tooltip="上一章", disabled=True,
            on_click=lambda e: self._fire(self.on_prev_chapter))
        self._next_btn = ft.IconButton(
            ft.Icons.CHEVRON_RIGHT, tooltip="下一章", disabled=True,
            on_click=lambda e: self._fire(self.on_next_chapter))
        self._font_size_label = ft.Text(str(self._font_size), size=12)
        # 沉浸模式**必须有按钮**：正文段是可划词的 selectable Text，点正文的那一下
        # 被文本控件吃掉（实测：点版心既不切沉浸，还把整段亮到划词条上），
        # `_reader_document.on_click` 只在「划词可选」关掉时才真的收得到。
        self._immersive_btn = ft.IconButton(
            ft.Icons.FULLSCREEN, tooltip="沉浸模式",
            on_click=lambda e: self._fire(self.on_toggle_immersive))
        # 朗读入口与「书内搜索」「阅读设置」并排（第二十九轮：用户要求朗读放到
        # 区域上方、和这两个动作放在一起）。它管**收起/展开**顶部那条朗读面板——
        # 面板挂在工具栏正下方（见 ``_reader_page``），默认展开；不给一颗收起的键，
        # 就等于在正文上方永久占一条高度。
        # 只有一条不让坏：**正在念的时候那一条不许藏着**（桌面模式没有通知栏，
        # 收起之后屏幕上就没有能按停的地方）。这条规则只写在 :meth:`sync_read_aloud`
        # 一处：speaking 时它把面板钉成展开，同时把这颗键禁用并写明原因——禁用而不
        # 说原因，用户只会以为它坏了。
        self._read_aloud_open = True
        self._read_aloud_available = False
        self._read_aloud_btn = ft.IconButton(
            ft.Icons.GRAPHIC_EQ, tooltip="朗读",
            on_click=lambda e: self.toggle_read_aloud_panel())
        self._toolbar = ft.Container(
            visible=True, padding=ft.Padding(6, 2, 6, 2),
            content=ft.Row([
                ft.IconButton(ft.Icons.CLOSE, tooltip="退出阅读",
                              on_click=lambda e: self._fire(self.on_exit_reader)),
                self._prev_btn,
                # 三个"开面板"动作放在一起：目录整页覆盖，书签/检索同一个左抽屉
                ft.IconButton(ft.Icons.FORMAT_LIST_BULLETED, tooltip="目录",
                              on_click=lambda e: self._fire(self.on_open_directory)),
                ft.IconButton(ft.Icons.BOOKMARK_OUTLINED, tooltip="书签",
                              on_click=lambda e: self._fire(self.on_open_bookmarks)),
                ft.IconButton(ft.Icons.SEARCH, tooltip="书内搜索",
                              on_click=lambda e: self._fire(self.on_open_search)),
                self._read_aloud_btn,
                ft.IconButton(ft.Icons.SETTINGS_OUTLINED, tooltip="阅读设置",
                              on_click=lambda e: self._fire(self.on_open_settings)),
                self._immersive_btn,
                self._progress_label,
                ft.IconButton(ft.Icons.TEXT_DECREASE, tooltip="减小字号",
                              on_click=lambda e: self._fire(
                                  self.on_change_font_size, -1)),
                self._font_size_label,
                ft.IconButton(ft.Icons.TEXT_INCREASE, tooltip="增大字号",
                              on_click=lambda e: self._fire(
                                  self.on_change_font_size, 1)),
                self._next_btn,
            ], spacing=0, run_spacing=0, wrap=True))
        self._status_bar = ft.Container(
            padding=ft.Padding(12, 2, 12, 4),
            content=ft.Text("", size=11, text_align=ft.TextAlign.CENTER,
                            color=ft.Colors.ON_SURFACE_VARIANT))
        # ------------------------------------------------------------------ #
        # 章节加载失败的补救条（平时整条不可见）。
        #
        # 只把"加载失败：xxx"写进进度标签，等于让用户读到一个死信：既不知道该
        # 重试还是换源，也够不到自愈功能（RuleRepairService 在 app/ 里曾经零调用）。
        # 这一条把失败原因摊开，并给出那一个动作。
        # ------------------------------------------------------------------ #
        self._repair_hint = ft.Text("", size=12, expand=True)
        self.repair_offer = ft.Container(
            visible=False, padding=ft.Padding(12, 6, 12, 6),
            bgcolor=ft.Colors.ERROR_CONTAINER,
            content=ft.Row([
                ft.Icon(ft.Icons.BUILD_OUTLINED, size=18,
                        color=ft.Colors.ON_ERROR_CONTAINER),
                self._repair_hint,
                ft.TextButton("自动修复该书源", on_click=lambda e: self._fire(
                    self.on_repair_source)),
                ft.IconButton(ft.Icons.CLOSE, icon_size=16, tooltip="先不管",
                              on_click=lambda e: self.hide_repair_offer()),
            ], spacing=6))
        # 朗读迷你播放条（设计 §1.1：桌面无通知栏，播放控制全靠这条）。开了一章
        # 才由控制器亮出来——没书的时候它没话可说。
        # 第二十九轮挪到**工具栏正下方**（用户："朗读功能放到区域上方，和书内
        # 搜索、设置放在一起"）：它和那两颗按钮是同一块"阅读时手要够到的控制区"，
        # 沉在窗口底部时它贴着状态栏，离自己的入口有一屏远。
        self.read_aloud = ReadAloudBar()
        # 视频书面板（§1.4）：放工具栏下面而不是底部——正文一直要滚，播放器跟着滚
        # 就没法边看边放；它自己也是"控制器亮了才可见"。
        self.video_play = VideoPlayBar()
        # ------------------------------------------------------------------ #
        # 划词条：选中正文后亮出来，只**显示**选中的词，不顺手查。
        #
        # 一次拖拽会发几十次 selection_change，每条都去抓释义页等于对着词典站点
        # 做压测；查询只发生在点「查词典」的那一刻（控制器那一侧）。
        # ------------------------------------------------------------------ #
        self._selection_word = ""
        #: 当前释义弹窗里的正文控件（查词是异步的，结果回来要回填这一条）。
        self._definition_body = None
        self._selection_label = ft.Text("", size=12, expand=True)
        self.selection_bar = ft.Container(
            visible=False, padding=ft.Padding(12, 4, 8, 4),
            bgcolor=ft.Colors.SECONDARY_CONTAINER,
            content=ft.Row([
                ft.Icon(ft.Icons.SEARCH, size=16,
                        color=ft.Colors.ON_SECONDARY_CONTAINER),
                self._selection_label,
                ft.TextButton("查词典",
                              on_click=lambda e: self._fire(
                                  self.on_lookup_selection, self._selection_word)),
                ft.IconButton(ft.Icons.CLOSE, icon_size=16, tooltip="取消选中",
                              on_click=lambda e: self.clear_selection()),
            ], spacing=6))
        self._reader_page = ft.Column(
            expand=True, spacing=0,
            controls=[self._toolbar, self.read_aloud, self.video_play,
                      self._reader_scroll, self.selection_bar,
                      self.repair_offer, self._status_bar])

        # ------------------------------------------------------------------ #
        # 左侧抽屉：书签 + 书内搜索合在一个面板里（两块同类列表不再互相顶掉）
        #
        # 旧实现把它们塞进同一个 Container 的 content 上换上换下：开搜索就看不见
        # 书签、加完书签要再点一次才能搜索。合并成页签后抽屉只开一次，两件事在同
        # 一处做完。目录仍是整页覆盖（旧架构 ``ReadingView._show_section`` 同款）。
        # ------------------------------------------------------------------ #
        self.bookmark_panel = BookmarkPanel()
        self.content_search_bar = ContentSearchBar()
        self._side_pages = ("bookmark", "search")
        self._side_tabs = ft.Tabs(
            length=len(self._side_pages), selected_index=0, expand=True,
            content=ft.Column(spacing=0, expand=True, controls=[
                ft.TabBar(tabs=[
                    ft.Tab(label="书签", icon=ft.Icons.BOOKMARK_OUTLINED),
                    ft.Tab(label="书内搜索", icon=ft.Icons.SEARCH),
                ]),
                ft.TabBarView(expand=True, controls=[
                    ft.Container(expand=True, padding=ft.Padding(8, 0, 8, 0),
                                 content=self.bookmark_panel),
                    ft.Container(expand=True, padding=ft.Padding(8, 0, 8, 0),
                                 content=self.content_search_bar),
                ]),
            ]))
        self._side_host = ft.Container(
            visible=False, width=_SIDE_PANEL_WIDTH, bgcolor=ft.Colors.SURFACE,
            content=self._side_tabs)

        # ------------------------------------------------------------------ #
        # 沉浸模式的出口（右下角浮标）
        #
        # 工具栏自己就是被沉浸藏掉的东西，所以"退出沉浸"不能只挂在工具栏那颗按钮
        # 上——那点正文本来也换不回点击：正文段 selectable，点击被文本控件吃掉。
        # 浮标常驻在正文之上，只有沉浸模式亮着，非沉浸时整块不可见（不挡阅读）。
        # ------------------------------------------------------------------ #
        self._immersive_exit = ft.Container(
            visible=False, right=18, bottom=76, padding=ft.Padding(2, 0, 2, 0),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, border_radius=22,
            content=ft.IconButton(ft.Icons.FULLSCREEN_EXIT,
                                  tooltip="退出沉浸模式",
                                  on_click=lambda e: self._fire(
                                      self.on_toggle_immersive)))

        self._host = ft.Stack(expand=True, controls=[
            self._reader_page, self._detail_page, self._side_host,
            self._immersive_exit,
        ])
        self.controls = [self._host]

        self._immersive = False
        self._directory_reversed = False
        #: 目录分批渲染的三份状态：原始目录 / 过滤排序后的可见表 / 已挂出行数。
        self._directory_all: list[dict] = []
        self._directory_visible: list[dict] = []
        self._directory_rendered = 0
        #: 当前章的 url：既用于打开目录时定位，也用于把那一行标成"在读"。
        self._directory_current_url = ""

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 渲染接口（ReaderController 调用）
    # ------------------------------------------------------------------ #
    def render_chapters(self, chapters: list[dict]) -> None:
        """收下整份目录并只画第一批：几千章的书一次建完会卡住界面。"""
        self._directory_all = list(chapters or [])
        self._apply_directory_filter()

    def _apply_directory_filter(self) -> None:
        """按关键字过滤 + 正/倒序，重挂第一批控件。

        过滤与排序都只动"看得见的那份列表"，``_directory_all`` 始终是原始目录，
        清空搜索框就能原样回来。
        """
        keyword = str(self._directory_query.value or "").strip().lower()
        items = [c for c in self._directory_all
                 if not keyword or keyword in str(c.get("name", "")).lower()]
        if self._directory_reversed:
            items = list(reversed(items))
        self._directory_visible = items
        self._directory_rendered = 0
        self._directory_list.controls.clear()
        self._append_chapter_controls()

    def _chapter_row(self, chapter: dict) -> ft.Control:
        """一行目录：卷标题是分组标题，普通章可点。

        行高钉死、标题单行截断：定位是按 ``行号 × 周期`` 算像素的，让某一行因为
        章名太长自己撑高，它下面所有行的落点就全偏了。
        """
        if chapter.get("is_volume"):
            return ft.Container(
                height=self.DIRECTORY_ROW_HEIGHT, alignment=ft.Alignment.CENTER_LEFT,
                content=ft.Text(chapter.get("name", ""),
                                weight=ft.FontWeight.W_600, size=13, italic=True))
        current = bool(chapter.get("url")) and \
            str(chapter.get("url")) == self._directory_current_url
        return ft.ListTile(
            height=self.DIRECTORY_ROW_HEIGHT,
            dense=True,
            selected=current,
            leading=ft.Icon(ft.Icons.PLAY_ARROW,
                            color=ft.Colors.PRIMARY) if current else None,
            title=ft.Text(chapter.get("name", ""), size=13, max_lines=1,
                          overflow=ft.TextOverflow.ELLIPSIS,
                          color=ft.Colors.PRIMARY if current else None,
                          weight=ft.FontWeight.W_600 if current else None),
            on_click=lambda e, c=chapter: self._fire(self.on_open_chapter, c),
        )

    def _append_chapter_controls(self) -> int:
        """追加渲染下一批目录行，返回新增条数。"""
        start = self._directory_rendered
        end = min(start + self.DIRECTORY_BATCH_SIZE, len(self._directory_visible))
        for position in range(start, end):
            self._directory_list.controls.append(
                self._chapter_row(self._directory_visible[position]))
        self._directory_rendered = end
        self._update_directory_status(len(self._directory_visible))
        return end - start

    def _update_directory_status(self, total: int) -> None:
        if not total:
            self._directory_status.value = "没有匹配的章节" if self._directory_all \
                else "尚未打开书籍"
        elif self._directory_rendered >= total:
            self._directory_status.value = f"共 {total} 章"
        else:
            self._directory_status.value = f"已加载 {self._directory_rendered}/{total} 章"

    def _on_directory_scroll(self, event) -> None:
        """目录滚到快到底：把下一批补上（离底 DIRECTORY_LOAD_AHEAD 像素就触发）。"""
        pixels = float(getattr(event, "pixels", 0.0) or 0.0)
        extent = float(getattr(event, "max_scroll_extent", 0.0) or 0.0)
        if self._directory_rendered >= len(self._directory_visible) or extent <= 0:
            return
        if pixels >= extent - self.DIRECTORY_LOAD_AHEAD:
            self._append_chapter_controls()

    def toggle_directory_order(self) -> None:
        self._directory_reversed = not self._directory_reversed
        # 只翻标志位等于"点了没反应"（要等下次开书才生效）：排序就地重挂
        self._apply_directory_filter()

    # ------------------------------------------------------------------ #
    # 目录定位到当前章
    # ------------------------------------------------------------------ #
    def focus_chapter(self, url: str) -> None:
        """打开目录时把视线放到当前章：停在列表顶上时，读到第 2000 章的人
        得自己一路滚过去（旧目录一次建完反而"看得见在读哪"）。

        分批渲染意味着目标行往往还没建出来，所以补一批、下发一次、让出一次
        循环——一口气补到第 2000 行就是"点开目录卡住"，正是分批要避免的那件事。
        """
        url = str(url or "")
        if url != self._directory_current_url:
            # 高亮挂在行上：只存标志不重挂，已经挂出的那批永远标不出来
            self._directory_current_url = url
            self._apply_directory_filter()
        self._run(self._locate_chapter(url))

    def _directory_position(self, url: str) -> int:
        """当前章在**可见表**里的行号：过滤/倒序后行号会变，只认 url。"""
        if not url:
            return -1
        for i, chapter in enumerate(self._directory_visible):
            if not chapter.get("is_volume") and str(chapter.get("url") or "") == url:
                return i
        return -1

    async def _locate_chapter(self, url: str) -> None:
        position = self._directory_position(url)
        if position < 0:
            # 搜索框里还留着关键字，当前章被过滤掉了：滚不动就别动滚动
            return
        while self._directory_rendered <= position:
            before = self._directory_rendered
            self._append_chapter_controls()
            if self._directory_rendered <= before:
                return
            try:
                self.update()
            except Exception:
                # 让出循环的路上切了页：控件脱离页面，没地方滚也没地方看
                return
            await asyncio.sleep(0)
        self._run(self._directory_list.scroll_to(
            offset=position * self._directory_pitch, duration=0))

    def render_content(self, document, append: bool = False,
                       chapter_name: str = "") -> None:
        """渲染正文（``append=True`` 接到文末，见 :meth:`render_plan`）。

        同步版：小章节直接在界面循环上切段即可。自动续读多章累积起来一章几千行
        时改用 :meth:`content_plan` + :meth:`render_plan`，切分放到后台线程。
        """
        self.render_plan(self.content_plan(document), append=append,
                         chapter_name=chapter_name)

    def content_plan(self, document) -> list[tuple[str, object, str]]:
        """正文文档 → ``(kind, payload, meta)`` 计划：纯计算，不碰 Flet 控件。

        切段是整章规模的正则+逐行处理，压在界面循环上就是几十上百毫秒的卡顿
        （旧实现同样丢给线程跑），所以计划在这里算、控件在 :meth:`render_plan`
        里建。text 的 payload 是 ``[(段落, 是否标题), …]``；媒体块的 payload 是
        原 block，meta 带 book 级图片样式。
        """
        indent = bool(self._preferences["indent"])
        image_style = str(getattr(document, "image_style", "") or "").upper()
        plan: list[tuple[str, object, str]] = []
        for block in getattr(document, "blocks", None) or []:
            kind = str(getattr(block, "kind", "") or "")
            value = getattr(block, "value", "") or ""
            if not value:
                continue
            if kind == "text":
                paragraphs = self.novel_paragraphs(value, indent)
                if paragraphs:
                    plan.append(("text", paragraphs, ""))
            elif kind in {"image", "audio", "video"}:
                plan.append((kind, block, image_style))
        return plan

    @classmethod
    def novel_paragraphs(cls, value, indent: bool = False) -> list[tuple[str, bool]]:
        """把引擎给的一大段正文切成自然段，并认出其中夹着的小节标题。

        书源正文经常整章只有一两个 ``<p>``：不切段就是一堵密不透风的墙，章节标题
        也淹没在正文中间。只影响显示，不改数据（原文照旧进缓存）。

        切段本身取自 :func:`app.core.text_layout.paragraph_lines`——书内全文索引按
        **同一份**切法数段落，命中第 k 段才能滚到屏幕上的第 k 段。
        """
        paragraphs: list[tuple[str, bool]] = []
        for line in paragraph_lines(value):
            is_heading = (len(line) <= 48
                          and bool(_HEADING_PATTERN.match(line)))
            if (indent and not is_heading
                    and not line.startswith(_INDENT_KEEP_PREFIXES)):
                line = f"　　{line.lstrip('　')}"
            paragraphs.append((line, is_heading))
        return paragraphs

    def render_plan(self, plan, append: bool = False,
                    chapter_name: str = "") -> None:
        """把 :meth:`content_plan` 的结果挂成正文控件。

        ``append=True`` 是"读到章尾自动接下一章"的那条路：新章节**接**在当前文档
        尾部（分隔线 + 章名 + 正文），而不是换掉它——正文因此是连续滚动的一长串，
        用户永远不需要点"下一章"。整篇替换会连带把滚动位置打回顶部，看起来就像
        书自己跳回了第一章。
        """
        target = self._content_host.controls
        if not append:
            del target[:]
            # 上一张文档的渲染高就此作废：整篇换章后它还挂在 `_content_size` 上时，
            # 段级定位会拿旧高度把用户滚到别的段（客户端要重新报一次尺寸）。
            self._content_size = (0.0, 0.0)
            # 像素与回位挂单同属上一张文档，留着会让下一次重排按旧书的段号回位。
            self._last_scroll_pixels = 0.0
            self._relocate_pending = None
        elif chapter_name:
            # 章节分隔标题不打正文标记：排版重排只认正文段。
            target.extend([
                ft.Divider(height=32),
                ft.Text(chapter_name, size=22, weight=ft.FontWeight.BOLD),
            ])
        for kind, payload, meta in plan or []:
            if kind == "text":
                for value, is_heading in payload:
                    target.append(self._paragraph_control(value, is_heading))
            elif kind == "image":
                target.append(ft.Image(
                    src=str(payload.value),
                    error_content=ft.Text("图片加载失败"),
                    **self._image_render_options(meta)))
            elif kind == "audio":
                target.append(self._audio_control(str(payload.value),
                                                  payload.title))
            elif kind == "video":
                target.append(self._video_control(payload))
        if not target:
            self._content_host.controls = [
                ft.Text("本章暂无正文", color=ft.Colors.GREY_500)]
        if not append:
            # 整篇换章 = 刚才选中的那段字已经不在这儿了：划词条留着只会让人对着一
            # 个看不见的选区点「查词典」。追加（自动接下一章）不动它。
            self.clear_selection()

    def _paragraph_control(self, value: str, is_heading: bool) -> ft.Text:
        """一段正文（或一行小标题）：排版参数全部取自 :attr:`_preferences`。"""
        background, foreground = _BACKGROUND_PALETTE[self._preferences["background"]]
        return ft.Text(
            value,
            selectable=bool(self._preferences["text_selection"]),
            on_selection_change=self._on_text_selection,
            size=self._font_size + (2 if is_heading else 0),
            font_family=self._preferences["font_family"] or None,
            weight=ft.FontWeight.W_600 if is_heading else None,
            style=ft.TextStyle(height=(1.45 if is_heading
                                       else float(self._preferences["line_height"]))),
            text_align=(ft.TextAlign.JUSTIFY
                        if self._preferences["text_align"] == "justify"
                        else ft.TextAlign.START),
            color=foreground,
            data=_HEADING_BLOCK if is_heading else _TEXT_BLOCK)

    def _on_text_selection(self, event) -> None:
        """正文选中变化：只把词亮到划词条上，不查（查询是点按钮那一刻的事）。

        同词早退：一次拖拽能发几十条事件，重复写同一条标签既没意义也是白刷。
        选中清空（点一下正文把光标挪走）会把条目收起来。

        **单击不算选词**（第二十八轮实测）：在 selectable 文本上点一下，事件带的是
        ``cause=TAP`` 和**整段**文本——用户没选任何东西，划词条却亮出来对着整段
        问要不要查词典。只认拖拽/双击/长按这类真选词。``cause`` 缺省（老版本或
        别的端）时按原样放行，宁可多亮一条也不把划词功能关掉。
        """
        word = str(getattr(event, "selected_text", "") or "").strip()
        if word and getattr(event, "cause", None) is ft.TextSelectionChangeCause.TAP:
            word = ""
        if word == self._selection_word:
            return
        self.set_selection(word)

    def set_selection(self, word: str) -> None:
        """亮出划词条：``word`` 为空等于收起。"""
        word = str(word or "").strip()
        self._selection_word = word
        # 条目窄，长段选中最多加一行就截断（完整词在点「查词典」之后才用得上）。
        self._selection_label.value = word[:60] + ("…" if len(word) > 60 else "")
        self.selection_bar.visible = bool(word)

    def clear_selection(self) -> None:
        self.set_selection("")

    def build_definition_dialog(self, word: str, text: str, status: str,
                                on_close) -> ft.Control:
        """释义弹窗：由视图**搭**控件，show/pop 归控制器（§7.2 视图拿不到生命周期）。

        先搭后填：查词要抓释义页（几秒），点完必须立刻有东西在转，否则用户只
        觉得"没点上"。回填走 :meth:`update_definition`。
        释义正文 selectable：查词的人下一步多半是想复制。
        """
        self._definition_body = ft.Text(status or text or "（无释义）",
                                        selectable=True, size=13)
        return ft.AlertDialog(
            title=ft.Text(f"查词典：{word}" if word else "查词典"),
            content=ft.Container(
                width=460, height=320,
                content=ft.Column([self._definition_body],
                                  tight=True, scroll=ft.ScrollMode.AUTO)),
            actions=[ft.TextButton("关闭", on_click=lambda e: on_close())],
        )

    def update_definition(self, text: str = "", status: str = "") -> None:
        """回填释义弹窗；弹窗没在屏幕上时是空操作（在途结果可能晚于关闭）。"""
        body = getattr(self, "_definition_body", None)
        if body is None:
            return
        body.value = status or text or "（无释义）"

    def _image_render_options(self, image_style: str) -> dict:
        """Book.imgStyle 的三档映射（DEFAULT/FULL/TEXT），照旧贴进渲染器。"""
        style = str(image_style or "").upper()
        if style == "FULL":
            return {"width": float("inf"), "fit": ft.BoxFit.FIT_WIDTH}
        if style == "TEXT":
            return {"height": float(self._font_size), "fit": ft.BoxFit.CONTAIN}
        return {"fit": ft.BoxFit.SCALE_DOWN}

    def _audio_control(self, src: str, title: str) -> ft.Control:
        """正文内嵌音频：播放器 + 标题 + 播放/暂停（旧阅读视图同款卡片）。"""
        import flet_audio as fta
        player = fta.Audio(src=src, autoplay=False)
        return ft.Card(content=ft.Container(padding=12, content=ft.Column([
            player,
            ft.Text(title or "音频"),
            ft.Row([
                ft.IconButton(ft.Icons.PLAY_ARROW, tooltip="播放",
                              on_click=lambda e, p=player: self._run(p.play())),
                ft.IconButton(ft.Icons.PAUSE, tooltip="暂停",
                              on_click=lambda e, p=player: self._run(p.pause())),
            ]),
        ])))

    def _video_control(self, block) -> ft.Control:
        """正文里的视频块 → 「在本页播放」条目，**不在这里内嵌播放器**。

        一个播放面（:class:`~app.views.reading.video_play_bar.VideoPlayBar`）由控制器
        经 ``MediaPlayService`` 驱动：内嵌一份 ``ftv.Video`` 就等于绕过服务——没有
        播放列表、没有"接着上次看"、切页也停不下来（这三条正是 §1.4 原来缺的）。
        防盗链站点的请求头跟着条目走，点「播放」时由控制器交给后端。
        """
        url = str(block.value)
        return ft.Card(content=ft.Container(
            padding=ft.Padding(12, 8, 8, 8),
            content=ft.Row([
                ft.Icon(ft.Icons.MOVIE_OUTLINED, size=22,
                        color=ft.Colors.PRIMARY),
                ft.Column([
                    ft.Text(getattr(block, "title", "") or "视频", size=14),
                    ft.Text(url, size=11, color=ft.Colors.GREY_600,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ], spacing=2, expand=True),
                ft.TextButton("在本页播放", on_click=lambda e: self._fire(
                    self.on_play_video, url)),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)))

    def _run(self, coroutine) -> None:
        """在界面循环上跑一个协程（未挂载/无头时把协程关掉，别留 never-awaited）。

        ``Page.run_task`` 只收**协程函数**再自己调用：直接把协程对象递过去会
        ``TypeError: handler must be a coroutine function``，于是滚动/播放动作
        一个都不会发生。这里用 :func:`_await_coroutine` 包一层。
        """
        try:
            page = self.page
        except Exception:
            # 控件还没上页面：``Control.page`` 抛的是 RuntimeError（不是
            # AttributeError），取不到页面就没地方跑这一趟。
            coroutine.close()
            return
        try:
            page.run_task(_await_coroutine, coroutine)
        except Exception:
            coroutine.close()

    def set_title(self, text: str) -> None:
        self._title.value = text or "正文"

    def set_progress(self, text: str) -> None:
        self._progress_label.value = text or ""

    def set_status_bar(self, text: str) -> None:
        self._status_bar.content.value = text or ""

    def show_repair_offer(self, text: str) -> None:
        """亮出"这一章读不出来"的补救条：一次失败只给一条可点的出路。"""
        self._repair_hint.value = text or "这一章读不出来"
        self.repair_offer.visible = True

    def hide_repair_offer(self) -> None:
        self.repair_offer.visible = False

    def set_nav_enabled(self, prev: bool, next_: bool) -> None:
        self._prev_btn.disabled = not prev
        self._next_btn.disabled = not next_

    # ------------------------------------------------------------------ #
    # 朗读：工具栏入口 + 顶部那条面板
    # ------------------------------------------------------------------ #
    def set_read_aloud_available(self, available: bool) -> None:
        """控制器写入"这一会儿到底能不能念"（有朗读服务 + 有可念的章）。

        不可用时入口整颗禁用：亮着按不动比不亮更让人以为是坏了。
        """
        self._read_aloud_available = bool(available)
        self.sync_read_aloud()

    def toggle_read_aloud_panel(self) -> None:
        """收起/展开那一条。**念着的时候收不起来**——规则在 :meth:`sync_read_aloud`。"""
        self._read_aloud_open = not self._read_aloud_open
        self.sync_read_aloud()

    def sync_read_aloud(self) -> None:
        """按"可用性 + 折叠 + 正在念"重算那一条和那颗键；朗读态只认 ``read_aloud.state``。"""
        speaking = self.read_aloud.state == "speaking"
        if speaking:
            # 念着的时候屏幕上必须有一条看得见的路能按停（桌面没有通知栏）：正在念
            # 就不允许那一条藏着。按钮同一时刻也被禁掉（下面），所以屏幕上不会出现
            # "看着能按、按下去没反应"的键。
            self._read_aloud_open = True
        self.read_aloud.visible = (self._read_aloud_available
                                   and self._read_aloud_open)
        btn = self._read_aloud_btn
        btn.icon = (ft.Icons.GRAPHIC_EQ if self._read_aloud_open
                    else ft.Icons.GRAPHIC_EQ_OUTLINED)
        btn.disabled = not self._read_aloud_available or speaking
        btn.tooltip = ("朗读中：先停止才能收起" if speaking
                       else ("收起朗读" if self._read_aloud_open else "展开朗读"))

    @property
    def preferences(self) -> dict:
        """当前排版偏好（副本：Controller 拿去落库）。"""
        return dict(self._preferences)

    def apply_preferences(self, preferences: dict | None = None) -> dict:
        """合并并套用排版偏好：版面参数 + 已挂出段落的就地重排。

        就地重排而不是重渲染：自动续读接上的多章文档一重渲染就换回"只有当前
        章"，用户改个行距被扔回第一章开头（旧实现同一条路径）。

        每个值都夹回可用区间：这些数字来自设置表，手改过的怪值（行距 0、宽度
        负数）会把正文挤成一条线，而不只是"难看一点"。

        重排会改动每段的高度，而滚动位置是按像素存的——像素不动等于把人挪段。
        所以重排前先在旧尺子下把像素换算成段号，等客户端报来新的版心高再滚回那
        一段（#87 取证一：300 段对话体从 16 号换 20 号，不补偿实测退 49 段）。
        """
        merged = dict(self._preferences)
        merged.update(preferences or {})
        # 换算必须在旧装尺还挂着的时候做：新字号一写进 `_preferences`，
        # `paragraph_at_pixel` 量的就是那把还没生效的新尺子了。
        if self._relocate_pending is not None:
            # 上一次重排还没兑现（新的版心高没报上来）：`_last_scroll_pixels` 还是
            # 那套更旧的尺子下的数，再换算一次就是把两套装尺混搭。段号才是不变量。
            anchor = self._relocate_pending[0]
        elif self._last_scroll_pixels > 0:
            anchor = self.paragraph_at_pixel(self._last_scroll_pixels)
        else:
            anchor = None
        reflowed = any(merged.get(key) != self._preferences.get(key)
                       for key in _REFLOW_KEYS)
        merged["font_size"] = self._clamped_int(
            merged.get("font_size"), _MIN_FONT_SIZE, _MAX_FONT_SIZE,
            DEFAULT_PREFERENCES["font_size"])
        merged["line_height"] = self._clamped_float(
            merged.get("line_height"), 1.3, 2.2,
            DEFAULT_PREFERENCES["line_height"])
        merged["paragraph_spacing"] = self._clamped_float(
            merged.get("paragraph_spacing"), 4, 24,
            DEFAULT_PREFERENCES["paragraph_spacing"])
        try:
            merged["max_width"] = float(merged.get("max_width"))
        except (TypeError, ValueError):
            merged["max_width"] = DEFAULT_PREFERENCES["max_width"]
        if merged["max_width"] not in _WIDTH_CHOICES:
            merged["max_width"] = DEFAULT_PREFERENCES["max_width"]
        merged["text_align"] = ("justify"
                                if merged.get("text_align") == "justify"
                                else "start")
        if merged.get("background") not in _BACKGROUND_PALETTE:
            merged["background"] = "follow"
        merged["indent"] = bool(merged.get("indent"))
        merged["show_status"] = bool(merged.get("show_status"))
        merged["text_selection"] = bool(merged.get("text_selection", True))
        merged["font_family"] = str(merged.get("font_family") or "").strip()
        self._preferences = merged

        self._font_size = int(merged["font_size"])
        self._font_size_label.value = str(self._font_size)
        self._content_host.spacing = float(merged["paragraph_spacing"])
        self._reader_document.width = float(merged["max_width"])
        self._status_bar.visible = (merged["show_status"]
                                    and not self._immersive)
        background, foreground = _BACKGROUND_PALETTE[merged["background"]]
        self._reader_document.bgcolor = background
        self._title.color = foreground
        if not merged["text_selection"]:
            # 关掉开关时顺手收起划词条：那条选区已经选不出来了，留着是个假线索。
            self.clear_selection()

        alignment = (ft.TextAlign.JUSTIFY if merged["text_align"] == "justify"
                     else ft.TextAlign.START)
        # 第 0 段不挂单：它的段顶本来就是 0，那点像素重排后仍在那一段里，
        # 滚去 0 反倒把人从第一段中间弹回章首。
        pending_anchor = anchor if (reflowed and anchor) else None
        baseline = float(self._content_size[1])
        paragraphs = self.paragraph_count()
        for control in self._content_host.controls:
            role = getattr(control, "data", None)
            if role not in {_TEXT_BLOCK, _HEADING_BLOCK}:
                continue
            is_heading = role == _HEADING_BLOCK
            control.size = self._font_size + (2 if is_heading else 0)
            control.font_family = merged["font_family"] or None
            control.style = ft.TextStyle(height=(
                1.45 if is_heading else float(merged["line_height"])))
            control.text_align = alignment
            control.color = foreground
            control.selectable = bool(merged["text_selection"])
            if not is_heading:
                # 缩进是"写在文本里"的两个全角空格：切换时加回去、也拆得掉。
                raw = str(control.value or "").lstrip("　")
                if merged["indent"] and not raw.startswith(_BULLET_PREFIXES):
                    control.value = "　　" + raw
                elif not merged["indent"]:
                    control.value = raw
        # 挂单交给尺寸事件兑现（`_on_content_size`）：新装尺下的版心高没报上来之前，
        # 任何一次滚动都是拿旧高度乘新段高，那是个看着合理、实则偏移的数。
        self._relocate_pending = (
            (int(pending_anchor), baseline, paragraphs)
            if pending_anchor is not None else None)
        return dict(merged)

    @staticmethod
    def _clamped_int(value, minimum: int, maximum: int, default: int) -> int:
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clamped_float(value, minimum: float, maximum: float,
                       default: float) -> float:
        try:
            return max(minimum, min(maximum, float(value)))
        except (TypeError, ValueError):
            return default

    def set_font_size(self, size: int) -> None:
        """改字号：已挂出来的段落就地重排，不重新拉取正文（旧阅读器区间 13-28）。"""
        self.apply_preferences({"font_size": size})

    def build_settings_dialog(self, on_change, on_close) -> ft.Control:
        """阅读设置弹层：字号/行距/段距/版心/对齐/背景/缩进/状态栏。

        只**搭**控件、不碰 page：谁 show_dialog、谁 pop_dialog 归 Controller
        （视图层拿不到生命周期，见 §7.2）。每次打开都按当前偏好重新起稿，
        所以滑块显示的就是屏幕上的值。
        """
        preferences = self._preferences

        def slider(label, key, minimum, maximum, divisions):
            return ft.Column([
                ft.Text(label, size=12),
                ft.Slider(
                    value=float(preferences[key]), min=minimum, max=maximum,
                    divisions=divisions, label="{value}",
                    on_change=lambda e, name=key: on_change(
                        name, float(e.control.value))),
            ], spacing=0)

        width = ft.Dropdown(
            label="正文宽度", value=str(int(preferences["max_width"])),
            options=[ft.dropdown.Option("620", "窄"),
                     ft.dropdown.Option("700", "标准"),
                     ft.dropdown.Option("760", "宽")],
            on_select=lambda e: on_change("max_width", float(e.control.value)))
        alignment = ft.Dropdown(
            label="文本对齐", value=preferences["text_align"],
            options=[ft.dropdown.Option("start", "左对齐"),
                     ft.dropdown.Option("justify", "两端对齐")],
            on_select=lambda e: on_change("text_align", e.control.value))
        background = ft.Dropdown(
            label="阅读背景", value=preferences["background"],
            options=[ft.dropdown.Option("follow", "跟随主题"),
                     ft.dropdown.Option("paper", "纸张"),
                     ft.dropdown.Option("eye", "护眼"),
                     ft.dropdown.Option("night", "夜间"),
                     ft.dropdown.Option("eink", "墨水屏")],
            on_select=lambda e: on_change("background", e.control.value))
        # 长标签必须**自己换行**，客户端不会：`ft.Switch(label=ft.Text(...))` 实测与裸串
        # 一模一样照字面画出去（第七十七轮 304 档，两臂图完全重合），因为开关把标签按
        # 无限宽排。所以补充说明另起一行小字，标签本身只留主谓那半截。
        def hint(text: str) -> ft.Text:
            return ft.Text(text, size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        indent = ft.Switch(
            label="段首缩进", value=bool(preferences["indent"]),
            on_change=lambda e: on_change("indent", bool(e.control.value)))
        status = ft.Switch(
            label="显示底部状态栏",
            value=bool(preferences["show_status"]),
            on_change=lambda e: on_change("show_status", bool(e.control.value)))
        selection = ft.Switch(
            label="划词可选",
            value=bool(preferences["text_selection"]),
            on_change=lambda e: on_change(
                "text_selection", bool(e.control.value)))
        # 这块**留** 420 定宽：弹层会被客户端夹到窗宽、内容跟着窄框重排（304 那一档实测
        # 框 256，三条下拉各自独占一行、滑条跟着变短，`out_dlg77b_reader_settings_320.png`）。
        # 真正出屏的只有开关的长标签——它按无限宽排，所以拆成短标签 + 一行小字（见上）。
        # 反过来把 420 去掉会更糟：宽窗里整块塌到 280，"左对齐"被截成"左对"（第七十七轮
        # 两臂对照，`out_dlg77A_reader_settings_1200.png`）。
        panel = ft.Container(width=420, padding=12, content=ft.Column([
            ft.Row([ft.Text("阅读设置", size=20, weight=ft.FontWeight.BOLD),
                    ft.IconButton(ft.Icons.CLOSE, tooltip="关闭",
                                  on_click=lambda e: on_close())]),
            slider("字号", "font_size", _MIN_FONT_SIZE, _MAX_FONT_SIZE, 15),
            slider("行距", "line_height", 1.3, 2.2, 9),
            slider("段距", "paragraph_spacing", 4, 24, 10),
            ft.ResponsiveRow([width, alignment], columns=12),
            background, indent, status,
            hint("那一栏显示书名 · 作者 · 总章数"),
            selection, hint("打开后能在正文里选字查词典"),
        ], tight=True))
        # 窄窗里两位下拉各占整行：并排时"左对齐"被截成"左对"（第七十七轮 360 档像素）。
        width.col = {"xs": 12, "sm": 6}
        alignment.col = {"xs": 12, "sm": 6}
        return ft.AlertDialog(modal=False, alignment=ft.Alignment.CENTER_RIGHT,
                              inset_padding=ft.Padding(24, 24, 24, 24),
                              scrollable=True, content=panel)


    def show_directory(self, show: bool) -> None:
        """目录页覆盖在正文之上。"""
        self._detail_page.visible = bool(show)
        self._sync_immersive_exit()

    def _sync_immersive_exit(self) -> None:
        """浮标只在"沉浸 + 目录没盖上来"时亮着。

        目录页是不透明整页覆盖，它自己带「返回阅读」；浮标压在目录行上是第二个
        出口，也是挡住章名的多余一圈。
        """
        self._immersive_exit.visible = (self._immersive
                                        and not self._detail_page.visible)

    def show_side_panel(self, kind: str = "bookmark") -> None:
        """左侧抽屉：kind in {bookmark, search} 选页签；空串收起。

        两个入口共用一个抽屉（书签 / 书内搜索各为一签），切签不关抽屉。
        """
        if kind not in self._side_pages:
            self._side_host.visible = False
            return
        self._side_host.visible = True
        self._side_tabs.selected_index = self._side_pages.index(kind)

    def set_immersive(self, immersive: bool) -> None:
        """沉浸模式：收起本视图的工具栏与状态栏，右下角亮出浮标当出口。

        状态栏要同时听两个开关：用户可以在设置里就把阅读状态关掉。左侧导航栏
        不在本视图的控制范围内（外壳的事），所以这里收的只是阅读器自己那两条。
        """
        self._immersive = bool(immersive)
        self._toolbar.visible = not self._immersive
        self._status_bar.visible = (not self._immersive
                                    and bool(self._preferences["show_status"]))
        self._sync_immersive_exit()
        # 图标跟着状态走：亮着的那一面就是当前读数（工具栏被藏时由浮标接手）。
        self._immersive_btn.icon = (ft.Icons.FULLSCREEN_EXIT if self._immersive
                                    else ft.Icons.FULLSCREEN)

    def scroll_by(self, delta: float, duration: int = 120) -> None:
        """正文区平滑滚动（快捷键 page_up/page_down 复用）。"""
        self._run(self._reader_scroll.scroll_to(
            delta=int(delta), duration=duration))

    def restore_scroll(self, offset: float) -> None:
        """续读定位：把正文滚到章内绝对偏移（瞬时，无动画）。"""
        try:
            self._last_scroll_pixels = max(0.0, float(offset))
        except (TypeError, ValueError):
            pass
        self._run(self._reader_scroll.scroll_to(offset=float(offset), duration=0))

    def _on_content_size(self, event) -> None:
        self._content_size = (float(getattr(event, "width", 0) or 0.0),
                              float(getattr(event, "height", 0) or 0.0))
        self._correct_relocation()

    def _correct_relocation(self) -> None:
        """就地重排后回到原来那一段：新版心高报上来，按新尺子滚一次。

        挂单只在**版心高真的变了**时才兑现——重排完高度没变（例如同行数的对齐方式）
        就说明屏幕上那段本来就没动，多滚一次反倒会把位置写歪。段数对不上说明文档
        在等尺寸的这几百毫秒里被追加过（自动续读），此时用户要看的已经不是那段。
        """
        pending = self._relocate_pending
        if pending is None:
            return
        anchor, baseline, paragraphs = pending
        height = float(self._content_size[1])
        if height <= 0 or height == baseline:
            return
        self._relocate_pending = None
        if self.paragraph_count() != paragraphs:
            return
        self.scroll_to_paragraph(anchor)

    def _on_document_scroll(self, event) -> None:
        """先记下正文像素，再转给 Controller 落库。

        记下的是**当前这套装尺下**的像素：就地重排（改字号/版心）时它是唯一能把
        屏幕位置换算回段号的凭据（#87）。
        """
        pixels = getattr(event, "pixels", None)
        if pixels is not None:
            try:
                self._last_scroll_pixels = max(0.0, float(pixels))
            except (TypeError, ValueError):
                pass
        self._fire(self.on_save_scroll, event)

    def paragraph_count(self) -> int:
        """当前挂载文档里的正文段数（与书内索引的 para_index 同一口径）。"""
        return sum(1 for control in self._content_host.controls
                   if getattr(control, "data", None)
                   in (_TEXT_BLOCK, _HEADING_BLOCK))

    def _paragraph_units(self) -> list[float]:
        """每段在当前装尺下的模型高度（px），与挂载顺序一一对应。

        正函数（:meth:`paragraph_pixel`）与反函数（:meth:`paragraph_at_pixel`）共用
        这一份，两套装尺之间换算才不会各算各的。宽度取客户端报来的版心渲染宽，
        还没量到时返回空表——宁可不换算，也不拿猜出来的宽度算出个像模像样的错值。
        """
        width = float(self._content_size[0])
        if width <= 0:
            return []
        units: list[float] = []
        for control in self._content_host.controls:
            if getattr(control, "data", None) not in (_TEXT_BLOCK, _HEADING_BLOCK):
                continue
            size = float(getattr(control, "size", 0) or self._font_size)
            multiplier = float(
                getattr(getattr(control, "style", None), "height", None)
                or self._preferences["line_height"])
            per_line = max(1.0, math.floor(width / size))
            text = str(getattr(control, "value", "") or "")
            units.append(max(1, math.ceil(len(text) / per_line)) * size * multiplier
                         + float(self._preferences["paragraph_spacing"]))
        return units

    def _paragraph_tops(self) -> list[float]:
        """每段段顶的像素 y（第 0 段是 0）；量不到装尺时返回空表。

        正反两个方向都从这张表出发：段顶是 ``高度 ×（前缀和 / 总高）``，而 Python
        的 ``sum()`` 对浮点走的是补偿求和，和自己一项项累加**差最后一两个 bit**——
        两个方向各算各的前缀和，段顶像素就会掉进上一段的带里，反查回 239 段这种
        "差一段"看着无害，重排几次越走越偏（#87）。
        """
        height = float(self._content_size[1])
        units = self._paragraph_units()
        total = sum(units)
        if height <= 0 or total <= 0:
            return []
        tops: list[float] = []
        walked = 0.0
        for unit in units:
            tops.append(height * (walked / total))
            walked += unit
        return tops

    def paragraph_pixel(self, paragraph_index: int) -> float | None:
        """第 n 段段首在版心里的像素 y；没量到渲染尺寸时返回 None。

        模型（#86 取证五/六，300 段两形状浏览器实测）：每段按
        ``ceil(段字符数 / 每行字数)`` 算折行数，行数 × 行高 + 段间距，再用客户端
        报上来的版心渲染高**等比缩放**。缩放是关键——几何行宽（``版心宽 / 字号``）
        本身有百分之几的偏差，整篇缩一次就把偏差吸收掉了：对话密集的开头
        （前 100 段每段一二十字）最坏差 **0.1 段**，而按字符占比折算
        （``ChapterViewport.track`` 那把尺）最坏差 **13.3 段**——差十几段等于没跳。

        段首 y = 前面那些段占的高度；版心之上还有章名与内衬，按版心坐标滚会让
        命中段落在屏幕偏下处——偏下看得见，偏上就被顶出屏幕了，所以不做补偿。
        """
        tops = self._paragraph_tops()
        if not tops:
            return None
        return tops[max(0, min(int(paragraph_index), len(tops) - 1))]

    def paragraph_at_pixel(self, pixel: float) -> int | None:
        """:meth:`paragraph_pixel` 的反函数：这个像素此刻压在第几段上。

        就地重排换的是尺子，不是位置：重排前先把裸像素换算成段号（段号才是那个
        不变量），重排后再用 :meth:`paragraph_pixel` 按新尺子滚回去（#87）。
        """
        tops = self._paragraph_tops()
        if not tops:
            return None
        return max(0, bisect_right(tops, float(pixel)) - 1)

    def scroll_to_paragraph(self, paragraph_index: int) -> bool:
        """滚到第 n 段（瞬时）。渲染尺寸还没报上来时返回 False，调用方自行重试。"""
        pixel = self.paragraph_pixel(paragraph_index)
        if pixel is None:
            return False
        self.restore_scroll(pixel)
        return True

    def anchor_paragraph(self, paragraph_index: int) -> bool:
        """把屏幕钉在第 n 段上：量得到尺寸就立刻滚，量不到就挂单等尺寸事件。

        续读回位等的是**新版心的渲染高**，那是客户端布局完自己报上来的（#86 取证四）——
        刚挂完正文那一下还是 ``(0, 0)``，此时任何像素数都是猜的。挂单与就地重排的回位
        共用一条机制（:meth:`_correct_relocation`）：等的是同一件事，轮询和超时都替代
        不了这个事件。

        第 0 段不挂单：它的段顶本来就是 0，那点像素在新装尺下仍落在第一段里，
        滚去 0 反倒把人从第一段中间弹回章首（与 #87 同一口径）。
        """
        index = int(paragraph_index)
        if index <= 0:
            return False
        if self.paragraph_pixel(index) is not None:
            return self.scroll_to_paragraph(index)
        self._relocate_pending = (index, 0.0, self.paragraph_count())
        return False

    @property
    def immersive(self) -> bool:
        return self._immersive
