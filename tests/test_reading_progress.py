"""ReadingPosition/ReadingSession 连续滚动定位模型单元测试（M7 改指新位置）。

M7 删除了冻结副本 ``app/services/reading_progress.py``；该定位模型已"零逻辑改动"
迁入 :mod:`app.services.reading.reading_service`（ReadingPosition/ReadingSession），
本测试随 import 改指新位置，断言保持不变。
"""
import unittest
from types import SimpleNamespace

from app.services.reading.reading_service import (
    ReadingPosition, ReadingSession)


class ReadingProgressTests(unittest.TestCase):
    def test_legacy_reading_fields_migrate_to_canonical_position(self):
        position = ReadingPosition.from_reading("source", "book", {
            "chapter_url": "chapter/10", "chapter_name": "ten",
            "chapter_index": 9, "scroll_offset": 321.5,
            "scroll_ratio": 0.25, "chapter_pos": 50,
            "font_size": 20, "last_read_at": 100,
        })

        self.assertEqual(position.chapter_url, "chapter/10")
        self.assertEqual(position.chapter_offset, 321.5)
        self.assertEqual(position.character_offset, 50)
        self.assertEqual(position.as_reading()["scroll_ratio"], 0.25)

    def test_session_maps_document_scroll_to_chapter_local_progress(self):
        first = SimpleNamespace(url="chapter/1", name="one", index=0)
        second = SimpleNamespace(url="chapter/2", name="two", index=1)
        session = ReadingSession()
        session.bind("source", "book", [first, second])
        session.mount(first, 0, 100)
        session.mount(second, 1000, 200)

        position, visible = session.track(1500, 2000, 18)

        self.assertIs(visible, second)
        self.assertEqual(position.chapter_offset, 500)
        self.assertEqual(position.chapter_ratio, 0.5)
        self.assertEqual(position.character_offset, 100)

    def test_new_single_chapter_document_restores_local_offset(self):
        chapter = SimpleNamespace(url="chapter/100", name="hundred", index=99)
        session = ReadingSession()
        session.bind("source", "book", [chapter])
        session.mount(chapter, 0, 400)
        session.enter(chapter, 20, offset=640, ratio=0.4,
                      character_offset=160)

        self.assertEqual(session.position.chapter_url, chapter.url)
        self.assertEqual(session.position.chapter_offset, 640)
        self.assertEqual(session.position.character_offset, 160)


if __name__ == "__main__":
    unittest.main()
