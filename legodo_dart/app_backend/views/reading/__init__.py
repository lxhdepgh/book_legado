"""M2 阅读域 UI 拆分：视图按域分包 + 每视图一个 Controller（设计 §7.2 / §7.4）。

本包（``app/views/reading/``）只承载 Flet 布局与渲染，**禁止 import 任何
``app.services.*`` 具体模块**（import-lint 守门，§7.5）。视图所需数据一律经
所属 Controller 从 ``ServiceHub.get()`` 取服务后注入；跨视图协作经
``ViewRegistry`` 路由与 EventBus 事件，互不直接 import。

阅读组目的地（注册进 ViewRegistry）：

- :class:`FindBooksView`    找书：书源列表 / 分类浏览 / 搜索页 / 结果流 / 详情对话框；
- :class:`BookshelfView`    书架：网格/列表、最近阅读卡、整书缓存管理、移除确认（换源入口保留）；
- :class:`RssView`          订阅：RSS 订阅源 / 文章列表 / 正文（RssService 的界面承接）；
- :class:`ReaderView`       阅读器：目录 / 正文渲染 / 滚动续读 / 偏好 / 沉浸模式 / 快捷键 / 上下章。

模态/覆盖层（非目的地）：:class:`SourceSwitchDialog` 换源、:class:`LoginDialog`
书源登录、:class:`BookmarkPanel` 书签、:class:`ContentSearchBar` 书内搜索条。
"""
from __future__ import annotations

from app.core.registry import ViewRegistry
from app.core.hub import ServiceHub

from app.views.reading.find_books_view import FindBooksView
from app.views.reading.bookshelf_view import BookshelfView
from app.views.reading.rss_view import RssView
from app.views.reading.reader_view import ReaderView
from app.views.reading.manga_reader_view import MangaReaderView
from app.views.reading.source_switch_dialog import SourceSwitchDialog
from app.views.reading.login_dialog import LoginDialog
from app.views.reading.bookmark_panel import BookmarkPanel
from app.views.reading.content_search_bar import ContentSearchBar

from app.controllers.find_books_controller import FindBooksController
from app.controllers.bookshelf_controller import BookshelfController
from app.controllers.rss_controller import RssController
from app.controllers.reader_controller import ReaderController
from app.controllers.manga_reader_controller import MangaReaderController

__all__ = [
    "FindBooksView", "BookshelfView", "RssView", "ReaderView", "MangaReaderView",
    "SourceSwitchDialog", "LoginDialog", "BookmarkPanel", "ContentSearchBar",
    "FindBooksController", "BookshelfController", "RssController",
    "ReaderController", "MangaReaderController",
    "register_destinations",
]


def register_destinations(registry: ViewRegistry, hub: ServiceHub) -> ViewRegistry:
    """把阅读组三个目的地绑进路由表（§7.3：工厂由装配处绑定，core 不 import 视图）。

    工厂闭包在实例化视图的同时 new 一个对应 Controller 并 ``attach(view)``，
    返回挂载好的视图。所需服务集按 §9.2 声明，供导航层做就绪性检查。
    """
    registry.register(
        "reading.find_books",
        lambda: _build_find_books(hub),
        required_services=("ReadingService",),
    )
    registry.register(
        "reading.bookshelf",
        lambda: _build_bookshelf(hub),
        required_services=("ReadingService", "DownloadService"),
    )
    # 订阅：RSS 服务自成一路（存储 + 引擎网络栈），不需要 ReadingService。
    registry.register(
        "reading.rss",
        lambda: _build_rss(hub),
        required_services=("RssService",),
    )
    registry.register(
        "reading.reader",
        lambda: _build_reader(hub),
        required_services=("ReadingService", "BookmarkService", "ContentSearchService"),
    )
    # M4 漫画专用阅读器（设计 §1.3）：ImageBookService 接阅读域渲染通道。
    registry.register(
        "reading.manga",
        lambda: _build_manga_reader(hub),
        required_services=("ReadingService", "ImageBookService"),
    )
    return registry


def _build_find_books(hub: ServiceHub):
    view = FindBooksView()
    FindBooksController(hub).attach(view)
    return view


def _build_bookshelf(hub: ServiceHub):
    view = BookshelfView()
    BookshelfController(hub).attach(view)
    return view


def _build_rss(hub: ServiceHub):
    view = RssView()
    RssController(hub).attach(view)
    return view


def _build_reader(hub: ServiceHub):
    view = ReaderView()
    ReaderController(hub).attach(view)
    return view


def _build_manga_reader(hub: ServiceHub):
    view = MangaReaderView()
    MangaReaderController(hub).attach(view)
    return view
