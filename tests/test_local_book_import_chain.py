"""本地书端到端链路（导入 → 上架 → 重开阅读）。

用户报"本地书读不了"其实是两条断链，且都在**成功路径**上：

1. ``LocalBookService.import_local_file`` 只写了 ``books`` 表那行进度，没写书架记录
   列表（SourceService.bookshelf），所以导完书架上看不见；
2. 从书架点开本地书时 ``SourceService.load_source("localsource://…")`` 必然查不到
   （它只登记规则源），旧代码把"查不到源"当成书坏了：会话 source_url 变成空串，
   reader-cache 的键对不上，最后派规则引擎去抓 ``file://`` ——白屏。

全程离线：临时目录 + 现场写的 txt + 引擎替身（一旦被调用即失败）。
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import flet as ft

from app.core.storage import StorageService
from app.services.app_paths import AppPaths
from app.services.content.local_book_service import (
    LOCAL_BOOK_SOURCE_URL,
    LocalBookCacheMissing,
    LocalBookService,
    SUPPORTED_EXTS,
    make_local_source,
)
from app.services.reading.reading_service import ReadingService
from app.services.reading_cache import ReadingCache
from app.services.sources.source_service import SourceService
from app.views.reading.bookshelf_view import BookshelfView

TXT = ("第一章 起\n少年上山。\n"
       "第二章 承\n山中有雾。\n"
       "第三章 合\n雾散而下。\n")


# --------------------------------------------------------------------------- #
# 服务替身
# --------------------------------------------------------------------------- #
class _NoEngine:
    """本地书绝不该走到引擎：走到了就是"派引擎去抓 file://"那个 bug。"""

    def __init__(self):
        self.calls: list = []

    async def chapters(self, source, book):
        self.calls.append("chapters")
        raise AssertionError("本地书不应请求引擎")

    async def content_document(self, source, chapter, next_url=""):
        self.calls.append("content")
        raise AssertionError("本地书不应请求引擎")


class _ShelfStub:
    """SourceService 替身：入架动作记账，反查源一律落空（ localsource 真是这样）。"""

    def __init__(self):
        self.shelved: list = []

    async def shelve_book(self, source, book, shelved=True):
        self.shelved.append((source, book, shelved))
        return True

    def add_to_bookshelf(self, source, book, shelved=True):
        self.shelved.append((source, book, shelved))
        return True

    async def load_source(self, url):
        return None

    def find_bookshelf_item(self, key):
        return None


class _CatalogOnlyCache:
    """目录查得到、正文一律查不到——模拟 content 目录被清掉的那本书。"""

    def __init__(self, inner: ReadingCache):
        self._inner = inner

    def load_catalog(self, source_url, book_url):
        return self._inner.load_catalog(source_url, book_url)

    def save_catalog(self, source_url, book_url, chapters):
        return self._inner.save_catalog(source_url, book_url, chapters)

    def load_content(self, source_url, book_url, chapter_url):
        return None

    def save_content(self, source_url, book_url, chapter_url, document):
        return self._inner.save_content(
            source_url, book_url, chapter_url, document)

    def remove_book(self, source_url, book_url):
        return self._inner.remove_book(source_url, book_url)


# --------------------------------------------------------------------------- #
# 1. 服务层：导入即上架 + 重开不联网
# --------------------------------------------------------------------------- #
class LocalBookChainTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="localchain_"))
        self.storage = StorageService(
            paths=AppPaths(data=self.root / "data", cache=self.root / "cache"))
        asyncio.run(self.storage.start())
        self.book_file = self.root / "上山.txt"
        self.book_file.write_text(TXT, encoding="utf-8")

    def tearDown(self):
        asyncio.run(self.storage.stop())
        shutil.rmtree(self.root, ignore_errors=True)

    def _service(self, shelf=None) -> LocalBookService:
        svc = LocalBookService(storage=self.storage, source_service=shelf)
        asyncio.run(svc.on_start())
        return svc

    def _reading(self, cache=None, shelf=None, engine=None) -> ReadingService:
        return ReadingService(rule_engine=engine or _NoEngine(),
                              storage=self.storage, settings=None,
                              source_service=shelf or _ShelfStub(),
                              cache=cache or ReadingCache(self.storage.paths.cache))

    def test_import_shelves_through_shelve_book(self):
        """导入必须走 shelve_book 上架（它才发 SourceChangedEvent，书架才会自己刷新）。"""
        shelf = _ShelfStub()
        result = asyncio.run(self._service(shelf).import_local_file(str(self.book_file)))
        self.assertEqual(len(shelf.shelved), 1, "导入没有把书放进书架")
        source, book, shelved = shelf.shelved[0]
        self.assertTrue(shelved)
        self.assertEqual(source.bookSourceUrl, LOCAL_BOOK_SOURCE_URL)
        self.assertEqual(book.url, result.book_url)

    def test_record_survives_the_real_bookshelf_file(self):
        """对真实 SourceService 入架后，书架列表读回来还是这一本本地书。"""
        real = SourceService(resource_dir=self.root / "resource",
                             storage=StorageService(
                                 paths=AppPaths(data=self.root / "src",
                                                cache=self.root / "srccache")))
        asyncio.run(real.start())
        try:
            result = asyncio.run(self._service(real).import_local_file(
                str(self.book_file)))
            records = asyncio.run(real.list_bookshelf())
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["source_url"], LOCAL_BOOK_SOURCE_URL)
            self.assertEqual(records[0]["url"], result.book_url)
            self.assertTrue(records[0]["in_bookshelf"])
        finally:
            asyncio.run(real.stop())

    def test_reopen_from_shelf_never_touches_the_engine(self):
        """书架重开：反查不到源也要认得伪源；目录与正文都从 reader-cache 命中。"""
        shelf = _ShelfStub()
        result = asyncio.run(self._service(shelf).import_local_file(str(self.book_file)))
        engine = _NoEngine()
        reading = self._reading(shelf=shelf, engine=engine)
        record = {"name": result.book.name, "author": "", "url": result.book_url,
                  "origin": LOCAL_BOOK_SOURCE_URL,
                  "source_url": LOCAL_BOOK_SOURCE_URL}
        session = asyncio.run(reading.open_book(None, record))
        self.assertEqual(session.source_url, LOCAL_BOOK_SOURCE_URL)
        self.assertEqual([c.name for c in session.chapters],
                         ["第一章 起", "第二章 承", "第三章 合"])
        document = asyncio.run(reading.load_content(session.chapters[1]))
        self.assertIn("山中有雾", document.plain_text)
        self.assertEqual(engine.calls, [])

    def test_missing_catalog_asks_for_reimport(self):
        """目录缓存没了：报"重新导入"，不许派引擎去抓 file://（那会被说成网络问题）。"""
        engine = _NoEngine()
        reading = self._reading(
            cache=ReadingCache(self.root / "empty-cache"), engine=engine)
        with self.assertRaises(LocalBookCacheMissing) as ctx:
            asyncio.run(reading.open_book(
                make_local_source(),
                {"name": "没缓存的书", "url": "file:///nowhere.txt"}))
        self.assertIn("重新导入", str(ctx.exception))
        self.assertEqual(engine.calls, [])

    def test_missing_content_asks_for_reimport(self):
        """目录在、正文不在：同样收口成"重新导入"。"""
        shelf = _ShelfStub()
        result = asyncio.run(self._service(shelf).import_local_file(str(self.book_file)))
        engine = _NoEngine()
        cache = _CatalogOnlyCache(ReadingCache(self.storage.paths.cache))
        reading = self._reading(cache=cache, shelf=shelf, engine=engine)
        session = asyncio.run(reading.open_book(
            None, {"name": result.book.name, "url": result.book_url,
                   "origin": LOCAL_BOOK_SOURCE_URL}))
        with self.assertRaises(LocalBookCacheMissing):
            asyncio.run(reading.load_content(session.chapters[0]))
        self.assertEqual(engine.calls, [])

    def test_supported_exts_match_the_picker(self):
        """解析器吃不下 mobi/umd，文件选择器就不能让用户挑到它。"""
        self.assertEqual(SUPPORTED_EXTS, {"txt", "epub", "pdf"})


# --------------------------------------------------------------------------- #
# 2. UI 层：书架「导入本地书」入口
# --------------------------------------------------------------------------- #
class _FakeHub:
    def __init__(self, services: dict):
        self._services = services
        self.bus = None

    def get(self, stype):
        if stype in self._services:
            return self._services[stype]
        raise KeyError(stype)


class _FakeBus:
    def subscribe(self, event_type, handler):
        return lambda: None

    def publish(self, event):
        return []


class _Picker:
    """FilePicker 替身：pick_files 返回预置文件，并记下调用参数。"""

    def __init__(self, paths):
        self.files = [SimpleNamespace(path=p) for p in paths]
        self.calls: list = []

    def pick_files(self, **kwargs):
        self.calls.append(kwargs)

        async def _resolve():
            return self.files
        return _resolve()


class _UiSourceService:
    """书架服务替身：交出**同一批 dict**（真 SourceService 也这样，只复制列表），
    所以控制器若在原对象上打标记就会被落进 bookshelf.json。"""

    def __init__(self):
        self.records: list = []
        self.cover_calls: list = []
        self.cover_fail: Exception | None = None
        self.cover_delay: float = 0.0
        # False = 写进了库但书架记录没对上（真服务里就是 book_url 对不上）
        self.cover_matches_record: bool = True

    async def list_bookshelf(self):
        return list(self.records)

    async def list_sources(self):
        return []

    async def shelve_book(self, source, book, shelved=True):
        self.records.append(
            {"name": book.name, "author": book.author, "url": book.url,
             "source_url": source.bookSourceUrl, "origin": source.bookSourceUrl,
             "in_bookshelf": bool(shelved)})
        return True

    async def update_cover(self, book_url, cover_url_or_path):
        self.cover_calls.append((book_url, str(cover_url_or_path)))
        if self.cover_delay:
            await asyncio.sleep(self.cover_delay)
        if self.cover_fail is not None:
            raise self.cover_fail
        resolved = str(cover_url_or_path)
        if self.cover_matches_record:
            for record in self.records:
                if str(record.get("url") or record.get("bookUrl") or "") == book_url:
                    record["cover_url"] = resolved
        return resolved


class _UiLocalService:
    """导入替身：解析成功后按真服务的做法经 ``shelve_book`` 入架。"""

    def __init__(self, source_service, fail: dict | None = None):
        self.source_service = source_service
        self.fail = fail or {}
        self.paths: list = []

    async def import_local_file(self, path):
        from app.services.content.local_book_service import make_local_source
        self.paths.append(path)
        if path in self.fail:
            raise self.fail[path]
        book = SimpleNamespace(name=Path(path).stem, author="", url=f"file:///{path}")
        await self.source_service.shelve_book(make_local_source(), book)
        return SimpleNamespace(book_url=book.url, chapter_count=3)


LOCAL_RECORD = {"name": "本地书", "author": "", "url": "file:///c/上山.txt",
                "source_url": LOCAL_BOOK_SOURCE_URL,
                "origin": LOCAL_BOOK_SOURCE_URL}
NET_RECORD = {"name": "网书", "author": "甲", "url": "http://s/book/1",
              "source_url": "http://src-1.example.com",
              "origin": "http://src-1.example.com"}


def _attach(records, paths, fail=None):
    from app.controllers.bookshelf_controller import BookshelfController
    source = _UiSourceService()
    source.records = list(records)
    view = BookshelfView()
    view.file_picker = _Picker(paths)
    controller = BookshelfController(_FakeHub({
        SourceService: source,
        LocalBookService: _UiLocalService(source, fail)}))
    controller._bus = _FakeBus()
    controller.attach(view)
    return controller, view, source


async def _wait_status(view, needle: str, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if needle in str(view._status.value or ""):
            return True
        await asyncio.sleep(0.02)
    return False


def _menu_titles(dialog) -> list[str]:
    return [tile.title.value for tile in dialog.content.controls]


def _leading_icon(row) -> str:
    """行首图标：记录没封面时 ``_cover_control`` 回落成图标（外面还包一层容器）。"""
    leading = row.leading.content
    return leading.icon if isinstance(leading, ft.Icon) else None


class BookshelfImportUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_view_exposes_picker_for_navigation(self):
        """navigation 只认 view.file_picker，并把它塞进 page.services（非 overlay）。"""
        self.assertIsInstance(
            getattr(BookshelfView(), "file_picker", None), ft.FilePicker)

    async def test_pick_imports_every_file_and_refreshes_the_shelf(self):
        controller, view, _source = _attach(
            [], ["c:/上山.txt", "c:/别集.epub"])
        view._import_btn.on_click(SimpleNamespace())
        picker_kwargs = view.file_picker.calls[0]
        self.assertTrue(picker_kwargs["allow_multiple"])
        self.assertEqual(sorted(picker_kwargs["allowed_extensions"]),
                         sorted(SUPPORTED_EXTS))
        self.assertTrue(await _wait_status(view, "已导入 2 本"),
                        f"导入没收口：{view._status.value!r}")
        titles = [tile.title.value for tile in view._list.controls]
        self.assertEqual(titles, ["上山", "别集"])
        # 导入→刷新→渲染一条链走到底：新行是本地书的样子（文件夹图标、无换源入口）
        self.assertEqual(_leading_icon(view._list.controls[0]), ft.Icons.FOLDER_OPEN)
        self.assertEqual(_menu_titles(view.build_manage_dialog(
            controller._records[0])), ["更换封面", "移出书架"])

    async def test_one_bad_file_does_not_swallow_the_rest(self):
        _controller, view, _source = _attach(
            [], ["c:/bad.txt", "c:/ok.txt"],
            fail={"c:/bad.txt": ValueError("暂不支持的文件类型 .xyz")})
        view._import_btn.on_click(SimpleNamespace())
        self.assertTrue(await _wait_status(view, "已导入 1 本"),
                        f"部分失败没收口：{view._status.value!r}")
        self.assertIn("bad.txt", str(view._status.value))
        self.assertIn("暂不支持", str(view._status.value))

    async def test_cancelled_pick_says_so_and_imports_nothing(self):
        _controller, view, source = _attach([], [])
        view._import_btn.on_click(SimpleNamespace())
        self.assertTrue(await _wait_status(view, "未选择文件"),
                        f"取消导入没有反馈：{view._status.value!r}")
        self.assertEqual(source.records, [])

    async def test_local_record_hides_switch_and_cache_actions(self):
        """本地书没有规则源（换源无从谈起），正文也已在磁盘（无需整书缓存）。"""
        controller, view, _source = _attach([LOCAL_RECORD, NET_RECORD], [])
        self.assertTrue(await _wait_status(view, "书架共 2 本"))
        # 长按弹出的菜单吃的是控制器渲染后的记录（local 标记在那一步打上）
        rendered = controller._records
        self.assertEqual([r["local"] for r in rendered], [True, False])
        # 「更换封面」本地书也能换（导入的 epub 常常没封面），所以两种记录都有这一行
        self.assertEqual(_menu_titles(view.build_manage_dialog(rendered[0])),
                         ["更换封面", "移出书架"])
        self.assertEqual(_menu_titles(view.build_manage_dialog(rendered[1])),
                         ["换源", "整书缓存 / 取消缓存", "更换封面", "移出书架"])

    async def test_local_flag_never_leaks_into_the_stored_record(self):
        """标记只能打在渲染副本上：写回服务内存里那批 dict 会被存进 bookshelf.json。"""
        stored = [dict(LOCAL_RECORD)]
        _controller, view, source = _attach(stored, [])
        self.assertTrue(await _wait_status(view, "书架共 1 本"))
        self.assertNotIn("local", stored[0])
        self.assertNotIn("local", source.records[0])
        # 本地行用文件夹图标，和网络行的书本图标区分开
        self.assertEqual(_leading_icon(view._list.controls[0]), ft.Icons.FOLDER_OPEN)


class BookshelfCoverTests(unittest.IsolatedAsyncioTestCase):
    """书架「更换封面」：对话框入口 → FilePicker → update_cover → 状态行。"""

    async def test_clicking_the_dialog_row_replaces_the_cover(self):
        """整条链：长按菜单那一行 → 控制器绑定的 request_cover → 选择器 → 服务。

        只断言"回调槽被填了"照不出真接线——把行点下去、看服务收没收到。
        """
        controller, view, source = _attach([dict(NET_RECORD)], ["c:/新封面.jpg"])
        self.assertTrue(await _wait_status(view, "书架共 1 本"))
        tile = next(t for t in view.build_manage_dialog(
            controller._records[0]).content.controls
                    if t.title.value == "更换封面")
        tile.on_click(SimpleNamespace())
        self.assertTrue(await _wait_status(view, "封面已更新"),
                        f"点行没走到服务：{view._status.value!r}")
        self.assertEqual(source.cover_calls,
                         [("http://s/book/1", "c:/新封面.jpg")])

    async def test_cover_action_opens_a_single_image_picker(self):
        _controller, view, _source = _attach([dict(NET_RECORD)], [])
        self.assertTrue(await _wait_status(view, "书架共 1 本"))
        view.controller.request_cover(view.controller._records[0])
        kwargs = view.file_picker.calls[-1]
        self.assertFalse(kwargs["allow_multiple"])
        self.assertEqual(kwargs["file_type"], ft.FilePickerFileType.IMAGE)

    async def test_picked_image_is_written_through_the_service(self):
        controller, view, source = _attach([dict(NET_RECORD)], ["c:/新封面.jpg"])
        self.assertTrue(await _wait_status(view, "书架共 1 本"))
        controller.request_cover(controller._records[0])
        self.assertTrue(await _wait_status(view, "封面已更新"),
                        f"换封面没收口：{view._status.value!r}")
        self.assertEqual(source.cover_calls,
                         [("http://s/book/1", "c:/新封面.jpg")])

    async def test_cancelled_pick_leaves_the_record_untouched(self):
        controller, view, source = _attach([dict(NET_RECORD)], [])
        self.assertTrue(await _wait_status(view, "书架共 1 本"))
        controller.request_cover(controller._records[0])
        self.assertTrue(await _wait_status(view, "已取消换封面"),
                        f"取消换封面没反馈：{view._status.value!r}")
        self.assertEqual(source.cover_calls, [])
        self.assertNotIn("cover_url", source.records[0])

    async def test_service_failure_lands_on_the_status_line(self):
        controller, view, source = _attach([dict(NET_RECORD)], ["c:/坏.jpg"])
        self.assertTrue(await _wait_status(view, "书架共 1 本"))
        source.cover_fail = OSError("图片解码失败")
        controller.request_cover(controller._records[0])
        self.assertTrue(await _wait_status(view, "换封面失败"),
                        f"失败没有反馈：{view._status.value!r}")
        self.assertIn("图片解码失败", str(view._status.value))
        # 失败之后架子上的记录还是原样
        self.assertNotIn("cover_url", source.records[0])

    async def test_success_claim_needs_the_record_to_actually_match(self):
        """服务写进了库但 book_url 没对上书架记录——这时说"已更新"是假话。"""
        controller, view, source = _attach([dict(NET_RECORD)], ["c:/新封面.jpg"])
        self.assertTrue(await _wait_status(view, "书架共 1 本"))
        source.cover_matches_record = False
        controller.request_cover(controller._records[0])
        self.assertTrue(await _wait_status(view, "封面没落到书架上"),
                        f"没落上却说成功了：{view._status.value!r}")

    async def test_record_without_book_url_never_opens_the_picker(self):
        controller, view, source = _attach(
            [{"name": "无地址", "author": ""}], ["c:/新封面.jpg"])
        self.assertTrue(await _wait_status(view, "书架共 1 本"))
        controller.request_cover(controller._records[0])
        self.assertTrue(await _wait_status(view, "没有书地址"),
                        f"缺书地址没有反馈：{view._status.value!r}")
        self.assertEqual(view.file_picker.calls, [])
        self.assertEqual(source.cover_calls, [])

    async def test_late_cover_result_after_detach_touches_no_widgets(self):
        """在途选完图用户切了页：_view 已 None，这条尾巴不能去碰控件。

        双向钉：状态行没被写，编排任务也没抛异常（去掉 _status 的守卫就是
        None.set_status_text，只会以"任务失败"的形式漏出来）。
        """
        controller, view, source = _attach([dict(NET_RECORD)], ["c:/新封面.jpg"])
        self.assertTrue(await _wait_status(view, "书架共 1 本"))
        source.cover_delay = 0.2
        controller.request_cover(controller._records[0])
        controller.detach()
        with self.assertNoLogs("app.controllers.base", level="ERROR"):
            await asyncio.sleep(0.5)
        self.assertNotIn("封面", str(view._status.value or ""))
        # 服务侧那一步是真做了的（持久化不因切页回滚）
        self.assertEqual(len(source.cover_calls), 1)

    async def test_cover_src_renders_as_image_and_falls_back_on_icon(self):
        """有封面就上 Image，取不到回落图标——视图不判断它是路径还是 URL。"""
        view = BookshelfView()
        record = {"name": "有封面", "url": "u", "cover_url": "c:/封面.jpg"}
        row = view._row(record)
        self.assertIsInstance(row.leading.content, ft.Image)
        self.assertEqual(row.leading.content.src, "c:/封面.jpg")
        self.assertIsInstance(row.leading.content.error_content, ft.Icon)
        # 没封面 = 老样子
        plain = view._row({"name": "没封面", "url": "u"})
        self.assertIsInstance(plain.leading.content, ft.Icon)
        cell = view._cell(dict(record))
        self.assertIsInstance(cell.content.controls[0].content, ft.Image)
