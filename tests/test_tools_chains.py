"""「更多工具」里那些点了没反应的功能（本轮修复）。

用户报「更多设置里有很多功能都是无效的」。这些页的服务**都注册在 main.py**，断的是
接线：控制器读一个没人赋值的视图属性、新增按钮塞进一条空规则、编辑是原样重存的空操
作、选中的字族没人渲染、词典规则没有管理入口、备份对象下拉的值被丢掉。

每个用例都吃**真视图控件**（不是替身）——静态断言照不出 Flet 表面变化，只有断到
``_chooser.options`` / ``disabled`` 这类真实属性上，"点了没反应"才会被测出来。
"""
from __future__ import annotations

import asyncio
import io
import json
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

import flet as ft

from app.controllers.tools.export_controller import ExportController
from app.controllers.tools.replace_rule_controller import ReplaceRuleController
from app.services.tools.book_export_service import BookExportService
from app.services.tools.dict_service import DictService
from app.services.tools.replace_rule_service import (GLOBAL_ENABLED_KEY,
                                                     ReplaceRuleService)
from app.views.tools.export_view import ExportView
from app.views.tools.replace_rule_view import ReplaceRuleView


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


BOOKS = [{"url": "http://s/book/1", "name": "已缓存的书", "author": "甲",
          "total": 10, "cached": 10},
         {"url": "http://s/book/2", "name": "半本的书", "author": "乙",
          "total": 10, "cached": 3}]


class _FakeExportService:
    """BookExportService 替身：清单/导出都记调用。

    ``export_delay`` 把动作卡在途里——瞬时返回时，收尾回调可能在那一句"条立起来了"
    的断言之前的空档里就把条关掉，测的就不再是在途状态。
    """

    def __init__(self, books=None, fail: Exception | None = None,
                 export_fail: Exception | None = None, export_delay: float = 0.0):
        self.books = BOOKS if books is None else books
        self.fail = fail
        self.export_fail = export_fail or fail
        self.export_delay = export_delay
        self.calls: list = []

    async def _hold(self) -> None:
        if self.export_delay:
            await asyncio.sleep(self.export_delay)

    async def list_exportable_books(self):
        self.calls.append(("list",))
        if self.fail is not None:
            raise self.fail
        return list(self.books)

    async def export_epub(self, book_url):
        self.calls.append(("epub", book_url))
        await self._hold()
        if self.export_fail is not None:
            raise self.export_fail
        return Path("downloads/书.epub")

    async def export_txt(self, book_url):
        self.calls.append(("txt", book_url))
        await self._hold()
        if self.export_fail is not None:
            raise self.export_fail
        return Path("downloads/书.txt")


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    """等跨循环编排落定（ui_call 排在 EngineHub 守护线程上，不是当前循环）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _attach(service, bus=None):
    view = ExportView()
    controller = ExportController(_FakeHub({BookExportService: service}),
                                  bus=bus or _FakeBus())
    controller.attach(view)
    return controller, view


def _option_keys(view):
    return [str(o.key) for o in view._chooser.options]


def _pick(view, url):
    """模拟用户选下拉项：Flet 先改 value 再回调 on_select。"""
    view._chooser.value = url
    view._chooser.on_select(SimpleNamespace(value=url))


class ExportBookPickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_lists_exportable_books(self) -> None:
        """进页面就有一版清单：老写法读 ``view._selected_book``（没人赋值）永远空。"""
        _controller, view = _attach(_FakeExportService())
        self.assertTrue(await wait_until(lambda: _option_keys(view)),
                        "attach 后没有拉到可导出清单")
        self.assertEqual(_option_keys(view),
                         ["http://s/book/1", "http://s/book/2"])
        self.assertIn("已缓存 3/10", view._chooser.options[1].text)

    async def test_export_disabled_until_a_book_is_chosen(self) -> None:
        _controller, view = _attach(_FakeExportService())
        self.assertTrue(await wait_until(lambda: bool(_option_keys(view))))
        self.assertTrue(view._epub_btn.disabled, "没选书就能点导出")
        self.assertTrue(view._txt_btn.disabled)
        _pick(view, "http://s/book/2")
        self.assertFalse(view._epub_btn.disabled)
        self.assertFalse(view._txt_btn.disabled)
        self.assertEqual(view._book.value, "半本的书")

    async def test_export_passes_the_chosen_book(self) -> None:
        service = _FakeExportService()
        controller, view = _attach(service)
        self.assertTrue(await wait_until(lambda: bool(_option_keys(view))))
        _pick(view, "http://s/book/1")
        view._epub_btn.on_click(None)
        self.assertTrue(
            await wait_until(lambda: ("epub", "http://s/book/1") in service.calls))
        self.assertTrue(
            await wait_until(lambda: "书.epub" in str(view._log.value)),
            str(view._log.value))

    async def test_buttons_re_enable_after_a_failed_export(self) -> None:
        """busy 收尾必须回到"选中即可点"：否则导出失败一次按钮就永久灰掉。"""
        service = _FakeExportService(export_fail=RuntimeError("磁盘满"))
        controller, view = _attach(service)
        self.assertTrue(await wait_until(lambda: bool(_option_keys(view))))
        _pick(view, "http://s/book/1")
        view._epub_btn.on_click(None)
        self.assertTrue(await wait_until(lambda: "导出失败" in str(view._log.value)))
        self.assertFalse(view._epub_btn.disabled, "失败后按钮还是灰的")
        self.assertFalse(view._progress.visible)

    async def test_empty_cache_says_what_to_do(self) -> None:
        """一本都没缓存：下拉框禁用并说明去哪缓存。"""
        _controller, view = _attach(_FakeExportService(books=[]))
        self.assertTrue(await wait_until(lambda: "没有可导出" in str(view._log.value)),
                        str(view._log.value))
        self.assertTrue(view._chooser.disabled)

    async def test_service_failure_does_not_leave_spinner_text(self) -> None:
        service = _FakeExportService(fail=RuntimeError("库打不开"))
        _controller, view = _attach(service)
        self.assertTrue(
            await wait_until(lambda: "读取已缓存书籍失败" in str(view._log.value)),
            str(view._log.value))
        self.assertEqual(_option_keys(view), [])

    async def test_refresh_keeps_the_current_selection(self) -> None:
        """点刷新后原来选中的书还在，不该被清掉。"""
        service = _FakeExportService()
        controller, view = _attach(service)
        self.assertTrue(await wait_until(lambda: bool(_option_keys(view))))
        _pick(view, "http://s/book/2")
        controller.refresh_books()
        self.assertTrue(await wait_until(
            lambda: sum(1 for call in service.calls if call == ("list",)) == 2))
        self.assertEqual(view._chooser.value, "http://s/book/2")
        self.assertEqual(view._book.value, "半本的书")

    async def test_refresh_drops_a_vanished_book(self) -> None:
        """清单里没有的那本不能再留在选中态（否则导出一个不存在的 url）。"""
        service = _FakeExportService()
        controller, view = _attach(service)
        self.assertTrue(await wait_until(lambda: bool(_option_keys(view))))
        _pick(view, "http://s/book/2")
        service.books = [BOOKS[0]]
        controller.refresh_books()
        self.assertTrue(await wait_until(lambda: view._chooser.value is None))
        self.assertTrue(view._epub_btn.disabled)
        self.assertEqual(view._book.value, "未选择书籍")

    async def test_service_lists_only_cached_books(self) -> None:
        """服务侧：缓存 0 章的书不进清单（导出必然失败，列出来只是骗人）。

        计数**故意不相等**：``ReadingCache.cache_counts`` 返回的是
        ``(已缓存, 目录总数)``，两边一样时把解包顺序写反也测不出来
        （这一处之前正是反的，界面显示成"已缓存 5/0 章"）。
        """
        class _Cache:
            def __init__(self, counts):
                self.counts = counts

            def cache_counts(self, source_url, book_url):
                return self.counts[(source_url, book_url)]

        class _Source:
            bookshelf = [
                {"url": "b1", "source_url": "s1", "name": "缓存了四章"},
                {"url": "b2", "source_url": "s1", "name": "没缓存"},
                {"url": "", "source_url": "s1", "name": "脏记录"},
            ]

        service = BookExportService(
            source_service=_Source(), cache=_Cache({
                ("s1", "b1"): (4, 5), ("s1", "b2"): (0, 5)}))
        items = await service.list_exportable_books()
        self.assertEqual(items, [{"url": "b1", "name": "缓存了四章", "author": "",
                                  "total": 5, "cached": 4}])


class _FakePage:
    """记录弹窗的假 page：控制器只在 ``view.page`` 上 show/pop。"""

    def __init__(self):
        self.dialogs: list = []
        self.popped = 0

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)

    def pop_dialog(self):
        self.popped += 1


class _FakeReplaceService:
    def __init__(self, rules=None, global_on: bool = True):
        self.rules = rules if rules is not None else []
        self.global_on = global_on
        self.calls: list = []

    async def list_rules(self):
        return list(self.rules)

    async def save_rule(self, rule):
        self.calls.append(("save", dict(rule)))
        return 1

    async def delete_rule(self, rid):
        self.calls.append(("delete", rid))
        return True

    async def set_enabled(self, rid, enabled):
        self.calls.append(("enable", rid, enabled))
        return True

    async def global_enabled(self):
        return self.global_on

    async def set_global_enabled(self, value):
        self.global_on = bool(value)
        self.calls.append(("global", bool(value)))
        return bool(value)


def _saved(service):
    return next((c[1] for c in reversed(service.calls) if c[0] == "save"), None)


class ReplaceRuleEditorTests(unittest.IsolatedAsyncioTestCase):
    def _attach(self, service):
        view = ReplaceRuleView()
        controller = ReplaceRuleController(_FakeHub({ReplaceRuleService: service}))
        page = _FakePage()
        controller._page = lambda: page
        controller.attach(view)
        return controller, view, page

    async def test_new_rule_opens_the_editor_instead_of_saving_a_blank(self) -> None:
        """老写法直接存一条 pattern="" 的规则：列表多一行垃圾，替换什么都不发生。"""
        service = _FakeReplaceService()
        controller, view, page = self._attach(service)
        view._add_btn.on_click(None)
        self.assertEqual(len(page.dialogs), 1)
        self.assertEqual([c for c in service.calls if c[0] == "save"], [])
        self.assertIn("新建规则", str(page.dialogs[0].title.value))

    async def test_saving_the_dialog_sends_what_was_typed(self) -> None:
        service = _FakeReplaceService()
        controller, view, page = self._attach(service)
        controller.show_editor()
        fields = controller._fields
        fields["name"].value = "去广告"
        fields["pattern"].value = r"\s*广告.*"
        fields["replacement"].value = ""
        fields["scope"].value = "title"
        fields["is_regex"].value = True
        fields["enabled"].value = False
        save = [b for b in page.dialogs[0].actions if isinstance(b, ft.Button)][0]
        save.on_click(None)
        self.assertTrue(await wait_until(lambda: _saved(service) is not None),
                        str(service.calls))
        rule = _saved(service)
        self.assertEqual(rule["pattern"], r"\s*广告.*")
        self.assertEqual(rule["name"], "去广告")
        self.assertEqual(rule["scope"], "title")
        self.assertTrue(rule["is_regex"])
        self.assertIsNone(rule["id"], "新建不该带 id")
        self.assertFalse(rule["enabled"])
        self.assertEqual(page.popped, 1, "保存后该关掉对话框")

    async def test_editing_prefills_and_keeps_the_id(self) -> None:
        """编辑必须是"改了点东西再存"，不是把原 dict 原样写回去的空操作。"""
        service = _FakeReplaceService(rules=[
            {"id": 7, "name": "旧名", "pattern": "abc", "replacement": "x",
             "scope": "content", "is_regex": True, "enabled": True}])
        controller, view, page = self._attach(service)
        self.assertTrue(await wait_until(lambda: view._rows.controls))
        rule = view._rows.controls[0]
        rule.on_click(SimpleNamespace())
        controller._fields["replacement"].value = "改过的替换文本"
        controller._fields["pattern"].value = "abc"
        save = [b for b in page.dialogs[0].actions if isinstance(b, ft.Button)][0]
        save.on_click(None)
        self.assertTrue(await wait_until(lambda: (_saved(service) or {}).get("id") == 7))
        self.assertEqual(_saved(service)["replacement"], "改过的替换文本")

    async def test_blank_pattern_is_refused(self) -> None:
        service = _FakeReplaceService()
        controller, view, page = self._attach(service)
        controller.show_editor({"id": 3, "pattern": "abc", "name": "n"})
        controller._fields["pattern"].value = "   "
        save = [b for b in page.dialogs[0].actions if isinstance(b, ft.Button)][0]
        save.on_click(None)
        self.assertEqual([c for c in service.calls if c[0] == "save"], [])
        self.assertIn("匹配内容不能为空", str(view._summary.value))

    async def test_global_switch_round_trip(self) -> None:
        """总开关：进页面回填服务状态，拨动写回服务（老代码这条链根本不存在）。"""
        service = _FakeReplaceService(global_on=False)
        controller, view, _page = self._attach(service)
        self.assertTrue(await wait_until(lambda: view._global.value is False))
        view._global.on_change(
            SimpleNamespace(control=SimpleNamespace(value=True)))
        self.assertTrue(await wait_until(lambda: ("global", True) in service.calls))


class ReplaceRuleServiceGlobalTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_switch_lands_on_the_settings_key(self) -> None:
        class _Settings:
            def __init__(self):
                self.store: dict = {}

            def get(self, key, default=None):
                return self.store.get(key, default)

            def set(self, key, value):
                self.store[key] = value

        settings = _Settings()
        service = ReplaceRuleService(settings=settings)
        self.assertTrue(await service.global_enabled())      # 默认开
        self.assertFalse(await service.set_global_enabled(False))
        self.assertEqual(settings.store[GLOBAL_ENABLED_KEY], False)
        self.assertFalse(await service.global_enabled())
        # 关掉开关后正文链路不再叠加（provider 返回空 = 等同未注入）
        self.assertEqual(service._provide_global_rules("content"), [])


class FontFamilyRenderingTests(unittest.TestCase):
    """字体页选中的字族必须真的进到正文控件，且两边读写**同一个** settings 键。"""

    PLAN = [("text", [("　　第一段正文。", False), ("第三百二十章 标题", True)], "")]

    def test_selected_family_is_a_reader_preference_key(self) -> None:
        from app.controllers.tools.font_controller import FONT_FAMILY_KEY
        from app.services.tools.font_service import (SELECTED_FAMILY_KEY,
                                                     FontService)

        class _Settings:
            def __init__(self):
                self.store: dict = {}

            def get(self, key, default=None):
                return self.store.get(key, default)

            def set(self, key, value):
                self.store[key] = value

        settings = _Settings()
        service = FontService(settings=settings)
        service.set_selected_family("楷体")
        self.assertEqual(service.get_selected_family(), "楷体")
        # 阅读器排版偏好统一落在 reader.<偏好名>（ReadingService._READER_PREFIX）；
        # 字体页另写一个键 = 选完永远不变字。
        self.assertEqual(SELECTED_FAMILY_KEY, "reader.font_family")
        self.assertEqual(FONT_FAMILY_KEY, SELECTED_FAMILY_KEY)
        self.assertEqual(settings.store, {"reader.font_family": "楷体"})

    def test_family_reaches_paragraphs_and_restyle(self) -> None:
        from app.views.reading.reader_view import ReaderView
        view = ReaderView()
        view.apply_preferences({"font_family": "霞鹜文楷"})
        view.render_plan(self.PLAN)
        controls = view._content_host.controls
        self.assertEqual(len(controls), 2)
        self.assertTrue(all(c.font_family == "霞鹜文楷" for c in controls),
                        [c.font_family for c in controls])
        # 改回默认：已挂出来的段落要一起退回去，不然只有新加载的章节变回来
        view.apply_preferences({"font_family": ""})
        self.assertTrue(all(c.font_family is None for c in controls))

    def test_unknown_family_does_not_break_defaults(self) -> None:
        from app.views.reading.reader_view import ReaderView
        view = ReaderView()
        self.assertEqual(view.apply_preferences(
            {"font_family": "  思源黑体  "})["font_family"], "思源黑体")
        self.assertEqual(view.apply_preferences(
            {"font_family": None})["font_family"], "")


class _FakeDictService:
    def __init__(self, rules=None):
        self.rules = rules if rules is not None else []
        self.calls: list = []

    async def list_rules(self):
        return list(self.rules)

    async def save_rule(self, rule):
        self.calls.append(("save", dict(rule)))
        return 1

    async def delete_rule(self, rid):
        self.calls.append(("delete", rid))
        return True

    async def lookup(self, word):
        self.calls.append(("lookup", word))
        return "释义正文"


RULES = [{"id": 1, "name": "有道", "url": "https://dict/{{key}}",
          "rules": {"ruleContent": "@css:div#Meaning"}, "enabled": True}]


class DictRuleManagementTests(unittest.IsolatedAsyncioTestCase):
    def _attach(self, service):
        from app.controllers.tools.dict_popup_controller import DictPopupController
        from app.views.tools.dict_popup import DictPopup
        view = DictPopup()
        controller = DictPopupController(_FakeHub({DictService: service}))
        page = _FakePage()
        controller._page = lambda: page
        controller.attach(view)
        return controller, view, page

    async def test_attach_lists_rules(self) -> None:
        """CRUD 建好了没人调用 = 永远「（无释义）」；清单必须自己出现在页上。"""
        service = _FakeDictService(rules=RULES)
        _controller, view, _page = self._attach(service)
        self.assertTrue(await wait_until(lambda: view._rule_rows.controls))
        row = view._rule_rows.controls[0]
        self.assertEqual(row.title.value, "有道")
        self.assertIn("dict/{{key}}", row.subtitle.value)

    async def test_lookup_without_an_enabled_rule_says_why(self) -> None:
        service = _FakeDictService(rules=[])
        controller, view, _page = self._attach(service)
        self.assertTrue(await wait_until(lambda: "还没有词典规则" in str(view._rules_hint.value)))
        controller.lookup("word")
        self.assertIn("还没有启用中的词典规则", str(view._status.value))
        self.assertNotIn(("lookup", "word"), service.calls)

    async def test_lookup_runs_once_a_rule_is_enabled(self) -> None:
        service = _FakeDictService(rules=RULES)
        controller, view, _page = self._attach(service)
        self.assertTrue(await wait_until(lambda: view._rule_rows.controls))
        controller.lookup("word")
        self.assertTrue(await wait_until(lambda: "释义正文" in str(view._definition.value)),
                        str(view._definition.value))

    async def test_new_rule_dialog_saves_typed_fields(self) -> None:
        service = _FakeDictService()
        controller, view, page = self._attach(service)
        view._add_rule_btn.on_click(None)
        self.assertEqual(len(page.dialogs), 1)
        fields = controller._fields
        fields["name"].value = "必应词典"
        fields["url"].value = "https://www.bing.com/dict/{{key}}"
        fields["rule_content"].value = "@css:div#hwd-terms"
        fields["enabled"].value = True
        [b for b in page.dialogs[0].actions if isinstance(b, ft.Button)][0].on_click(None)
        self.assertTrue(await wait_until(
            lambda: any(c[0] == "save" for c in service.calls)), str(service.calls))
        saved = next(c[1] for c in service.calls if c[0] == "save")
        self.assertEqual(saved["url"], "https://www.bing.com/dict/{{key}}")
        self.assertEqual(saved["rules"], {"ruleContent": "@css:div#hwd-terms"})

    async def test_blank_url_is_refused(self) -> None:
        service = _FakeDictService()
        controller, view, page = self._attach(service)
        controller.show_rule_editor()
        controller._fields["url"].value = "  "
        [b for b in page.dialogs[0].actions if isinstance(b, ft.Button)][0].on_click(None)
        # 关框是同步的，保存派发是跨循环的：等框关掉再断"没发过保存"才有意义。
        self.assertTrue(await wait_until(lambda: page.popped == 1))
        self.assertEqual([c for c in service.calls if c[0] == "save"], [])
        self.assertIn("释义页地址不能为空", str(view._rules_hint.value))

    async def test_edit_prefills_and_keeps_the_id(self) -> None:
        service = _FakeDictService(rules=RULES)
        controller, view, page = self._attach(service)
        self.assertTrue(await wait_until(lambda: view._rule_rows.controls))
        view._rule_rows.controls[0].on_click(SimpleNamespace())
        fields = controller._fields
        self.assertEqual(fields["id"], 1)
        self.assertEqual(fields["url"].value, "https://dict/{{key}}")
        self.assertEqual(fields["rule_content"].value, "@css:div#Meaning")

    async def test_delete_forwards_the_id(self) -> None:
        service = _FakeDictService(rules=RULES)
        controller, view, _page = self._attach(service)
        self.assertTrue(await wait_until(lambda: view._rule_rows.controls))
        view._rule_rows.controls[0].trailing.on_click(None)
        self.assertTrue(await wait_until(lambda: ("delete", 1) in service.calls))


# --------------------------------------------------------------------------- #
# 备份：kind 真的裁剪 zip 成员 + 远端清单可选（不用手敲 URL）
# --------------------------------------------------------------------------- #
class _WdSource:
    """SourceService 替身：备份要的两样（导出文本 + bookshelf 属性）。"""

    def __init__(self):
        self.bookshelf = [{"bookUrl": "b1", "name": "书架上的书"}]
        self.imported: list = []

    async def export_sources_json(self):
        return json.dumps([{"bookSourceUrl": "s1", "bookSourceName": "假源"}])

    async def import_sources_json(self, text, mode="append"):
        self.imported.append(text)
        return 1, []

    def save_bookshelf(self):
        pass


class _WdSettings:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.set_calls: list = []

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value
        self.set_calls.append((key, value))


class _WdDict:
    def __init__(self):
        self.exported = json.dumps(
            [{"name": "有道", "url": "https://dict/{{key}}",
              "rules": {"ruleContent": "@css:div"}, "enabled": True}],
            ensure_ascii=False)
        self.imported: list = []

    async def export_rules_json(self):
        return self.exported

    async def import_rules_json(self, text):
        self.imported.append(text)
        return 1, []


class _WdClient:
    """WebDavClient 替身：上传的字节原样存着，download 按路径取回（离线闭环）。"""

    def __init__(self, entries=None):
        self.uploads: dict[str, bytes] = {}
        self.entries = entries or []

    async def mkdir(self, path):
        return None

    async def upload(self, path, data):
        self.uploads[path] = data

    async def download(self, path):
        if path not in self.uploads:
            raise RuntimeError(f"远端没有该文件: {path}")
        return self.uploads[path]

    async def list(self, path=""):
        return list(self.entries)


def _wd_service(client=None, settings=None, source=None, rss=None):
    from app.services.tools.web_dav_service import WebDavService
    svc = WebDavService(source_service=source or _WdSource(),
                        settings=settings or _WdSettings(),
                        client=client or _WdClient(),
                        dict_service=_WdDict(), rss_service=rss)
    return svc


def _zip_names(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return sorted(zf.namelist())


REMOTE_ZIP = "legado/backup/legado_backup_dict_20260926_101010.zip"


class BackupTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_kind_trims_the_zip_members(self) -> None:
        """「备份对象」下拉以前只是事件标签：选词典也整库上传。"""
        client = _WdClient()
        svc = _wd_service(client=client)
        result = await svc.backup("dict")
        self.assertTrue(result.ok, result.detail)
        (path, data), = client.uploads.items()
        self.assertEqual(_zip_names(data), ["dictRules.json", "meta.json"])
        self.assertIn("_dict_", path)

    async def test_all_still_backs_up_everything(self) -> None:
        client = _WdClient()
        svc = _wd_service(client=client)
        self.assertTrue((await svc.backup("all")).ok)
        names = _zip_names(next(iter(client.uploads.values())))
        for member in ("bookSources.json", "bookShelf.json", "config.json",
                       "dictRules.json", "meta.json"):
            self.assertIn(member, names)

    async def test_unknown_kind_is_refused_before_upload(self) -> None:
        client = _WdClient()
        svc = _wd_service(client=client)
        result = await svc.backup("瞎写的")
        self.assertFalse(result.ok)
        self.assertIn("未知备份对象", result.detail)
        self.assertEqual(client.uploads, {})

    async def test_dict_backup_restores_into_the_dict_service(self) -> None:
        """备份→列出→恢复闭环（全程离线，客户端是内存替身）。"""
        client = _WdClient()
        svc = _wd_service(client=client)
        path = (await svc.backup("dict")).path
        entries = [{"name": path.rsplit("/", 1)[-1], "path": path,
                    "size": 128, "modified": "Mon, 26 Sep 2026", "is_dir": False}]
        client.entries = entries
        remote = await svc.list_remote()
        self.assertEqual([e["path"] for e in remote], [path])
        # confirm=True：不确认会在闸上直接返回（连下载都不做），这条闭环就转不起来
        result = await svc.restore(remote[0]["path"], confirm=True)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(json.loads(svc._dict.imported[0])[0]["name"], "有道")

    async def test_settings_member_uses_the_keys_the_reader_actually_writes(self) -> None:
        """备份成员里的键必须是 reader.<偏好名>：以前列的是没人写过的键名。"""
        settings = _WdSettings({
            "reader.font_size": 18, "reader.font_family": "楷体",
            "reader.font.size": 999,          # 旧白名单里的假键
            "webdav.password": "secrecy"})
        client = _WdClient()
        svc = _wd_service(client=client, settings=settings)
        self.assertTrue((await svc.backup("config")).ok)
        with zipfile.ZipFile(io.BytesIO(next(iter(client.uploads.values())))) as zf:
            config = json.loads(zf.read("config.json").decode("utf-8"))
        self.assertEqual(config.get("reader.font_size"), 18)
        self.assertEqual(config.get("reader.font_family"), "楷体")
        self.assertNotIn("reader.font.size", config)
        self.assertNotIn("webdav.password", config)


def _fake_page(controller):
    """给控制器接一只假 page：确认框只认 show_dialog / pop_dialog。"""
    page = SimpleNamespace(dialogs=[], popped=0)

    def show_dialog(dialog):
        page.dialogs.append(dialog)

    def pop_dialog():
        page.popped += 1
        if page.dialogs:
            page.dialogs.pop()

    page.show_dialog = show_dialog
    page.pop_dialog = pop_dialog
    controller._page = lambda: page
    return page


class _FakeWebDavService:
    def __init__(self, entries=None):
        self.entries = entries if entries is not None else []
        self.calls: list = []
        self.delay = 0.0            # >0 就能把 restore 卡在途里（跨循环别用 Event）
        self.backup_delay = 0.0     # 同理，把 backup 卡在途里

    async def backup(self, kind="all"):
        self.calls.append(("backup", kind))
        if self.backup_delay:
            await asyncio.sleep(self.backup_delay)
        return SimpleNamespace(ok=True, detail=f"{kind} 备份完成",
                               path=REMOTE_ZIP, size=10, members={})

    async def list_remote(self):
        self.calls.append(("list",))
        return list(self.entries)

    async def restore(self, path, token="", confirm=False):
        self.calls.append(("restore", path, token, confirm))
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(ok=True, detail="恢复成功", path=path)


class WebDavRemoteListTests(unittest.IsolatedAsyncioTestCase):
    def _attach(self, service):
        from app.controllers.tools.webdav_controller import WebDavController
        from app.services.tools.web_dav_service import WebDavService
        from app.views.tools.webdav_view import WebDavView
        view = WebDavView()
        controller = WebDavController(_FakeHub({WebDavService: service}))
        controller._bus = _FakeBus()
        controller.attach(view)
        return controller, view

    async def test_listing_remotes_fills_the_dropdown(self) -> None:
        service = _FakeWebDavService([
            {"name": "a.zip", "path": "legado/backup/a.zip", "size": 2048,
             "modified": "Mon, 26 Sep 2026 10:00:00 GMT"}])
        _controller, view = self._attach(service)
        view._list_remote_btn.on_click(None)
        self.assertTrue(await wait_until(lambda: ("list",) in service.calls),
                        str(service.calls))
        self.assertTrue(await wait_until(
            lambda: [o.key for o in view._remote.options] == ["legado/backup/a.zip"]),
                        str(view._remote.options))
        self.assertIn("2048 字节", view._remote.options[0].text)

    async def test_picking_a_remote_fills_the_restore_path(self) -> None:
        """恢复路径以前要手敲一个 zip URL：现在点一下就填好。"""
        service = _FakeWebDavService([
            {"name": "a.zip", "path": "legado/backup/a.zip", "size": 1,
             "modified": ""}])
        _controller, view = self._attach(service)
        view._list_remote_btn.on_click(None)
        self.assertTrue(await wait_until(lambda: view._remote.options))
        view._remote.value = "legado/backup/a.zip"
        view._remote.on_select(SimpleNamespace(value="legado/backup/a.zip"))
        self.assertEqual(view._restore_path.value, "legado/backup/a.zip")

    async def test_restore_button_asks_before_touching_anything(self) -> None:
        """点「从云端恢复」只弹确认，不许直接落库：覆盖的是整张书源表，
        而点的人根本看不见包里是什么（导入的整表替换尚且要确认）。"""
        service = _FakeWebDavService()
        controller, view = self._attach(service)
        page = _fake_page(controller)
        view._restore_path.value = "legado/backup/a.zip"
        view._restore_btn.on_click(None)
        self.assertEqual([c for c in service.calls if c[0] == "restore"], [])
        self.assertTrue(page.dialogs, "没弹出确认框")
        labels = [str(getattr(a, "content", "")) for a in page.dialogs[0].actions]
        self.assertIn("取消", labels)
        self.assertIn("确认恢复", labels)

    async def test_confirming_the_dialog_runs_the_restore(self) -> None:
        service = _FakeWebDavService()
        controller, view = self._attach(service)
        page = _fake_page(controller)
        view._restore_path.value = "legado/backup/a.zip"
        view._restore_btn.on_click(None)
        confirm = [a for a in page.dialogs[0].actions
                   if str(getattr(a, "content", "")) == "确认恢复"][0]
        confirm.on_click(None)
        self.assertTrue(await wait_until(lambda: (
            "restore", "legado/backup/a.zip", "", True) in service.calls),
            str(service.calls))
        # 确认框要收掉：留着下一轮点开的还是它
        self.assertEqual(page.popped, 1)

    async def test_cancel_leaves_the_local_data_alone(self) -> None:
        service = _FakeWebDavService()
        controller, view = self._attach(service)
        page = _fake_page(controller)
        view._restore_path.value = "legado/backup/a.zip"
        view._restore_btn.on_click(None)
        cancel = [a for a in page.dialogs[0].actions
                  if str(getattr(a, "content", "")) == "取消"][0]
        cancel.on_click(None)
        self.assertEqual(page.popped, 1)
        self.assertEqual([c for c in service.calls if c[0] == "restore"], [])

    async def test_a_second_confirm_while_one_is_running_is_refused(
        self) -> None:
        """连点两下「确认恢复」不能覆盖两次：第二次的"恢复前留底"抄的是第一次
        恢复完的样子，真正的旧数据已经没了。"""
        service = _FakeWebDavService()
        service.delay = 0.5                     # 第一趟停在服务里（半秒）
        controller, view = self._attach(service)
        view._restore_path.value = "legado/backup/a.zip"
        controller.restore("legado/backup/a.zip", confirmed=True)
        self.assertTrue(await wait_until(lambda: len(service.calls) == 1),
                        str(service.calls))

        controller.restore("legado/backup/a.zip", confirmed=True)
        self.assertEqual(len(service.calls), 1, "在途期间又派发了一次恢复")
        self.assertIn("上一次恢复还在进行", str(view._log.value))

        self.assertTrue(await wait_until(
            lambda: not controller._restore_inflight),
            "收尾后标志没落回：这个页面再也恢复不动了")
        controller.restore("legado/backup/a.zip", confirmed=True)
        self.assertTrue(await wait_until(lambda: len(service.calls) == 2),
                        str(service.calls))

    async def test_restore_without_a_page_refuses_instead_of_asking_nothing(
        self) -> None:
        """拿不到 page 就拒绝执行，而不是"问不了那就不问了"直接恢复。"""
        service = _FakeWebDavService()
        _controller, view = self._attach(service)
        view._restore_path.value = "legado/backup/a.zip"
        view._restore_btn.on_click(None)
        self.assertIn("界面尚未就绪", str(view._log.value))
        self.assertEqual([c for c in service.calls if c[0] == "restore"], [])

    async def test_empty_remote_list_says_so(self) -> None:
        service = _FakeWebDavService(entries=[])
        _controller, view = self._attach(service)
        view._list_remote_btn.on_click(None)
        self.assertTrue(await wait_until(
            lambda: "远端还没有备份包" in str(view._log.value)), str(view._log.value))
        self.assertTrue(view._remote.disabled)

    async def test_backup_button_sends_the_key_not_the_label(self) -> None:
        service = _FakeWebDavService()
        _controller, view = self._attach(service)
        view._kind.value = "dict"
        view._backup_btn.on_click(None)
        self.assertTrue(await wait_until(
            lambda: ("backup", "dict") in service.calls), str(service.calls))
        self.assertIn("词典", str(view._log.value))

    async def test_restore_without_a_path_is_refused(self) -> None:
        service = _FakeWebDavService()
        _controller, view = self._attach(service)
        view._restore_path.value = ""
        view._restore_btn.on_click(None)
        self.assertIn("列出远端备份", str(view._log.value))
        self.assertEqual([c for c in service.calls if c[0] == "restore"], [])


class _SubRecordingBus(_FakeBus):
    """记下订阅的总线替身：不把处理器记下来，就没法把在途事件灌回控制器。"""

    def __init__(self) -> None:
        self.subscriptions: list = []

    def subscribe(self, event_type, handler):
        self.subscriptions.append((event_type, handler))
        return lambda: None

    def handlers(self, event_type) -> list:
        return [h for t, h in self.subscriptions if t is event_type]


class WebDavProgressTests(unittest.IsolatedAsyncioTestCase):
    """备份/恢复的逐阶段进度与收尾（BackupProgressEvent / BackupFinishedEvent）。"""

    def _attach(self, service):
        from app.controllers.tools.webdav_controller import WebDavController
        from app.services.tools.web_dav_service import WebDavService
        from app.views.tools.webdav_view import WebDavView
        view = WebDavView()
        bus = _SubRecordingBus()
        controller = WebDavController(_FakeHub({WebDavService: service}), bus=bus)
        controller.attach(view)
        return controller, view

    async def test_both_backup_events_are_subscribed(self) -> None:
        """进度事件定义了没人订阅 = 功能照旧没做（服务发了 ≠ 界面收到）。"""
        from app.core.events import BackupFinishedEvent, BackupProgressEvent
        _controller, _view = self._attach(_FakeWebDavService())
        self.assertEqual(len(_controller._bus.handlers(BackupProgressEvent)), 1)
        self.assertEqual(len(_controller._bus.handlers(BackupFinishedEvent)), 1)

    async def test_progress_event_fills_the_stage_line_and_bar(self) -> None:
        from app.core.events import BackupProgressEvent
        controller, view = self._attach(_FakeWebDavService())
        handler = controller._bus.handlers(BackupProgressEvent)[0]

        await handler(BackupProgressEvent(kind="backup", stage="正在导出书源…"))
        self.assertEqual(view._stage.value, "正在导出书源…")
        self.assertTrue(view._progress.visible)
        self.assertIsNone(view._progress.value, "给不出条数时该是不定长动画")

        await handler(BackupProgressEvent(kind="backup", stage="正在校验 书架（2/3）",
                                          done=2, total=3))
        self.assertAlmostEqual(view._progress.value, 2 / 3)

    async def test_the_bar_is_up_before_the_service_answers(self) -> None:
        """点下去那一拍就要有反馈：ui_call 派出去之后界面循环里什么都还没发生。

        替身带 ``backup_delay``：假服务瞬时返回时，收尾的 ``_after`` 可能就在这一句
        断言之前的空档里把条关掉（第一次全量跑正是这么红的），测的就不再是"立起来"。
        """
        service = _FakeWebDavService()
        service.backup_delay = 0.4
        controller, view = self._attach(service)
        view._kind.value = "dict"
        view._backup_btn.on_click(None)
        self.assertTrue(view._progress.visible, "点了立即备份，进度条没出现")
        self.assertIn("词典", view._stage.value)
        self.assertTrue(await wait_until(lambda: not view._progress.visible),
                        "收尾后进度条还挂着")

    async def test_finished_event_closes_the_bar(self) -> None:
        from app.core.events import BackupFinishedEvent, BackupProgressEvent
        controller, view = self._attach(_FakeWebDavService())
        await controller._bus.handlers(BackupProgressEvent)[0](
            BackupProgressEvent(kind="backup", stage="正在上传…"))
        self.assertTrue(view._progress.visible)
        await controller._bus.handlers(BackupFinishedEvent)[0](
            BackupFinishedEvent(kind="backup", ok=True, detail="备份成功"))
        self.assertFalse(view._progress.visible, "备份都成功了进度条还在转")

    async def test_a_page_initiated_backup_reports_the_result_once(self) -> None:
        """结果行只有一支笔：本页发起的动作归 future 收尾写，事件不许再改一次字。

        两条路都写时，最终上屏的是哪句取决于事件与回调谁先跑到——同一件事两种说法。
        """
        from app.core.events import BackupFinishedEvent
        controller, view = self._attach(_FakeWebDavService())
        view._kind.value = "dict"
        view._backup_btn.on_click(None)
        self.assertTrue(await wait_until(
            lambda: "词典" in str(view._log.value)), str(view._log.value))
        written = str(view._log.value)

        await controller._bus.handlers(BackupFinishedEvent)[0](
            BackupFinishedEvent(kind="dict", ok=True, detail="另一套措辞"))
        self.assertEqual(str(view._log.value), written,
                         "事件路径把本页已经报过的结果行改写成了另一种说法")

    async def test_a_finished_from_elsewhere_still_reports(self) -> None:
        """别处触发的备份（脚本 / 网络桥）没有本页的 future 收尾，只能由事件报。"""
        from app.core.events import BackupFinishedEvent
        controller, view = self._attach(_FakeWebDavService())
        await controller._bus.handlers(BackupFinishedEvent)[0](
            BackupFinishedEvent(kind="restore", ok=False, detail="校验失败"))
        self.assertIn("校验失败", str(view._log.value))
        self.assertIn("失败", str(view._log.value))

    async def test_late_events_after_detach_touch_no_widgets(self) -> None:
        """EventBus 异步派发：detach 挡不住已排进去的事件，处理器自己要认视图没了。"""
        from app.core.events import BackupFinishedEvent, BackupProgressEvent
        controller, view = self._attach(_FakeWebDavService())
        progress = controller._bus.handlers(BackupProgressEvent)[0]
        finished = controller._bus.handlers(BackupFinishedEvent)[0]
        controller.detach()
        view._stage.value = "（切页前的阶段行）"
        view._progress.visible = True
        await progress(BackupProgressEvent(kind="backup", stage="正在导出书源…"))
        await finished(BackupFinishedEvent(kind="backup", ok=True, detail="备份成功"))
        self.assertEqual(view._stage.value, "（切页前的阶段行）")
        self.assertTrue(view._progress.visible, "detach 后的在途事件把控件改了")
        self.assertEqual(str(view._log.value), "", "detach 后不该再写结果行")


class ExportProgressTests(unittest.IsolatedAsyncioTestCase):
    """整本导出的逐章进度（ExportProgressEvent → 导出页进度行）。"""

    async def _pick_one(self, controller, view, service):
        """走到"选中一本书、能点导出"这一步（导出按钮默认禁用）。"""
        self.assertTrue(await wait_until(lambda: bool(_option_keys(view))))
        _pick(view, BOOKS[0]["url"])

    async def test_export_progress_event_is_subscribed(self) -> None:
        """事件定义了没人订阅 = 这条链照旧没做（服务发了 ≠ 界面收到）。"""
        from app.core.events import ExportProgressEvent
        service = _FakeExportService()
        bus = _SubRecordingBus()
        controller, view = _attach(service, bus)
        self.assertEqual(len(bus.handlers(ExportProgressEvent)), 1,
                         "导出页没订阅进度事件")

    async def test_a_beat_fills_the_stage_line_and_the_bar(self) -> None:
        from app.core.events import ExportProgressEvent
        service = _FakeExportService(export_delay=0.4)
        bus = _SubRecordingBus()
        controller, view = _attach(service, bus)
        await self._pick_one(controller, view, service)
        view._txt_btn.on_click(None)          # 在途才有进度可显示
        beat = bus.handlers(ExportProgressEvent)[0]

        await beat(ExportProgressEvent(kind="txt", stage="正在写入第 3 章",
                                       done=3, total=10))
        self.assertEqual(view._stage.value, "正在写入第 3 章")
        self.assertTrue(view._progress.visible)
        self.assertAlmostEqual(view._progress.value, 0.3)

        await beat(ExportProgressEvent(kind="txt", stage="正在打包 epub"))
        self.assertIsNone(view._progress.value, "给不出分母时该走不定长动画")
        self.assertEqual(view._stage.value, "正在打包 epub")

    async def test_the_bar_is_up_before_the_service_answers(self) -> None:
        """点下去那一拍就要有反馈：ui_call 派出去之后界面循环里什么都还没发生。"""
        service = _FakeExportService(export_delay=0.4)
        controller, view = _attach(service)
        await self._pick_one(controller, view, service)
        view._txt_btn.on_click(None)
        self.assertTrue(view._progress.visible, "点了导出，进度条没出现")
        self.assertTrue(view._txt_btn.disabled, "导出途中还能再点一次")
        self.assertTrue(await wait_until(lambda: not view._progress.visible),
                        "导出收尾后进度条还挂着")

    async def test_a_beat_arriving_after_the_finish_does_not_reopen_the_bar(self):
        """进度是异步投递的：收尾之后迟到的那一拍不许把条再支起来。

        症状是"结果都出来了，进度条还在转"（备份那一轮踩过同一处）。
        """
        from app.core.events import ExportProgressEvent
        service = _FakeExportService()
        bus = _SubRecordingBus()
        controller, view = _attach(service, bus)
        await self._pick_one(controller, view, service)
        view._epub_btn.on_click(None)
        self.assertTrue(await wait_until(
            lambda: "已导出" in str(view._log.value)), str(view._log.value))
        self.assertFalse(view._progress.visible)

        await bus.handlers(ExportProgressEvent)[0](
            ExportProgressEvent(kind="epub", stage="迟到的最后一拍", done=9, total=10))
        self.assertFalse(view._progress.visible, "迟到的进度把条支回来了")
        self.assertEqual(view._stage.value, "", "迟到的进度改了阶段行")

    async def test_a_failed_export_closes_the_bar_and_says_so(self) -> None:
        """失败也要把在途的痕迹擦干净：条、阶段行、可点状态三样都回位。"""
        from app.core.events import ExportProgressEvent
        service = _FakeExportService(export_delay=0.4,
                                     export_fail=RuntimeError("该书无已缓存章节"))
        bus = _SubRecordingBus()
        controller, view = _attach(service, bus)
        await self._pick_one(controller, view, service)
        view._txt_btn.on_click(None)
        await bus.handlers(ExportProgressEvent)[0](
            ExportProgressEvent(kind="txt", stage="正在写入第 1 章", done=1, total=5))
        self.assertTrue(view._progress.visible)

        self.assertTrue(await wait_until(
            lambda: "导出失败" in str(view._log.value)), str(view._log.value))
        self.assertIn("该书无已缓存章节", str(view._log.value))
        self.assertFalse(view._progress.visible, "导出都失败了进度条还在转")
        self.assertEqual(view._stage.value, "", "上一阶段的说法留在那儿了")
        self.assertFalse(view._txt_btn.disabled, "失败后该能再点一次")

    async def test_late_beats_after_detach_touch_no_widgets(self) -> None:
        """EventBus 异步派发：detach 挡不住已排进去的事件，处理器自己要认视图没了。"""
        from app.core.events import ExportProgressEvent
        service = _FakeExportService(export_delay=0.4)
        bus = _SubRecordingBus()
        controller, view = _attach(service, bus)
        await self._pick_one(controller, view, service)
        view._txt_btn.on_click(None)          # 让在途标记为真，才能只测视图没了这一半
        beat = bus.handlers(ExportProgressEvent)[0]
        controller.detach()
        view._stage.value = "（切页前的阶段行）"
        view._progress.visible = False
        await beat(ExportProgressEvent(kind="txt", stage="正在写入第 1 章",
                                       done=1, total=5))
        self.assertEqual(view._stage.value, "（切页前的阶段行）")
        self.assertFalse(view._progress.visible, "detach 后的在途进度把控件改了")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
