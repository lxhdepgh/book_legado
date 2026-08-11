"""Reader-owned progress state, isolated from source rule evaluation."""
from __future__ import annotations

from dataclasses import dataclass
import time


@dataclass(frozen=True)
class ReadingPosition:
    source_url: str
    book_url: str
    chapter_url: str = ""
    chapter_name: str = ""
    chapter_index: int = 0
    chapter_offset: float = 0.0
    chapter_ratio: float = 0.0
    character_offset: int = 0
    font_size: int = 16
    updated_at: int = 0

    @classmethod
    def from_reading(cls, source_url, book_url, reading):
        value = reading or {}
        return cls(
            source_url=str(source_url or ""), book_url=str(book_url or ""),
            chapter_url=str(value.get("chapter_url") or ""),
            chapter_name=str(value.get("chapter_name") or ""),
            chapter_index=int(value.get("chapter_index", 0) or 0),
            chapter_offset=max(0.0, float(value.get("scroll_offset", 0) or 0)),
            chapter_ratio=max(0.0, min(
                1.0, float(value.get("scroll_ratio", 0) or 0))),
            character_offset=max(0, int(value.get("chapter_pos", 0) or 0)),
            font_size=max(1, int(value.get("font_size", 16) or 16)),
            updated_at=int(value.get("last_read_at", 0) or 0),
        )

    def as_reading(self):
        return {
            "chapter_url": self.chapter_url,
            "chapter_name": self.chapter_name,
            "chapter_index": self.chapter_index,
            "scroll_offset": self.chapter_offset,
            "scroll_ratio": self.chapter_ratio,
            "chapter_pos": self.character_offset,
            "font_size": self.font_size,
            "last_read_at": self.updated_at,
        }


@dataclass
class ChapterViewport:
    chapter: object
    document_start: float
    text_length: int


class ReadingSession:
    """Single authority for visible chapter and chapter-local coordinates."""

    def __init__(self):
        self.reset()

    def reset(self, source_url="", book_url="", chapters=None, position=None):
        self.source_url = str(source_url or "")
        self.book_url = str(book_url or "")
        self.chapters = list(chapters or [])
        self.viewports: dict[str, ChapterViewport] = {}
        self.document_extent = 0.0
        self.position = position or ReadingPosition(
            self.source_url, self.book_url)

    def bind(self, source_url, book_url, chapters, reading=None):
        position = (reading if isinstance(reading, ReadingPosition) else
                    ReadingPosition.from_reading(source_url, book_url, reading))
        self.reset(source_url, book_url, chapters, position)

    def mount(self, chapter, document_start, text_length):
        self.viewports[str(chapter.url)] = ChapterViewport(
            chapter=chapter,
            document_start=max(0.0, float(document_start or 0)),
            text_length=max(0, int(text_length or 0)))

    def track(self, document_offset, document_extent, font_size):
        self.document_extent = max(0.0, float(document_extent or 0))
        offset = max(0.0, float(document_offset or 0))
        ordered = sorted(
            self.viewports.values(), key=lambda item: item.document_start)
        if not ordered:
            return self.position, None
        selected = ordered[0]
        selected_index = 0
        for index, viewport in enumerate(ordered):
            if viewport.document_start > offset:
                break
            selected, selected_index = viewport, index
        end = (ordered[selected_index + 1].document_start
               if selected_index + 1 < len(ordered)
               else self.document_extent)
        extent = max(0.0, end - selected.document_start)
        local_offset = max(0.0, offset - selected.document_start)
        ratio = min(1.0, local_offset / extent) if extent else 0.0
        chapter = selected.chapter
        self.position = ReadingPosition(
            source_url=self.source_url, book_url=self.book_url,
            chapter_url=str(chapter.url or ""),
            chapter_name=str(chapter.name or ""),
            chapter_index=int(chapter.index or 0),
            chapter_offset=local_offset, chapter_ratio=ratio,
            character_offset=int(selected.text_length * ratio),
            font_size=int(font_size), updated_at=int(time.time()))
        return self.position, chapter

    def enter(self, chapter, font_size, offset=0.0, ratio=0.0,
              character_offset=0):
        self.position = ReadingPosition(
            source_url=self.source_url, book_url=self.book_url,
            chapter_url=str(chapter.url or ""),
            chapter_name=str(chapter.name or ""),
            chapter_index=int(chapter.index or 0),
            chapter_offset=max(0.0, float(offset or 0)),
            chapter_ratio=max(0.0, min(1.0, float(ratio or 0))),
            character_offset=max(0, int(character_offset or 0)),
            font_size=int(font_size), updated_at=int(time.time()))
        return self.position


class ReadingProgressStore:
    """Bookshelf adapter; rule engine objects never depend on this service."""

    def __init__(self, service):
        self.service = service

    def load(self, source_url, book_url):
        item = next((value for value in getattr(self.service, "bookshelf", [])
                     if (value.get("source_url"), value.get("url")) ==
                     (source_url, book_url)), None)
        if item is None:
            return None
        normalizer = getattr(self.service, "reading_state", None)
        reading = normalizer(item) if callable(normalizer) else item.get("reading")
        return (ReadingPosition.from_reading(source_url, book_url, reading)
                if reading else None)

    def save(self, position, chapter, total_chapters):
        return self.service.update_bookshelf_state(
            position.source_url, position.book_url, chapter=chapter,
            scroll_offset=position.chapter_offset,
            scroll_ratio=position.chapter_ratio,
            font_size=position.font_size,
            chapter_pos=position.character_offset,
            total_chapters=total_chapters)
