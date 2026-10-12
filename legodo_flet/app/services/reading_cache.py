"""Persistent catalogue and chapter cache for bookshelf reading."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import shutil
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.services.engine.rule_engine import ChapterItem

#: ``GET /media/{key}`` 的第二种 key 形状（设计 §4.4）：
#: ``<书目录摘要>-<章文件摘要>-<块序号>``。
#:
#: 三段全是 :class:`ReadingCache` **自己已经写在盘上的名字**——前两段就是
#: ``_book_dir()`` 的目录名与 ``content/*.json`` 的文件名，第三段是那份 JSON 里
#: ``blocks`` 的下标。所以这一格不是新造一套键空间，是把已有的键拼成一条能取回
#: 字节的地址；而正文插图（``_load_protected_images`` 换成字节的那些块）只有
#: 这一条路能跨进程渲染：字节按 §4.4 不过协议面，``url`` 那一格是远端地址，
#: 受保护图带着它去取恰好缺了服务端那份 cookie/UA。
#:
#: 分隔符用 ``-`` 不用 ``/``：``{key}`` 是一段的路径参数，而三段只可能是十六进制
#: 与十进制——``.`` 与 ``/`` 在语法层就进不来，目录穿越拼不出来。
MEDIA_KEY_RE = re.compile(r"^([0-9a-f]{64})-([0-9a-f]{64})-([0-9]{1,6})$")


def is_media_key(key: str) -> bool:
    """这条 key 是不是正文插图那一族（网关只问形状，认不认得路由归 ``MEDIA_KEY_RE``）。"""
    return MEDIA_KEY_RE.match(str(key or "")) is not None


class ReadingCache:
    #: 缓存格式版本：1 = 字节块存 JSON 内（base64）；2 = 字节块存兄弟文件（.bin），
    #: ``media_bytes`` 走 ``Path.read_bytes`` 不再 parse 整章 JSON。
    #:
    #: **迁移策略**：旧文件（VERSION 1）继续能读——``media_bytes`` 先找 .bin，
    #: 找不到再回退到 JSON 里那格 base64；新写的文件一律 VERSION 2 + .bin。
    #: 旧文件不会被自动升级（按需读的代价可接受，见第九十五轮实测账）。
    VERSION = 2
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
        if not isinstance(data, dict):
            return None
        # 目录文件不涉及媒体块形状，VERSION 1 和 2 都能读（G4-C 只改了内容缓存的字节存储）。
        version = data.get("version")
        if version not in (1, 2):
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

    def has_catalog(self, source_url: str, book_url: str) -> bool:
        """这本书的目录在不在盘上——**只 stat，不读不 parse**。

        普查"本地读过几本书"要过一整架书，读一次目录是 25 ms 量级，stat 是
        0.014 ms/本（`.probe/out_catalog_concurrency.txt`），差三个数量级。
        """
        return (self._book_dir(source_url, book_url) / "catalog.json").is_file()

    def _content_path(self, source_url: str, book_url: str,
                      chapter_url: str) -> Path:
        return (self._book_dir(source_url, book_url) / "content" /
                f"{self._digest(chapter_url)}.json")

    def load_content(self, source_url: str, book_url: str,
                     chapter_url: str) -> ChapterContent | None:
        data = self._read(self._content_path(source_url, book_url, chapter_url))
        if not isinstance(data, dict):
            return None
        # 兼容旧缓存：VERSION 1（字节在 JSON 里）和 VERSION 2（字节在 .bin）都能读。
        version = data.get("version")
        if version not in (1, 2):
            return None
        content_dir = self._book_dir(source_url, book_url) / "content"
        chapter_digest = self._digest(chapter_url)
        blocks = []
        try:
            for idx, item in enumerate(data.get("blocks", [])):
                value = item.get("value", "")
                if item.get("bytes"):
                    # VERSION 2：优先从 .bin 读；没有就回退到 JSON 里的 base64（兼容迁移期）
                    bin_path = content_dir / f"{chapter_digest}-{idx}.bin"
                    if bin_path.is_file():
                        try:
                            value = bin_path.read_bytes()
                        except OSError:
                            # .bin 坏了 → 回退到 JSON 里那格
                            value = base64.b64decode(value)
                    else:
                        value = base64.b64decode(value)
                blocks.append(ContentBlock(
                    str(item.get("kind") or "text"), value,
                    dict(item.get("headers") or {}), str(item.get("title") or ""),
                    # 旧缓存文件没有 url 这一格：取空串，等于这块本来就没被换成字节。
                    str(item.get("url") or "")))
            return ChapterContent(
                blocks=blocks, title=str(data.get("title") or ""),
                image_style=str(data.get("image_style") or ""),
                raw_content=str(data.get("raw_content") or ""))
        except (TypeError, ValueError, binascii.Error):
            return None

    def save_content(self, source_url: str, book_url: str, chapter_url: str,
                     document: ChapterContent) -> None:
        """存一章内容：字节块落盘成兄弟文件（.bin），JSON 里只留元数据。

        **为什么**：第九十五轮实测，一页 30 张图 → 章 JSON 786 KB、取一张中位 1.47 ms、
        整页 44.4 ms、底价（一次读完再解）1.43 ms——每张图片摊一次 JSON parse。
        文字书够用，漫画必须换形状：把字节抽到 .bin，``media_bytes`` 走 ``read_bytes``。
        """
        content_dir = self._book_dir(source_url, book_url) / "content"
        chapter_digest = self._digest(chapter_url)
        json_path = content_dir / f"{chapter_digest}.json"

        blocks = []
        for idx, block in enumerate(document.blocks):
            is_bytes = isinstance(block.value, bytes)
            value = (base64.b64encode(block.value).decode("ascii")
                     if is_bytes else str(block.value))
            entry = {
                "kind": block.kind, "value": value, "bytes": is_bytes,
                "headers": block.headers, "title": block.title,
                "url": str(getattr(block, "url", "") or ""),
            }
            # 字节块额外写一份 .bin 兄弟文件：media_bytes 直接 read_bytes，不碰 JSON。
            if is_bytes:
                bin_path = content_dir / f"{chapter_digest}-{idx}.bin"
                bin_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = bin_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
                temporary.write_bytes(block.value)
                temporary.replace(bin_path)
            blocks.append(entry)

        self._write(json_path, {
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

    # ------------------------------------------------------------------ #
    # /media 的键：正文里那些字节块的地址（设计 §4.4）
    # ------------------------------------------------------------------ #
    def media_key(self, source_url: str, book_url: str, chapter_url: str,
                  block_index: int) -> str:
        """这块字节在 ``/media`` 那一条路上的名字。

        两个摘要走的是 :meth:`_digest`，与 ``save_content`` 落盘用的那两个**是同一个
        调用**——键与磁盘名字对不上就是两份真相，所以这里不另算一遍。
        """
        return (f"{self._digest(source_url, book_url)}-{self._digest(chapter_url)}"
                f"-{int(block_index)}")

    def media_bytes(self, key: str) -> bytes | None:
        """按 ``/media`` 的 key 取回那块字节；取不到就是 None（不猜、不回空串）。

        **优先走 .bin 兄弟文件**（VERSION 2）：``Path.read_bytes``，不 parse JSON。
        旧缓存（VERSION 1）没有 .bin → 回退到 JSON 里那格 base64（兼容迁移期）。
        只认 ``bytes`` 为真的那一格：文本块本来就该过协议面，给它一条二进制路等于
        多一个能读正文的口子。缓存文件不在/版本不对/下标越界/base64 坏了，一律 None——
        网关那边据此回 404，而不是回一份"看着像"的载荷。
        """
        match = MEDIA_KEY_RE.match(str(key or ""))
        if match is None:
            return None
        book_digest, chapter_digest, index = match.groups()
        # 三段都已被正则钉死成十六进制/十进制：拼不出 ``..``，也拼不出分隔符。
        content_dir = self.root / book_digest / "content"
        json_path = content_dir / f"{chapter_digest}.json"
        position = int(index)

        # ① VERSION 2：先找 .bin 兄弟文件（快，不碰 JSON）
        bin_path = content_dir / f"{chapter_digest}-{position}.bin"
        if bin_path.is_file():
            try:
                return bin_path.read_bytes()
            except OSError:
                return None

        # ② 回退：旧缓存（VERSION 1）或 .bin 被删了 → 从 JSON 里解 base64
        data = self._read(json_path)
        if not isinstance(data, dict):
            return None
        # 兼容 VERSION 1 和 2：只要 blocks 在就试
        blocks = data.get("blocks")
        if not isinstance(blocks, list) or position >= len(blocks):
            return None
        item = blocks[position]
        if not isinstance(item, dict) or not item.get("bytes"):
            return None
        try:
            return base64.b64decode(str(item.get("value") or ""))
        except (binascii.Error, ValueError):
            return None

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
        """Keep the persistent reader cache bounded, evicting oldest content.

        同时扫 .json 与 .bin：.bin 是字节块的兄弟文件，只随对应的章 JSON 一起删。
        """
        try:
            json_files = [path for path in self.root.glob("*/content/*.json")
                          if path.is_file() and not (path.parent.parent / ".offline").exists()]
            bin_files = [path for path in self.root.glob("*/content/*.bin")
                         if path.is_file() and not (path.parent.parent / ".offline").exists()]
            sized = [(path.stat().st_mtime, path.stat().st_size, path)
                     for path in json_files + bin_files]
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
