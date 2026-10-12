# -*- coding: utf-8 -*-
"""FontService：阅读字体文件扫描 / 管理 / 选择（M5，契约 §8）。

- **系统字体枚举**：``scan_system_fonts()`` 按平台分支枚举系统字体目录（只读），
  读取/解析统一走 fontTools——Windows ``C:\\Windows\\Fonts`` /
  macOS ``~/Library/Fonts`` + ``/Library/Fonts`` /
  Linux ``~/.fonts`` + ``/usr/share/fonts``。路径枚举平台分支，解析不分支。
- **用户字体**：``add_font_file(path)`` 把字体文件拷进 app_paths 数据目录
  ``fonts/`` 并登记 ``fonts`` 表（id/name/family/path）；
  ``list_user_fonts()`` / ``remove_font(id)`` 管理登记行（同时删文件）。
- **当前选中字体**：SettingsService 键 ``reader.font_family``（阅读器排版偏好读的
  就是这同一个键，选完正文立刻用得上）。
- 字体族解析复用 query_ttf.py 的经验（name 表优先 family nameID=1），
  这里统一用 fontTools 读 name 表（query_ttf 是规则兼容层的手写解析，
  字体管理不需要其 cmap/glyph 细节，fontTools 更稳）。

硬约束：零 flet import；fontTools 懒加载；跨平台（目录枚举按平台分支）。
"""
from __future__ import annotations

import logging
import shutil
import sys
import threading
import time
from pathlib import Path

from app.core.lifecycle import Service
from app.core.storage import LockedConnection, StorageService

logger = logging.getLogger(__name__)

# 当前选中字体的 SettingsService 键。**必须**是 ``reader.font_family``：阅读器排版
# 偏好统一存在 ``reader.<键>`` 下（ReadingService.get_reader_preference），而它的
# 偏好名就叫 ``font_family``（ReaderView.DEFAULT_PREFERENCES）。这里另写一个键，
# 选完字体正文永远不变字（本轮修的就是这条）。
SELECTED_FAMILY_KEY = "reader.font_family"

# 可解析的字体扩展名。
_FONT_EXTS = {".ttf", ".otf", ".ttc", ".woff", ".woff2"}


class FontService(Service):
    """系统字体只读枚举 + 用户字体登记/选择。"""

    name = "FontService"
    dependencies: tuple[type[Service], ...] = (StorageService,)

    def __init__(self, storage: StorageService | None = None,
                 settings=None) -> None:
        super().__init__()
        self._storage = storage
        self._settings = settings
        self._lock = threading.Lock()

    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            self._storage = self.hub.require(StorageService)
        if self._settings is None and self.hub is not None:
            try:
                from app.core.settings import SettingsService
                self._settings = self.hub.require(SettingsService)
            except Exception:
                self._settings = None

    def _conn(self) -> LockedConnection | None:
        return self._storage.db if self._storage is not None else None

    # ------------------------------------------------------------------ #
    # 系统字体（只读枚举，不落库）
    # ------------------------------------------------------------------ #
    def _system_font_dirs(self) -> list[Path]:
        """按平台分支列出系统字体目录（契约 §8）。"""
        home = Path.home()
        plat = sys.platform
        if plat.startswith("win"):
            return [Path(r"C:\Windows\Fonts")]
        if plat == "darwin":
            return [home / "Library" / "Fonts", Path("/Library/Fonts")]
        # Linux 及其它
        import os
        xdg = Path(os.environ.get("XDG_DATA_HOME",
                                  home / ".local" / "share"))
        return [home / ".fonts", xdg / "fonts", Path("/usr/share/fonts")]

    async def scan_system_fonts(self) -> list[dict]:
        """枚举系统字体目录下所有字体文件，解析族名。

        返回 [{name, family, path}]；不可解析的文件跳过（不报错）。
        解析走 fontTools（懒加载）。
        """
        results: list[dict] = []
        seen: set[str] = set()
        for directory in self._system_font_dirs():
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if entry.suffix.lower() not in _FONT_EXTS:
                    continue
                try:
                    family = self._read_family(entry)
                except Exception:
                    continue  # 损坏/不支持的字体文件跳过
                if not family:
                    continue
                key = str(entry).lower()
                if key in seen:
                    continue
                seen.add(key)
                results.append({
                    "name": family, "family": family,
                    "path": str(entry), "source": "system",
                })
        return results

    # ------------------------------------------------------------------ #
    # 用户字体（拷进数据目录 fonts/ + 登记 fonts 表）
    # ------------------------------------------------------------------ #
    def _user_font_dir(self) -> Path:
        """用户字体落点：app_paths 数据目录 fonts/（契约 §8）。"""
        base = (self._storage.paths.data if self._storage is not None
                else None)
        if base is None:
            from app.services.app_paths import default_app_paths
            base = default_app_paths().data
        path = base / "fonts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def add_font_file(self, path: str) -> int:
        """把一个字体文件拷进数据目录 fonts/ 并登记，返回登记 id。

        同一文件路径已登记过则直接返回已有 id（UNIQUE(path) 约束）。
        """
        src = Path(path)
        if not src.is_file():
            raise FileNotFoundError(f"字体文件不存在: {path}")
        family = self._read_family(src) or src.stem
        dest_dir = self._user_font_dir()
        # 目标文件名加族名前缀避免重名覆盖。
        safe_name = f"{int(time.time())}_{src.name}"
        dest = dest_dir / safe_name
        shutil.copyfile(src, dest)

        conn = self._conn()
        rid = 0
        if conn is not None:
            with self._lock:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO fonts(name, family, path)"
                    " VALUES(?,?,?)",
                    (family, family, str(dest)))
                rid = cur.lastrowid
                if cur.rowcount == 0:
                    row = conn.execute(
                        "SELECT id FROM fonts WHERE path=?",
                        (str(dest),)).fetchone()
                    rid = row[0] if row is not None else 0
        return int(rid)

    async def list_user_fonts(self) -> list[dict]:
        conn = self._conn()
        if conn is None:
            return []
        with self._lock:
            rows = conn.execute(
                "SELECT id, name, family, path FROM fonts ORDER BY id ASC"
            ).fetchall()
        return [
            {"id": r[0], "name": r[1], "family": r[2],
             "path": r[3], "source": "user"}
            for r in rows
        ]

    async def remove_font(self, rid: int) -> bool:
        """删除登记行并删数据目录里的字体文件。"""
        conn = self._conn()
        if conn is None:
            return False
        with self._lock:
            row = conn.execute(
                "SELECT path FROM fonts WHERE id=?", (int(rid),)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM fonts WHERE id=?", (int(rid),))
        try:
            Path(row[0]).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("删除用户字体文件失败 %s: %s", row[0], exc)
        return True

    # ------------------------------------------------------------------ #
    # 当前选中字体（SettingsService 键 reader.font_family）
    # ------------------------------------------------------------------ #
    def get_selected_family(self) -> str:
        """当前选中字体族名；未设置返回空串（视图用系统默认）。"""
        if self._settings is not None:
            try:
                return str(self._settings.get(SELECTED_FAMILY_KEY, "") or "")
            except Exception:
                return ""
        return ""

    def set_selected_family(self, family: str) -> None:
        """记录当前选中字体族名（视图切字时调用）。"""
        if self._settings is not None:
            self._settings.set(SELECTED_FAMILY_KEY, str(family or ""))

    # ------------------------------------------------------------------ #
    # 内部：fontTools 读 family name（懒加载）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_family(path: Path) -> str:
        """用 fontTools 读字体族名（nameID=1）；失败返回空串。"""
        from fontTools.ttLib import TTFont  # 懒加载

        # ttc 取第 0 个子字体；lazy 打开加速枚举。
        font = TTFont(str(path), fontNumber=0, lazy=True)
        try:
            name_table = font["name"]
            # 优先 Windows Unicode 记录，兜底任意 family nameID=1。
            family = name_table.getDebugName(1) or ""
            return str(family).strip()
        finally:
            font.close()
