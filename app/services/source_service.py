"""
书源服务层 - 业务逻辑：加载、保存、导入、过滤书源
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Callable

from app.models.book_source import BookSource


class SourceService:
    """书源业务逻辑服务"""

    def __init__(self, resource_dir: str | Path):
        self.resource_dir = Path(resource_dir)
        self._sources: list[BookSource] = []
        self._current_path: str | None = None
        # 书源 URL -> 分类检测结果。仅作为本次运行缓存，不改写原始 exploreUrl。
        self.category_checks: dict[str, list[dict]] = {}

    # ── 加载/保存 ──

    def load(self, path: str | Path | None = None) -> list[BookSource]:
        """从 JSON 文件加载书源，默认加载 bookSource.json"""
        if path is None:
            path = self.resource_dir / "bookSource.json"
        self._current_path = str(path)
        self._sources = BookSource.from_json(path)
        return self._sources

    def load_passed(self) -> list[BookSource]:
        """加载已通过测试的书源"""
        path = self.resource_dir / "bookSource_passed.json"
        if path.exists():
            return self.load(path)
        return []

    def save(self, path: str | Path | None = None) -> bool:
        """保存书源列表到 JSON 文件"""
        if path is None:
            if self._current_path:
                path = self._current_path
            else:
                path = self.resource_dir / "bookSource.json"
        try:
            data = [s.to_dict() for s in self._sources]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
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
