"""RulePreExecutor：在桌面端预执行 JS 规则，生成静态 HTML 模板（Android 兼容方案）。

核心思路：借鉴 Legado 的 BackstageWebView(html=..., javaScript=...) 模式，
但把"渲染时机"从运行时提前到桌面端预执行阶段。

工作流程：
1. 桌面端：用 HeadlessBrowser 渲染需要 JavaScript 的书源章节
2. 提取正文并缓存到 storage
3. Android 端：优先从缓存读取静态模板，无需 WebView

设计依据：
- reference_code/legado-lyc486/app/src/main/java/io/legado/app/help/http/BackstageWebView.kt
- reference_code/legado-lyc486/app/src/main/java/io/legado/app/help/webView/WebViewPool.kt
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.lifecycle import Service

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PreRenderedChapter:
    """预渲染的章节缓存项。"""
    
    chapter_url: str
    content_html: str  # 渲染后的 HTML
    title: str = ""
    rendered_at: float = 0.0  # Unix 时间戳
    source_url: str = ""  # 书源 URL（用于失效检测）
    js_executed: bool = False  # 是否执行过 JS


class RulePreExecutor(Service):
    """在桌面端预执行 JS 规则，生成静态 HTML 模板。
    
    使用方式：
        executor = RulePreExecutor(browser_service, storage)
        await executor.pre_execute_chapter(chapter_url, source, rules)
        
        # Android 端读取
        template = await executor.get_cached_template(chapter_url)
    """
    
    name = "RulePreExecutor"
    dependencies: tuple[type[Service], ...] = ()
    
    def __init__(self, browser_service=None, storage=None, engine=None):
        """
        Args:
            browser_service: BrowserService 实例（用于无头渲染）
            storage: StorageService 实例（用于缓存）
            engine: RuleEngineService 或 LegadoReadingEngine（用于规则执行）
        """
        super().__init__()
        self._browser_service = browser_service
        self._storage = storage
        self._engine = engine
        self._cache_key_prefix = "pre_render:"
    
    async def on_start(self) -> None:
        """从 hub 获取未注入的依赖。"""
        if self._storage is None and self.hub is not None:
            from app.core.storage import StorageService
            self._storage = self.hub.require(StorageService)
        
        logger.info("RulePreExecutor 已启动")
    
    async def on_stop(self) -> None:
        logger.info("RulePreExecutor 已停止")
    
    async def pre_execute_chapter(
        self,
        chapter_url: str,
        source: Any = None,
        rules: dict | None = None,
        force_refresh: bool = False
    ) -> PreRenderedChapter | None:
        """预执行单个章节的 JS 规则，生成静态 HTML 模板。
        
        Args:
            chapter_url: 章节 URL
            source: 书源对象（需有 bookSourceUrl 属性）
            rules: 书源规则字典（可选，若提供则先执行规则获取原始 HTML）
            force_refresh: 强制刷新缓存
            
        Returns:
            PreRenderedChapter 或 None（失败时）
        """
        import time
        
        # 检查缓存
        if not force_refresh:
            cached = await self._get_from_cache(chapter_url)
            if cached is not None:
                logger.debug("命中预渲染缓存：%s", chapter_url)
                return cached
        
        # 检查是否有浏览器内核
        if self._browser_service is None:
            logger.warning("BrowserService 未注入，无法预渲染：%s", chapter_url)
            return None
        
        if not self._browser_service.available_fetcher():
            logger.info("浏览器内核不可用，跳过预渲染：%s", chapter_url)
            return None
        
        try:
            # Step 1: 用 HeadlessBrowser 渲染
            rendered = await self._render_with_browser(chapter_url, source, rules)
            if rendered is None:
                logger.warning("渲染失败：%s", chapter_url)
                return None
            
            # Step 2: 提取正文（浏览器返回的 body）
            content_html = rendered.get("body", "")
            if not content_html:
                logger.warning("渲染结果为空：%s", chapter_url)
                return None
            
            # Step 3: 构建缓存项
            chapter = PreRenderedChapter(
                chapter_url=chapter_url,
                content_html=content_html,
                title=rendered.get("title", ""),
                rendered_at=time.time(),
                source_url=getattr(source, "bookSourceUrl", "") if source else "",
                js_executed=True
            )
            
            # Step 4: 写入缓存
            await self._save_to_cache(chapter)
            
            logger.info("预渲染完成：%s (%d 字符)", chapter_url, len(content_html))
            return chapter
            
        except Exception as exc:
            logger.exception("预渲染异常：%s - %s", chapter_url, exc)
            return None
    
    async def pre_execute_book(
        self,
        chapters: list[dict],
        source: Any = None,
        batch_size: int = 5,
        delay_between: float = 0.5
    ) -> dict[str, PreRenderedChapter]:
        """批量预执行一本书的所有章节。
        
        Args:
            chapters: 章节列表，每项为 {"url": ..., "title": ...}
            source: 书源对象
            batch_size: 并发批次大小（避免同时打开太多 Chromium 标签页）
            delay_between: 批次间延迟（秒）
            
        Returns:
            {chapter_url: PreRenderedChapter} 映射
        """
        results = {}
        total = len(chapters)
        
        logger.info("开始批量预渲染：%d 个章节，每批 %d 个", total, batch_size)
        
        for i in range(0, total, batch_size):
            batch = chapters[i:i + batch_size]
            tasks = [
                self.pre_execute_chapter(
                    ch["url"],
                    source=source,
                    rules=None  # 批量模式不传规则，直接用 URL 渲染
                )
                for ch in batch
            ]
            
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for ch, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.warning("章节预渲染失败：%s - %s", ch["url"], result)
                elif result is not None:
                    results[ch["url"]] = result
            
            # 批次间延迟，避免压垮目标站点
            if i + batch_size < total:
                await asyncio.sleep(delay_between)
            
            progress = min(i + batch_size, total)
            logger.info("预渲染进度：%d/%d (%.1f%%)", 
                       progress, total, progress / total * 100)
        
        success_count = len(results)
        logger.info("批量预渲染完成：成功 %d/%d", success_count, total)
        return results
    
    async def get_cached_template(self, chapter_url: str) -> str | None:
        """Android 端调用：获取预渲染的静态 HTML 模板。
        
        Args:
            chapter_url: 章节 URL
            
        Returns:
            HTML 字符串或 None（未缓存时）
        """
        cached = await self._get_from_cache(chapter_url)
        if cached is not None:
            return cached.content_html
        return None
    
    async def invalidate_cache(self, chapter_url: str | None = None, 
                               source_url: str | None = None) -> int:
        """使缓存失效。
        
        Args:
            chapter_url: 指定章节 URL（精确失效）
            source_url: 书源 URL（批量失效该书源所有章节）
            
        Returns:
            失效的缓存项数量
        """
        count = 0
        
        if chapter_url:
            key = f"{self._cache_key_prefix}{chapter_url}"
            if self._storage:
                await self._storage.delete(key)
                count = 1
            logger.info("缓存已失效：%s", chapter_url)
        
        elif source_url and self._storage:
            # 扫描所有该书的缓存键
            prefix = f"{self._cache_key_prefix}{source_url}:"
            # 注意：StorageService 可能不支持前缀扫描，这里简化处理
            logger.warning("按书源批量失效暂未实现（需 StorageService 支持前缀扫描）")
        
        return count
    
    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #
    
    async def _render_with_browser(
        self,
        url: str,
        source: Any = None,
        rules: dict | None = None
    ) -> dict | None:
        """用 HeadlessBrowser 渲染 URL。
        
        如果提供了 rules，先执行规则获取原始 URL，再渲染。
        """
        try:
            # 如果有规则，先执行规则获取最终 URL
            final_url = url
            if rules and self._engine:
                # TODO: 执行规则解析逻辑
                # final_url = await self._engine.resolve_url(url, rules)
                pass
            
            # 用 BrowserService 渲染
            options = {
                "webView": True,  # 启用 JavaScript
                "timeout": 30000,  # 30 秒超时
            }
            
            if source:
                # 传递 UA 和 headers
                ua = getattr(source, "headerMap", {}).get("User-Agent")
                if ua:
                    options["userAgent"] = ua
            
            rendered = await self._browser_service.fetch(final_url, options)
            return rendered
            
        except Exception as exc:
            logger.error("浏览器渲染失败：%s - %s", url, exc)
            return None
    
    @staticmethod
    def _digest(value: str) -> str:
        """生成 SHA256 hash 作为安全的文件名。"""
        import hashlib
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    
    @staticmethod
    def _read_json(path):
        """读取 JSON 文件，失败返回 None。"""
        import json
        from pathlib import Path
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
    
    @staticmethod
    def _write_json(path, data) -> None:
        """原子写入 JSON 文件（临时文件 + replace）。"""
        import json
        import uuid
        from pathlib import Path
        
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8"
        )
        temporary.replace(path)
    
    async def _get_from_cache(self, chapter_url: str) -> PreRenderedChapter | None:
        """从缓存读取预渲染结果（遵循 ReadingCache 模式）。"""
        if not self._storage:
            return None
        
        try:
            # 构建缓存文件路径（类似 ReadingCache._book_dir）
            cache_dir = self._storage.paths.cache / "pre_render"
            filename = self._digest(chapter_url) + ".json"
            cache_file = cache_dir / filename
            
            obj = self._read_json(cache_file)
            if obj is None:
                return None
            
            return PreRenderedChapter(**obj)
            
        except Exception as exc:
            logger.warning("缓存读取失败：%s - %s", chapter_url, exc)
            return None
    
    async def _save_to_cache(self, chapter: PreRenderedChapter) -> None:
        """保存预渲染结果到缓存（遵循 ReadingCache 原子写入模式）。"""
        if not self._storage:
            return
        
        try:
            # 构建缓存文件路径
            cache_dir = self._storage.paths.cache / "pre_render"
            filename = self._digest(chapter.chapter_url) + ".json"
            cache_file = cache_dir / filename
            
            # 序列化为 JSON
            data = {
                "chapter_url": chapter.chapter_url,
                "content_html": chapter.content_html,
                "title": chapter.title,
                "rendered_at": chapter.rendered_at,
                "source_url": chapter.source_url,
                "js_executed": chapter.js_executed,
            }
            
            self._write_json(cache_file, data)
            
        except Exception as exc:
            logger.error("缓存保存失败：%s - %s", chapter.chapter_url, exc)
