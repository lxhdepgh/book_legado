"""Persistent catalogue and chapter cache for bookshelf reading."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import shutil
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.services.engine.rule_engine import ChapterItem


class ReadingCache:
    VERSION = 1
    MAX_DISK_BYTES = 256 * 1024 * 1024

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir) / "reader-cache"

    @staticmethod
    def _digest(*values: str) -> str:
        raw = "\0".join(str(value or "") for value in values)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _book_dir(self, source_url: str, book_url: str) -> Path:
        return self.root / self._digest(source_url, book_url)

    @staticmethod
    def _read(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write(path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")
        temporary.replace(path)

    def load_catalog(self, source_url: str, book_url: str) -> list[ChapterItem] | None:
        data = self._read(self._book_dir(source_url, book_url) / "catalog.json")
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            return None
        try:
            return [ChapterItem(**item) for item in data.get("chapters", [])]
        except (TypeError, ValueError):
            return None

    def save_catalog(self, source_url: str, book_url: str,
                     chapters: list[ChapterItem]) -> None:
        self._write(self._book_dir(source_url, book_url) / "catalog.json", {
            "version": self.VERSION,
            "saved_at": int(time.time()),
            "chapters": [asdict(chapter) for chapter in chapters],
        })

    def _content_path(self, source_url: str, book_url: str,
                      chapter_url: str) -> Path:
        return (self._book_dir(source_url, book_url) / "content" /
                f"{self._digest(chapter_url)}.json")

    def load_content(self, source_url: str, book_url: str,
                     chapter_url: str) -> ChapterContent | None:
        data = self._read(self._content_path(source_url, book_url, chapter_url))
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            return None
        blocks = []
        try:
            for item in data.get("blocks", []):
                value = item.get("value", "")
                if item.get("bytes"):
                    value = base64.b64decode(value)
                blocks.append(ContentBlock(
                    str(item.get("kind") or "text"), value,
                    dict(item.get("headers") or {}), str(item.get("title") or "")))
            return ChapterContent(
                blocks=blocks, title=str(data.get("title") or ""),
                image_style=str(data.get("image_style") or ""),
                raw_content=str(data.get("raw_content") or ""))
        except (TypeError, ValueError, binascii.Error):
            return None

    def save_content(self, source_url: str, book_url: str, chapter_url: str,
                     document: ChapterContent) -> None:
        blocks = []
        for block in document.blocks:
            is_bytes = isinstance(block.value, bytes)
            value = (base64.b64encode(block.value).decode("ascii")
                     if is_bytes else str(block.value))
            blocks.append({
                "kind": block.kind, "value": value, "bytes": is_bytes,
                "headers": block.headers, "title": block.title,
            })
        self._write(self._content_path(source_url, book_url, chapter_url), {
            "version": self.VERSION,
            "saved_at": int(time.time()),
            "title": document.title,
            "image_style": document.image_style,
            "raw_content": document.raw_content,
            "blocks": blocks,
        })
        self._prune()

    def has_content(self, source_url: str, book_url: str, chapter_url: str) -> bool:
        return self._content_path(source_url, book_url, chapter_url).is_file()

    def cache_counts(self, source_url: str, book_url: str) -> tuple[int, int]:
        chapters = self.load_catalog(source_url, book_url) or []
        readable = [item for item in chapters if not item.is_volume and item.url]
        cached = sum(self.has_content(source_url, book_url, item.url)
                     for item in readable)
        return cached, len(readable)

    def pin_book(self, source_url: str, book_url: str) -> None:
        """Protect a user-requested offline book from automatic cache pruning."""
        marker = self._book_dir(source_url, book_url) / ".offline"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)

    def _prune(self) -> None:
        """Keep the persistent reader cache bounded, evicting oldest content."""
        try:
            files = [path for path in self.root.glob("*/content/*.json")
                     if path.is_file() and not (path.parent.parent / ".offline").exists()]
            sized = [(path.stat().st_mtime, path.stat().st_size, path)
                     for path in files]
        except OSError:
            return
        total = sum(size for _, size, _ in sized)
        for _, size, path in sorted(sized):
            if total <= self.MAX_DISK_BYTES:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                continue

    def remove_book(self, source_url: str, book_url: str) -> None:
        directory = self._book_dir(source_url, book_url)
        if directory.parent == self.root and directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
