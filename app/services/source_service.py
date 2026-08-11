"""
书源服务层 - 业务逻辑：加载、保存、导入、过滤书源
"""
from __future__ import annotations
import json
import shutil
import time
from pathlib import Path
from typing import Callable

from app.models.book_source import BookSource
from app.services.app_paths import default_app_paths


class SourceService:
    """书源业务逻辑服务"""

    def __init__(self, resource_dir: str | Path, data_dir: str | Path | None = None,
                 cache_dir: str | Path | None = None):
        self.resource_dir = Path(resource_dir)
        defaults = default_app_paths()
        self.data_dir = Path(data_dir) if data_dir else defaults.data
        self.cache_dir = Path(cache_dir) if cache_dir else (
            Path(data_dir) / "cache" if data_dir is not None else defaults.cache)
        if data_dir is None:
            self._migrate_legacy_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_cache_dirs()
        self._sources: list[BookSource] = []
        self._current_path: str | None = None
        # 书源 URL -> 分类检测结果。仅作为本次运行缓存，不改写原始 exploreUrl。
        self.category_checks: dict[str, list[dict]] = {}
        self.bookshelf: list[dict] = []

    @staticmethod
    def _default_data_dir() -> Path:
        """返回与程序安装目录分离的持久化用户数据目录。"""
        return default_app_paths().data

    @property
    def sources_path(self) -> Path:
        return self.data_dir / "bookSource.json"

    def _migrate_legacy_cache_dirs(self) -> None:
        """Copy caches created by older versions under data_dir."""
        for name in ("reader-cache", "edge-webview-profile"):
            old = self.data_dir / name
            new = self.cache_dir / name
            if old.exists() and not new.exists():
                try:
                    shutil.copytree(old, new)
                except OSError:
                    pass

    def _migrate_legacy_data_dir(self) -> None:
        legacy = Path.home() / ".legado_booksource_check"
        if self.data_dir.exists() or not legacy.exists() or legacy == self.data_dir:
            return
        try:
            shutil.copytree(legacy, self.data_dir)
        except OSError:
            pass

    # ── 加载/保存 ──

    def load(self, path: str | Path | None = None) -> list[BookSource]:
        """从 JSON 文件加载书源，默认加载 bookSource.json"""
        if path is None:
            path = (self.sources_path if self.sources_path.exists() else
                    self.resource_dir / "bookSource.json")
        self._sources = BookSource.from_json(path)
        # Runtime mutations always target the writable user copy. Explicit
        # save_as remains available for exports to a user-selected path.
        self._current_path = str(self.sources_path)
        if Path(path) != self.sources_path and not self.sources_path.exists():
            self.save()
        self.load_bookshelf()
        return self._sources

    @property
    def bookshelf_path(self) -> Path:
        return self.data_dir / "bookshelf.json"

    def load_bookshelf(self) -> list[dict]:
        path = self.bookshelf_path
        legacy_path = self.resource_dir / "bookshelf.json"
        if not path.exists() and legacy_path.exists():
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy_path, path)
            except OSError:
                # 旧目录可能只读；此时读取旧文件，首次保存时写入新目录。
                path = legacy_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.bookshelf = data if isinstance(data, list) else []
            changed = False
            for item in self.bookshelf:
                changed = self._migrate_bookshelf_item(item) or changed
            if changed and path == self.bookshelf_path:
                self.save_bookshelf()
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.bookshelf = []
        return self.bookshelf

    @staticmethod
    def _migrate_bookshelf_item(item: dict) -> bool:
        """Add Legado-compatible book/progress fields without dropping old data."""
        changed = False
        aliases = {
            "bookUrl": item.get("url", ""),
            "tocUrl": item.get("toc_url", ""),
            "origin": item.get("source_url", ""),
            "originName": item.get("source_name", ""),
            "latestChapterTitle": item.get("last_chapter", ""),
            "totalChapterNum": 0,
            "lastCheckTime": 0,
            "lastCheckCount": 0,
            "canUpdate": True,
            "order": 0,
            "group": 0,
            "groupName": "",
            "latestChapterTime": 0,
            "cachedChapterCount": 0,
            "cacheTotalChapterCount": 0,
            "cacheErrorCount": 0,
        }
        reading = item.get("reading") or {}
        if not reading and (item.get("durChapterTitle") or item.get("durChapterTime")):
            reading = {
                "chapter_url": "",
                "chapter_name": item.get("durChapterTitle") or "",
                "chapter_index": int(item.get("durChapterIndex", 0) or 0),
                "chapter_pos": int(item.get("durChapterPos", 0) or 0),
                "scroll_offset": 0.0,
                "scroll_ratio": 0.0,
                "font_size": 16,
                "last_read_at": int(item.get("durChapterTime", 0) or 0) // 1000,
            }
            item["reading"] = reading
            changed = True
        aliases.update({
            "durChapterIndex": int(reading.get("chapter_index", 0) or 0),
            "durChapterPos": int(reading.get("chapter_pos", 0) or 0),
            "durChapterTime": int(reading.get("last_read_at", 0) or 0) * 1000,
            "durChapterTitle": reading.get("chapter_name") or None,
        })
        for key, value in aliases.items():
            if key not in item:
                item[key] = value
                changed = True
        return changed

    @staticmethod
    def reading_state(item: dict) -> dict | None:
        """Return one normalized progress object for old and Legado-style data."""
        reading = dict(item.get("reading") or {})
        if not reading and not (item.get("durChapterTitle") or item.get("durChapterTime")):
            return None
        reading.setdefault("chapter_url", "")
        reading.setdefault("chapter_name", item.get("durChapterTitle") or "")
        reading.setdefault("chapter_index", int(item.get("durChapterIndex", 0) or 0))
        reading.setdefault("chapter_pos", int(item.get("durChapterPos", 0) or 0))
        reading.setdefault("last_read_at", int(item.get("durChapterTime", 0) or 0) // 1000)
        reading.setdefault("scroll_offset", 0.0)
        reading.setdefault("scroll_ratio", 0.0)
        reading.setdefault("font_size", 16)
        return reading

    def save_bookshelf(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.bookshelf_path
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self.bookshelf, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)

    def add_to_bookshelf(self, source: BookSource, book) -> bool:
        key = (source.bookSourceUrl, book.url)
        if any((item.get("source_url"), item.get("url")) == key for item in self.bookshelf):
            return False
        self.bookshelf.append({
            "name": book.name, "author": book.author, "intro": book.intro,
            "url": book.url, "cover_url": book.cover_url,
            "toc_url": getattr(book, "toc_url", ""),
            "kind": getattr(book, "kind", ""),
            "last_chapter": getattr(book, "last_chapter", ""),
            "word_count": getattr(book, "word_count", ""),
            "download_urls": getattr(book, "download_urls", ""),
            "variables": book.variables or {}, "source_url": source.bookSourceUrl,
            "source_name": source.bookSourceName,
            # Legado Book entity aliases.  Keep the existing snake_case keys so
            # old bookshelf files and callers remain compatible.
            "bookUrl": book.url, "tocUrl": getattr(book, "toc_url", ""),
            "origin": source.bookSourceUrl, "originName": source.bookSourceName,
            "latestChapterTitle": getattr(book, "last_chapter", ""),
            "totalChapterNum": 0, "lastCheckTime": int(time.time() * 1000),
            "lastCheckCount": 0, "durChapterTitle": None,
            "durChapterIndex": 0, "durChapterPos": 0,
            "durChapterTime": 0, "canUpdate": True, "order": 0,
            "group": 0,
            "groupName": "", "latestChapterTime": 0,
            "cachedChapterCount": 0, "cacheTotalChapterCount": 0,
            "cacheErrorCount": 0,
        })
        self.save_bookshelf()
        return True

    def remove_from_bookshelf(self, source_url: str, book_url: str) -> bool:
        old_count = len(self.bookshelf)
        self.bookshelf = [item for item in self.bookshelf if
                          (item.get("source_url"), item.get("url")) !=
                          (source_url, book_url)]
        if len(self.bookshelf) != old_count:
            self.save_bookshelf()
            return True
        return False

    def switch_bookshelf_source(self, item: dict, source: BookSource, book) -> bool:
        """Replace one shelf entry's origin while retaining user-owned state."""
        if item not in self.bookshelf:
            return False
        new_key = (source.bookSourceUrl, book.url)
        if any(other is not item and
               (other.get("source_url"), other.get("url")) == new_key
               for other in self.bookshelf):
            return False
        previous = dict(item)
        try:
            identity = {
                "name": book.name, "author": book.author,
                "intro": book.intro, "url": book.url,
                "cover_url": book.cover_url,
                "toc_url": getattr(book, "toc_url", ""),
                "kind": getattr(book, "kind", ""),
                "last_chapter": getattr(book, "last_chapter", ""),
                "word_count": getattr(book, "word_count", ""),
                "download_urls": getattr(book, "download_urls", ""),
                "variables": dict(getattr(book, "variables", {}) or {}),
                "source_url": source.bookSourceUrl,
                "source_name": source.bookSourceName,
                "bookUrl": book.url,
                "tocUrl": getattr(book, "toc_url", ""),
                "origin": source.bookSourceUrl,
                "originName": source.bookSourceName,
                "latestChapterTitle": getattr(book, "last_chapter", ""),
                "lastCheckTime": int(time.time() * 1000),
                "lastCheckCount": 0,
                "latestChapterTime": 0,
                "totalChapterNum": 0,
                "updateError": "",
            }
            item.update(identity)
            # Chapter URLs belong to the old origin. Keep semantic progress so
            # the new catalog can resume by chapter index/title instead.
            if item.get("reading"):
                item["reading"] = dict(item["reading"])
                item["reading"]["chapter_url"] = ""
                item["reading"]["scroll_offset"] = 0.0
            self.save_bookshelf()
            return True
        except Exception:
            item.clear()
            item.update(previous)
            raise

    def update_bookshelf_check(self, source_url: str, book_url: str, *,
                               latest_title: str = "",
                               total_chapters: int | None = None,
                               error: str = "") -> bool:
        """Persist one Legado-style bookshelf refresh result."""
        for item in self.bookshelf:
            if (item.get("source_url"), item.get("url")) != (source_url, book_url):
                continue
            old_total = int(item.get("totalChapterNum", 0) or 0)
            new_total = old_total if total_chapters is None else max(
                0, int(total_chapters))
            item["lastCheckTime"] = int(time.time() * 1000)
            item["lastCheckCount"] = max(0, new_total - old_total)
            item["totalChapterNum"] = new_total
            if latest_title:
                item["latestChapterTitle"] = latest_title
                item["last_chapter"] = latest_title
                item["latestChapterTime"] = int(time.time() * 1000)
            item["updateError"] = str(error or "")
            self.save_bookshelf()
            return True
        return False

    def update_bookshelf_options(self, source_url: str, book_url: str, **options) -> bool:
        allowed = {"canUpdate", "group", "groupName", "order",
                   "cachedChapterCount", "cacheTotalChapterCount",
                   "cacheErrorCount", "cacheUpdatedAt"}
        for item in self.bookshelf:
            if (item.get("source_url"), item.get("url")) != (source_url, book_url):
                continue
            for key, value in options.items():
                if key in allowed:
                    item[key] = value
            self.save_bookshelf()
            return True
        return False

    def update_bookshelf_state(self, source_url: str, book_url: str,
                               variables: dict | None = None,
                               chapter=None, book=None,
                               scroll_offset: float | None = None,
                               scroll_ratio: float | None = None,
                               font_size: int | None = None,
                               chapter_pos: int | None = None,
                               total_chapters: int | None = None) -> bool:
        """Persist rule variables and the last reading position outside resources."""
        for item in self.bookshelf:
            if (item.get("source_url"), item.get("url")) != (source_url, book_url):
                continue
            if variables is not None:
                item["variables"] = dict(variables)
            if book is not None:
                for key, attribute in {
                    "name": "name", "author": "author", "intro": "intro",
                    "cover_url": "cover_url", "toc_url": "toc_url", "kind": "kind",
                    "last_chapter": "last_chapter", "word_count": "word_count",
                    "download_urls": "download_urls",
                }.items():
                    value = getattr(book, attribute, "")
                    if value:
                        item[key] = value
            if chapter is not None:
                previous = item.get("reading") or {}
                same_chapter = (previous.get("chapter_url") ==
                                getattr(chapter, "url", ""))
                now = int(time.time())
                resolved_pos = int(
                    chapter_pos if chapter_pos is not None else
                    previous.get("chapter_pos", item.get("durChapterPos", 0))
                    if same_chapter else 0)
                item["reading"] = {
                    "chapter_url": getattr(chapter, "url", ""),
                    "chapter_name": getattr(chapter, "name", ""),
                    "chapter_index": int(getattr(chapter, "index", 0) or 0),
                    "scroll_offset": float(
                        scroll_offset if scroll_offset is not None else
                        previous.get("scroll_offset", 0) if same_chapter else 0),
                    "scroll_ratio": float(
                        scroll_ratio if scroll_ratio is not None else
                        previous.get("scroll_ratio", 0) if same_chapter else 0),
                    "chapter_pos": resolved_pos,
                    "font_size": int(
                        font_size if font_size is not None else
                        previous.get("font_size", 16)),
                    "last_read_at": now,
                }
                # These are the canonical progress fields used by Legado.
                item["durChapterTitle"] = getattr(chapter, "name", "") or None
                item["durChapterIndex"] = int(getattr(chapter, "index", 0) or 0)
                item["durChapterPos"] = resolved_pos
                item["durChapterTime"] = now * 1000
            if total_chapters is not None:
                item["totalChapterNum"] = max(0, int(total_chapters))
            self.save_bookshelf()
            return True
        return False

    def load_passed(self) -> list[BookSource]:
        """加载已通过测试的书源"""
        path = self.resource_dir / "bookSource_passed.json"
        if path.exists():
            return self.load(path)
        return []

    def save(self, path: str | Path | None = None) -> bool:
        """保存书源列表到 JSON 文件"""
        if path is None:
            path = self.sources_path
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = [s.to_dict() for s in self._sources]
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
            if path == self.sources_path:
                self._current_path = str(path)
            return True
        except Exception as e:
            raise RuntimeError(f"保存失败: {e}")

    def save_as(self, path: str | Path) -> bool:
        """另存为"""
        return self.save(path)

    @property
    def sources(self) -> list[BookSource]:
        return self._sources

    @sources.setter
    def sources(self, val: list[BookSource]):
        self._sources = val

    @property
    def current_path(self) -> str | None:
        return self._current_path

    # ── 导入 ──

    def import_sources(self, file_path: str | Path, mode: str = "append") -> dict:
        """
        从文件导入书源

        Args:
            file_path: JSON 文件路径
            mode: "append" 追加 / "replace" 替换

        Returns:
            {"total": int, "imported": int, "duplicates": int, "errors": int}
        """
        result = {"total": 0, "imported": 0, "duplicates": 0, "errors": 0}

        try:
            new_sources = BookSource.from_json(file_path)
        except Exception as e:
            result["errors"] = 1
            raise RuntimeError(f"读取文件失败: {e}")

        result["total"] = len(new_sources)

        if mode == "replace":
            self._sources = new_sources
            result["imported"] = len(new_sources)
            return result

        # append 模式：按 bookSourceUrl 去重
        existing_urls = {s.bookSourceUrl for s in self._sources}
        for s in new_sources:
            if s.bookSourceUrl in existing_urls:
                result["duplicates"] += 1
            else:
                self._sources.append(s)
                result["imported"] += 1
                existing_urls.add(s.bookSourceUrl)

        return result

    # ── 查询/过滤 ──

    def filter(
        self,
        keyword: str = "",
        group: str = "",
        enabled_only: bool = False,
    ) -> list[BookSource]:
        """过滤书源列表"""
        result = self._sources

        if keyword:
            kw = keyword.lower()
            result = [
                s for s in result
                if kw in s.bookSourceName.lower()
                or kw in s.bookSourceUrl.lower()
                or kw in (s.bookSourceGroup or "").lower()
                or kw in (s.bookSourceComment or "").lower()
            ]

        if group:
            result = [s for s in result if s.bookSourceGroup == group]

        if enabled_only:
            result = [s for s in result if s.enabled]

        return result

    def get_groups(self) -> list[str]:
        """获取所有分组名称（去重排序）"""
        groups = {s.bookSourceGroup or "未分组" for s in self._sources}
        return sorted(groups, key=lambda x: (x == "未分组", x))

    def get_statistics(self) -> dict:
        """获取统计信息"""
        total = len(self._sources)
        enabled = sum(1 for s in self._sources if s.enabled)
        disabled = total - enabled
        groups = len(self.get_groups())
        return {
            "total": total,
            "enabled": enabled,
            "disabled": disabled,
            "groups": groups,
        }

    # ── 批量操作 ──

    def batch_enable(self, sources: list[BookSource]) -> int:
        """批量启用书源，返回修改数量"""
        count = 0
        source_set = {id(s) for s in sources}
        for s in self._sources:
            if id(s) in source_set and not s.enabled:
                s.enabled = True
                count += 1
        return count

    def batch_disable(self, sources: list[BookSource]) -> int:
        """批量禁用"""
        count = 0
        source_set = {id(s) for s in sources}
        for s in self._sources:
            if id(s) in source_set and s.enabled:
                s.enabled = False
                count += 1
        return count

    def batch_delete(self, sources: list[BookSource]) -> int:
        """批量删除，返回删除数量"""
        count = len(sources)
        source_set = {id(s) for s in sources}
        self._sources = [s for s in self._sources if id(s) not in source_set]
        return count
