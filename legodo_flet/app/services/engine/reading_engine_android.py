"""ReadingEngineAndroid：Android 专用阅读引擎（无 WebView）。

核心思路：借鉴 Legado 的 BackstageWebView 决策逻辑，但 Android 端永远不走 WebView 路径，
而是优先从预渲染缓存读取静态 HTML 模板。

设计依据：
- reference_code/legado-lyc486/app/src/main/java/io/legado/app/model/analyzeRule/AnalyzeUrl.kt:445-476
  （if (this.useWebView && useWebView) { ... } else { ... }）

工作流程：
1. 加载章节时，先查预渲染缓存
2. 缓存命中 → 直接解析静态 HTML
3. 缓存未命中 → 尝试纯 HTTP 请求
4. 如果返回拦截页 → 提示用户在桌面端打开
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.engine.reading_engine import LegadoReadingEngine, ChapterContent, ContentBlock

logger = logging.getLogger(__name__)


class ReadingEngineAndroid(LegadoReadingEngine):
    """Android 专用阅读引擎（无 WebView，缓存优先）。
    
    与父类 LegadoReadingEngine 的差异：
    - webview_fetcher 永远为 None（Android 不支持 Playwright CDP）
    - load_chapter() 优先从预渲染缓存读取
    - 遇到拦截页时给出友好错误提示
    
    使用方式：
        engine = ReadingEngineAndroid(
            pre_executor=pre_executor,  # RulePreExecutor 实例
            storage=storage,
            ...
        )
        content = await engine.load_chapter(chapter_url, source)
    """
    
    def __init__(self, pre_executor=None, **kwargs):
        """
        Args:
            pre_executor: RulePreExecutor 实例（用于读取预渲染缓存）
            **kwargs: 传递给父类的参数（timeout, state_dir 等）
        """
        # 强制禁用 WebView fetcher（Android 不支持）
        kwargs["webview_fetcher"] = None
        super().__init__(**kwargs)
        
        self._pre_executor = pre_executor
        self._cache_hits = 0
        self._cache_misses = 0
    
    async def get_content(self, source, chapter, **kwargs) -> ChapterContent:
        """加载章节内容（Android 优化版：缓存优先）。
        
        决策流程：
        1. 查预渲染缓存 → 命中则直接解析
        2. 缓存未命中 → 纯 HTTP 请求
        3. 检测到拦截页 → 抛出友好错误
        
        Args:
            source: 书源对象
            chapter: 章节对象（需有 url 属性）
            **kwargs: 其他参数
            
        Returns:
            ChapterContent
            
        Raises:
            RuntimeError: 当检测到拦截页且无法渲染时
        """
        chapter_url = getattr(chapter, "url", "")
        if not chapter_url:
            raise ValueError("章节 URL 为空")
        
        # Step 1: 尝试从预渲染缓存读取
        if self._pre_executor:
            cached_html = await self._pre_executor.get_cached_template(chapter_url)
            if cached_html:
                self._cache_hits += 1
                logger.debug("命中预渲染缓存：%s (%d 字符)", 
                           chapter_url, len(cached_html))
                
                # 解析静态 HTML
                content = await self._parse_static_html(cached_html, chapter)
                return content
        
        # Step 2: 缓存未命中，尝试纯 HTTP 请求
        self._cache_misses += 1
        logger.info("缓存未命中，尝试 HTTP 请求：%s", chapter_url)
        
        try:
            # 调用父类的 HTTP 请求逻辑（不带 WebView）
            content = await super().get_content(source, chapter, **kwargs)
            
            # 检查是否返回了拦截页
            if self._is_gate_page(content):
                raise RuntimeError(
                    f"该章节需要人工验证（Cloudflare/登录等），"
                    f"请在桌面端打开此书或使用其他书源\n"
                    f"章节：{getattr(chapter, 'title', '')}\n"
                    f"URL：{chapter_url}"
                )
            
            return content
            
        except Exception as exc:
            # 如果是网络错误，给出更友好的提示
            error_msg = str(exc)
            if "Connection refused" in error_msg or "Timeout" in error_msg:
                raise RuntimeError(
                    f"无法连接到书源服务器，请检查网络连接\n"
                    f"章节：{getattr(chapter, 'title', '')}"
                ) from exc
            raise
    
    async def _parse_static_html(self, html: str, chapter: Any) -> ChapterContent:
        """解析静态 HTML 为 ChapterContent。
        
        Args:
            html: 渲染后的 HTML 字符串
            chapter: 章节对象
            
        Returns:
            ChapterContent
        """
        from bs4 import BeautifulSoup
        
        soup = BeautifulSoup(html, "html.parser")
        
        # 提取标题（如果 chapter.title 为空）
        title = getattr(chapter, "title", "")
        if not title:
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else ""
        
        # 提取正文（移除 script/style/nav/footer 等无关元素）
        for tag_name in ["script", "style", "nav", "footer", "header"]:
            for tag in soup.find_all(tag_name):
                tag.decompose()
        
        # 查找正文容器（常见选择器）
        body_container = None
        for selector in ["article", ".content", "#content", ".text", "main"]:
            container = soup.select_one(selector)
            if container:
                body_container = container
                break
        
        if body_container is None:
            # 降级：用整个 body
            body_container = soup.find("body") or soup
        
        # 提取文本块
        text_blocks = []
        for element in body_container.find_all(["p", "div", "br"]):
            text = element.get_text(strip=True)
            if text:
                text_blocks.append(ContentBlock(kind="text", value=text))
        
        # 如果没有找到段落，尝试直接取文本
        if not text_blocks:
            full_text = body_container.get_text(separator="\n", strip=True)
            if full_text:
                text_blocks.append(ContentBlock(kind="text", value=full_text))
        
        return ChapterContent(
            blocks=text_blocks,
            title=title,
            raw_content=html
        )
    
    @staticmethod
    def _is_gate_page(content: ChapterContent) -> bool:
        """检测是否为拦截页（Cloudflare/登录等）。
        
        复用 app.services.block_pages.looks_like_gate 的逻辑。
        """
        from app.services.block_pages import looks_like_gate
        
        # 拼接所有文本块
        full_text = "\n".join(
            str(block.value) for block in content.blocks if block.kind == "text"
        )
        
        return looks_like_gate(full_text)
    
    @property
    def cache_stats(self) -> dict:
        """缓存统计信息。"""
        total = self._cache_hits + self._cache_misses
        hit_rate = (self._cache_hits / total * 100) if total > 0 else 0
        
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "total": total,
            "hit_rate": f"{hit_rate:.1f}%",
        }
    
    def reset_cache_stats(self) -> None:
        """重置缓存统计。"""
        self._cache_hits = 0
        self._cache_misses = 0
