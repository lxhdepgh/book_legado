import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.models.book_source import BookSource
from app.services import check_service
from app.views.manage_view import ManageView


class UiSafetyTests(unittest.TestCase):
    def test_single_source_toggle_rolls_back_when_save_fails(self):
        source = BookSource(bookSourceName="A", bookSourceUrl="https://a")
        service = SimpleNamespace(sources=[source], save=lambda: None)
        view = ManageView(service)
        view._render_page = lambda: None

        with patch.object(service, "save", side_effect=RuntimeError("disk full")):
            view._toggle_enabled(source, False)

        self.assertTrue(source.enabled)


class CheckSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_does_not_disable_by_default(self):
        source = SimpleNamespace(
            bookSourceName="bad", bookSourceUrl="https://bad",
            searchUrl="", ruleSearch={}, ruleExplore={}, header="",
            enabled=True, enabledExplore=False, exploreUrl="",
            to_dict=lambda: {"bookSourceName": "bad",
                             "bookSourceUrl": "https://bad"})

        async def unusable(*args, **kwargs):
            return {"idx": 1, "name": "bad", "raw_url": "https://bad",
                    "status": "err", "search_status": "error",
                    "error": "offline"}

        with patch.object(check_service, "test_one", unusable):
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

        async def unusable(*args, **kwargs):
            return {"idx": 1, "name": "bad", "raw_url": "https://bad",
                    "status": "err", "search_status": "error",
                    "error": "offline"}

        with patch.object(check_service, "test_one", unusable):
            result = await check_service.check_sources(
                [source], timeout=0.1, auto_disable=True)

        self.assertTrue(result[0]["auto_disabled"])
        self.assertFalse(source.enabled)


if __name__ == "__main__":
    unittest.main()
