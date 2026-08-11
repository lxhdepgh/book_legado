import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.models.book_source import BookSource
from app.services import check_service
from app.views.manage_view import ManageView
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


if __name__ == "__main__":
    unittest.main()
