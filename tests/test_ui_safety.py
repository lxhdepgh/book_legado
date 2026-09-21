import csv
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import flet as ft

from app.models.book_source import BookSource
from app.services import check_service
from app.views.manage_view import ManageView
from app.views import check_view
from app.views.check_view import CheckView
from app.ui_feedback import show_snack_bar


class UiSafetyTests(unittest.TestCase):
    def test_snack_bar_uses_flet_dialog_api(self):
        shown = []
        page = SimpleNamespace(show_dialog=shown.append)
        snack = object()
        show_snack_bar(page, snack)
        self.assertEqual([snack], shown)

    def test_single_source_toggle_rolls_back_when_save_fails(self):
        source = BookSource(bookSourceName="A", bookSourceUrl="https://a")
        service = SimpleNamespace(sources=[source], save=lambda: None)
        view = ManageView(service)
        view._render_page = lambda: None

        with patch.object(service, "save", side_effect=RuntimeError("disk full")):
            view._toggle_enabled(source, False)

        self.assertTrue(source.enabled)

    def test_source_rows_wire_secondary_tap_and_long_press_to_menu(self):
        source = BookSource(bookSourceName="A", bookSourceUrl="https://a")
        view = ManageView(SimpleNamespace(sources=[source], save=lambda: None))
        detector = view._build_card(0, source).content
        self.assertIsInstance(detector, ft.GestureDetector)
        self.assertIsNotNone(detector.on_secondary_tap_down)
        self.assertIsNotNone(detector.on_long_press)
        with patch.object(view, "_show_source_menu") as menu:
            detector.on_secondary_tap_down(object())
            detector.on_long_press(object())
        self.assertEqual(menu.call_args_list[0].args[0], source)
        self.assertEqual(menu.call_count, 2)

    def test_source_context_menu_lists_high_frequency_actions(self):
        source = BookSource(bookSourceName="A", bookSourceUrl="https://a",
                            bookSourceGroup="g1")
        service = SimpleNamespace(
            sources=[source], save=lambda: None, get_groups=lambda: ["g1"],
            batch_delete=lambda items: None)
        view = ManageView(service)
        view._render_page = lambda: None
        view._apply_filters = lambda update_groups=False: None
        dialogs = []
        page = SimpleNamespace(show_dialog=dialogs.append,
                               pop_dialog=lambda: None)
        with patch.object(ManageView, "page", new_callable=PropertyMock,
                          return_value=page):
            view._show_source_menu(source)
        self.assertEqual(len(dialogs), 1)
        self.assertEqual([control.content for control in dialogs[0].actions],
                         ["关闭", "停用", "保存分组", "删除"])
        with patch.object(ManageView, "page", new_callable=PropertyMock,
                          return_value=page):
            view._menu_save_group(source, ft.TextField(label="分组", value="g2"))
        self.assertEqual(source.bookSourceGroup, "g2")

    def test_source_group_save_rolls_back_when_save_fails(self):
        source = BookSource(bookSourceName="A", bookSourceUrl="https://a",
                            bookSourceGroup="g1")
        service = SimpleNamespace(sources=[source], save=lambda: None)
        view = ManageView(service)
        view._render_page = lambda: None
        view._apply_filters = lambda update_groups=False: None
        shown = []
        page = SimpleNamespace(show_dialog=shown.append,
                               pop_dialog=lambda: None)
        with patch.object(ManageView, "page", new_callable=PropertyMock,
                          return_value=page), \
                patch.object(service, "save", side_effect=RuntimeError("disk full")):
            view._menu_save_group(source, ft.TextField(label="分组", value="g2"))
        self.assertEqual(source.bookSourceGroup, "g1")

    def test_background_check_can_update_after_view_unmounts(self):
        view = CheckView(SimpleNamespace())
        view.update = unittest.mock.Mock(side_effect=RuntimeError("not mounted"))

        view._safe_update()

        view.update.assert_called_once()

    def test_check_failure_snack_bar_offers_retry(self):
        view = CheckView(SimpleNamespace())
        view.update = lambda: None
        shown = []
        started = []
        page = SimpleNamespace(
            show_dialog=shown.append, pop_dialog=lambda: None,
            run_task=lambda target: (
                started.append(target),
                target.close() if hasattr(target, "close") else None))
        with patch.object(CheckView, "page", new_callable=PropertyMock,
                          return_value=page):
            view._show_check_error(RuntimeError("网络断了"))
        snack = shown[0]
        self.assertIsInstance(snack, ft.SnackBar)
        self.assertEqual(snack.content.value, "检测失败：网络断了")
        self.assertEqual(snack.action, "重试")
        self.assertEqual(view._summary.value, "检测失败：网络断了")
        snack.on_action(None)
        self.assertEqual(len(started), 1)


class CheckSafetyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _patch_unusable():
        async def probe(*args, **kwargs):
            return {"status": "ok", "status_code": 200,
                    "response_time_ms": 1, "error": None}

        async def search(*args, **kwargs):
            return ({"search_status": "rule_error", "book_count": None,
                     "search_keyword": "test", "search_method": "rule-engine",
                     "search_error": "broken"}, [])

        class Engine:
            def __init__(self, **kwargs):
                self.js = SimpleNamespace(close=lambda: None)

        return (patch.object(check_service, "_probe_source", probe),
                patch.object(check_service, "_engine_search_check", search),
                patch.object(check_service, "LegadoReadingEngine", Engine))

    async def test_check_does_not_disable_by_default(self):
        source = SimpleNamespace(
            bookSourceName="bad", bookSourceUrl="https://bad",
            searchUrl="", ruleSearch={}, ruleExplore={}, header="",
            enabled=True, enabledExplore=False, exploreUrl="",
            to_dict=lambda: {"bookSourceName": "bad",
                             "bookSourceUrl": "https://bad"})

        first, second, third = self._patch_unusable()
        with first, second, third:
            result = await check_service.check_sources([source], timeout=0.1)

        self.assertFalse(result[0]["auto_disabled"])
        self.assertTrue(source.enabled)

    async def test_check_only_disables_when_explicitly_requested(self):
        source = SimpleNamespace(
            bookSourceName="bad", bookSourceUrl="https://bad",
            searchUrl="", ruleSearch={}, ruleExplore={}, header="",
            enabled=True, enabledExplore=False, exploreUrl="",
            to_dict=lambda: {"bookSourceName": "bad",
                             "bookSourceUrl": "https://bad"})

        first, second, third = self._patch_unusable()
        with first, second, third:
            result = await check_service.check_sources(
                [source], timeout=0.1, auto_disable=True)

        self.assertTrue(result[0]["auto_disabled"])
        self.assertFalse(source.enabled)


class CheckResultViewTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _view(sources=None, data_dir=None):
        service = SimpleNamespace(
            sources=sources or [], category_checks={}, save=lambda: None,
            data_dir=data_dir)
        view = CheckView(service)
        view.update = lambda: None
        return view

    @staticmethod
    def _result(name, url, state, ms=None):
        return {"name": name, "raw_url": url, "status": "ok",
                "check_state": state, "response_time_ms": ms}

    def test_state_filter_and_sort_narrow_visible_results(self):
        view = self._view()
        view._store_result(self._result("a", "https://a", "unusable", 30))
        view._store_result(self._result("b", "https://b", "fully_usable", 5))
        view._store_result(self._result("c", "https://c", "unusable", 15))
        view._state_filter.value = "unusable"
        self.assertEqual([row["name"] for row in view._visible_results()],
                         ["a", "c"])
        view._state_filter.value = "all"
        view._sort_select.value = "respond"
        self.assertEqual([row["name"] for row in view._visible_results()],
                         ["b", "c", "a"])
        view._sort_select.value = "state"
        self.assertEqual([row["name"] for row in view._visible_results()],
                         ["b", "a", "c"])
        view._render_rows()
        self.assertEqual([tile.title.value for tile in view._rows.controls],
                         ["b", "a", "c"])

    async def test_progress_advances_even_while_rows_are_batched_away(self):
        sources = [SimpleNamespace(bookSourceUrl=f"https://s{index}", enabled=True)
                   for index in range(25)]
        view = self._view(sources)
        # A state filter that hides every result must not stall the bar: rows are
        # batched, progress is pushed on its own.
        view._state_filter.value = "blocked"
        seen = []
        view._safe_update = lambda: seen.append(view._progress.value)

        async def fake_check(items, concurrency=8, timeout=12, on_result=None,
                             auto_disable=False):
            results = []
            for index, item in enumerate(items):
                result = self._result(item.bookSourceUrl, item.bookSourceUrl,
                                      "fully_usable", 5)
                results.append(result)
                on_result(result, index + 1, len(items))
            return results

        page = SimpleNamespace(show_dialog=lambda snack: None,
                               pop_dialog=lambda: None,
                               run_task=lambda handler: None)
        with patch.object(check_view, "check_sources", fake_check), \
                patch.object(CheckView, "PROGRESS_REFRESH_SECONDS", 0.0), \
                patch.object(CheckView, "page", new_callable=PropertyMock,
                             return_value=page):
            await view._run_check()
        self.assertEqual(len(set(seen)), len(sources) + 1)
        self.assertEqual(seen[-1], 1.0)
        self.assertEqual(list(view._rows.controls), [])

    def test_retry_keeps_original_slot_when_result_refreshed(self):
        view = self._view()
        view._store_result(self._result("a", "https://a", "unknown", 10))
        view._store_result(self._result("b", "https://b", "fully_usable", 20))
        refreshed = self._result("a", "https://a", "fully_usable", 8)
        view._store_result(refreshed)
        self.assertEqual(refreshed["_order"], 0)
        self.assertEqual([row["name"] for row in view._visible_results()],
                         ["a", "b"])
        self.assertEqual(len(view._results), 2)

    def test_retry_failed_targets_only_failed_sources(self):
        bad = SimpleNamespace(bookSourceUrl="https://bad", enabled=True)
        good = SimpleNamespace(bookSourceUrl="https://good", enabled=True)
        view = self._view(sources=[bad, good])
        view._store_result(self._result("bad", "https://bad", "content_unavailable"))
        view._store_result(self._result("good", "https://good", "fully_usable"))
        started = []
        page = SimpleNamespace(
            show_dialog=lambda snack: None, pop_dialog=lambda: None,
            run_task=lambda target: (
                started.append(target),
                target.close() if hasattr(target, "close") else None))
        with patch.object(CheckView, "page", new_callable=PropertyMock,
                          return_value=page):
            view._retry_failed(None)
        self.assertEqual(view._retry_targets, [bad])
        self.assertEqual(len(started), 1)

    def test_retry_failed_without_failures_only_notifies(self):
        view = self._view()
        view._store_result(self._result("good", "https://good", "fully_usable"))
        shown = []
        page = SimpleNamespace(show_dialog=shown.append,
                               pop_dialog=lambda: None,
                               run_task=lambda target: None)
        with patch.object(CheckView, "page", new_callable=PropertyMock,
                          return_value=page):
            view._retry_failed(None)
        self.assertIsNone(view._retry_targets)
        self.assertEqual(shown[0].content.value, "没有需要重测的失败/待确认书源")

    async def test_sample_scope_limits_sources_and_full_run_asks_first(self):
        sources = [SimpleNamespace(bookSourceUrl=f"https://s{i}", enabled=True,
                                   bookSourceName=f"s{i}")
                   for i in range(check_view.CHECK_SAMPLE_SIZE + 20)]
        view = self._view(sources=sources)
        seen = []

        async def fake_check(items, **kwargs):
            seen.append(len(items))
            return []

        asked = []
        page = SimpleNamespace(show_dialog=asked.append, pop_dialog=lambda: None,
                               run_task=lambda target: None)
        with patch.object(check_view, "check_sources", fake_check), \
                patch.object(CheckView, "page", new_callable=PropertyMock,
                             return_value=page):
            await view._run_check()
            self.assertEqual(seen, [check_view.CHECK_SAMPLE_SIZE])

            view._scope.value = "all"
            view._running = False
            view._start_check(None)
            self.assertEqual(len(asked), 1)
            self.assertEqual(seen, [check_view.CHECK_SAMPLE_SIZE])

    def test_header_lays_out_toolbar_progress_and_summary_in_order(self):
        view = self._view()
        header = view.controls[0].content
        self.assertIsInstance(header, ft.Column)
        toolbar, progress, summary = header.controls[-3:]
        self.assertIsInstance(toolbar, ft.Row)
        self.assertTrue(toolbar.wrap)
        # An expanded child inside a wrapped Row gets unbounded main-axis
        # width and collapses the whole header, progress bar included.
        offenders = [control for control in toolbar.controls
                     if getattr(control, "expand", None)]
        self.assertEqual(offenders, [])
        self.assertIs(progress, view._progress)
        self.assertIs(summary, view._summary)
        self.assertIn(view._concurrency, toolbar.controls)
        self.assertEqual([option.key for option in view._concurrency.options],
                         ["4", "8", "12", "16"])
        self.assertEqual(view._concurrency_value(), check_service.CHECK_CONCURRENCY)

    async def test_concurrency_dropdown_reaches_check_sources(self):
        view = self._view(sources=[SimpleNamespace(
            bookSourceUrl="https://s", bookSourceName="s", enabled=True)])
        seen = []

        async def fake_check(items, **kwargs):
            seen.append(kwargs.get("concurrency"))
            return []

        page = SimpleNamespace(show_dialog=lambda snack: None,
                               pop_dialog=lambda: None,
                               run_task=lambda target: None)
        with patch.object(check_view, "check_sources", fake_check), \
                patch.object(CheckView, "page", new_callable=PropertyMock,
                             return_value=page):
            for value in ("4", "16", "garbage"):
                view._concurrency.value = value
                view._running = False
                await view._run_check()
        self.assertEqual(seen, [4, 16, check_service.CHECK_CONCURRENCY])

    def test_blocked_source_is_never_a_retry_target(self):
        blocked = SimpleNamespace(bookSourceUrl="https://blocked", enabled=True)
        view = self._view(sources=[blocked])
        view._store_result({**self._result("blocked", "https://blocked", "blocked"),
                            "error": "本机安全软件已拦截该站点（火绒）"})
        self.assertIn("访问被拦截", view.detail_text(
            view._result_for("https://blocked")))
        started = []
        page = SimpleNamespace(show_dialog=lambda snack: None, pop_dialog=lambda: None,
                               run_task=lambda target: started.append(target))
        with patch.object(CheckView, "page", new_callable=PropertyMock,
                          return_value=page):
            view._retry_failed(None)
        self.assertIsNone(view._retry_targets)
        self.assertEqual(started, [])

    async def test_export_writes_visible_rows_as_bom_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            view = self._view(data_dir=directory)
            view._store_result(self._result("a", "https://a", "unusable", 30))
            view._store_result(self._result("b", "https://b", "fully_usable", 5))
            view._state_filter.value = "fully_usable"
            await view._export_results(None)
            files = [name for name in os.listdir(directory) if name.endswith(".csv")]
            self.assertEqual(len(files), 1)
            with open(os.path.join(directory, files[0]),
                      encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0][0], "书源名称")
            self.assertEqual([row[0] for row in rows[1:]], ["b"])


if __name__ == "__main__":
    unittest.main()
