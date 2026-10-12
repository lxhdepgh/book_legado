"""PreRenderSync：预渲染同步服务。

核心思路：将桌面端预执行的章节模板同步到 Android 设备。

同步方式（按优先级）：
1. WebDAV 同步（推荐，跨平台通用）
2. 局域网 HTTP 推送（快速测试用）
3. 文件导出/导入（离线场景）

设计依据：
- app/services/sync/webdav_sync.py（现有 WebDAV 同步服务）
- reference_code/legado-lyc486 的 CookieStore/WebCacheManager（缓存管理思路）
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncProgress:
    """同步进度跟踪。"""
    
    total_chapters: int = 0
    synced_chapters: int = 0
    failed_chapters: int = 0
    current_chapter_url: str = ""
    status: str = "idle"  # idle / syncing / completed / failed
    error_message: str = ""


class PreRenderSync:
    """将桌面端预渲染的结果同步到 Android 设备。
    
    使用方式：
        sync = PreRenderSync(pre_executor, webdav_service)
        await sync.sync_book(book_id, device_id="android_device_1")
    """
    
    def __init__(self, pre_executor=None, webdav_service=None, storage=None):
        """
        Args:
            pre_executor: RulePreExecutor 实例
            webdav_service: WebDAVService 实例（用于同步）
            storage: StorageService 实例（用于读取缓存）
        """
        self._pre_executor = pre_executor
        self._webdav_service = webdav_service
        self._storage = storage
        self._progress = SyncProgress()
    
    async def sync_book(
        self,
        book_id: str,
        source: Any = None,
        device_id: str | None = None,
        chapters: list[dict] | None = None,
        force_refresh: bool = False
    ) -> SyncProgress:
        """同步一本书的所有章节预渲染结果。
        
        Args:
            book_id: 书籍 ID
            source: 书源对象
            device_id: 目标设备 ID（可选，用于多设备同步）
            chapters: 章节列表（可选，若不提供则从 storage 读取）
            force_refresh: 强制重新预渲染
            
        Returns:
            SyncProgress（同步进度）
        """
        self._progress = SyncProgress(status="syncing")
        
        try:
            # Step 1: 获取章节列表
            if chapters is None:
                chapters = await self._get_chapters_for_book(book_id)
            
            if not chapters:
                logger.warning("未找到书籍 %s 的章节列表", book_id)
                self._progress.status = "failed"
                self._progress.error_message = "未找到章节列表"
                return self._progress
            
            self._progress.total_chapters = len(chapters)
            logger.info("开始同步：%d 个章节", len(chapters))
            
            # Step 2: 确保所有章节已预渲染
            await self._ensure_all_rendered(chapters, source, force_refresh)
            
            # Step 3: 打包同步数据
            sync_data = await self._package_sync_data(book_id, chapters)
            
            # Step 4: 推送到目标设备
            if self._webdav_service:
                await self._push_via_webdav(book_id, sync_data, device_id)
            else:
                # 降级：保存到本地文件
                await self._save_to_export_file(book_id, sync_data)
            
            self._progress.status = "completed"
            logger.info("同步完成：%s", book_id)
            
        except Exception as exc:
            logger.exception("同步失败：%s - %s", book_id, exc)
            self._progress.status = "failed"
            self._progress.error_message = str(exc)
        
        return self._progress
    
    async def sync_selected_chapters(
        self,
        chapter_urls: list[str],
        source: Any = None,
        device_id: str | None = None
    ) -> SyncProgress:
        """同步选中的章节（而非整本书）。
        
        Args:
            chapter_urls: 章节 URL 列表
            source: 书源对象
            device_id: 目标设备 ID
            
        Returns:
            SyncProgress
        """
        self._progress = SyncProgress(status="syncing")
        
        try:
            chapters = [{"url": url, "title": ""} for url in chapter_urls]
            self._progress.total_chapters = len(chapters)
            
            # 预渲染
            await self._ensure_all_rendered(chapters, source)
            
            # 打包并推送
            sync_data = {
                "type": "selected_chapters",
                "chapters": {
                    url: await self._pre_executor.get_cached_template(url)
                    for url in chapter_urls
                }
            }
            
            if self._webdav_service:
                await self._push_via_webdav("selected", sync_data, device_id)
            else:
                await self._save_to_export_file("selected", sync_data)
            
            self._progress.status = "completed"
            
        except Exception as exc:
            logger.exception("同步失败：%s", exc)
            self._progress.status = "failed"
            self._progress.error_message = str(exc)
        
        return self._progress
    
    @property
    def progress(self) -> SyncProgress:
        """当前同步进度。"""
        return self._progress
    
    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #
    
    async def _get_chapters_for_book(self, book_id: str) -> list[dict]:
        """从 storage 读取书籍的章节列表。"""
        if not self._storage:
            logger.warning("StorageService 未注入，无法读取章节列表")
            return []
        
        try:
            key = f"catalog:{book_id}"
            data = await self._storage.get(key)
            if data is None:
                return []
            
            chapters = json.loads(data)
            # 标准化格式
            return [
                {"url": ch.get("url", ""), "title": ch.get("title", "")}
                for ch in chapters
                if ch.get("url")
            ]
            
        except Exception as exc:
            logger.error("读取章节列表失败：%s - %s", book_id, exc)
            return []
    
    async def _ensure_all_rendered(
        self,
        chapters: list[dict],
        source: Any = None,
        force_refresh: bool = False
    ) -> None:
        """确保所有章节都已预渲染。"""
        if not self._pre_executor:
            logger.warning("RulePreExecutor 未注入，跳过预渲染检查")
            return
        
        for i, chapter in enumerate(chapters):
            url = chapter["url"]
            
            # 更新进度
            self._progress.current_chapter_url = url
            self._progress.synced_chapters = i
            
            # 检查是否已缓存
            cached = await self._pre_executor.get_cached_template(url)
            if cached and not force_refresh:
                continue
            
            # 预渲染
            result = await self._pre_executor.pre_execute_chapter(
                url, source=source, force_refresh=force_refresh
            )
            
            if result is None:
                self._progress.failed_chapters += 1
                logger.warning("章节预渲染失败：%s", url)
            
            # 每 10 个章节让出一次控制权
            if i % 10 == 0:
                await asyncio.sleep(0)
    
    async def _package_sync_data(
        self,
        book_id: str,
        chapters: list[dict]
    ) -> dict:
        """打包同步数据。"""
        sync_data = {
            "type": "book",
            "book_id": book_id,
            "chapters": {}
        }
        
        for chapter in chapters:
            url = chapter["url"]
            html = await self._pre_executor.get_cached_template(url) if self._pre_executor else None
            if html:
                sync_data["chapters"][url] = html
        
        return sync_data
    
    async def _push_via_webdav(
        self,
        book_id: str,
        sync_data: dict,
        device_id: str | None = None
    ) -> None:
        """通过 WebDAV 推送同步数据。"""
        if not self._webdav_service:
            raise RuntimeError("WebDAVService 未注入")
        
        # 构建远程路径
        device_prefix = device_id or "default"
        remote_path = f"/legodo/pre_render/{device_prefix}/{book_id}.json"
        
        # 序列化为 JSON
        content = json.dumps(sync_data, ensure_ascii=False)
        
        # 上传到 WebDAV
        await self._webdav_service.upload(
            remote_path,
            content.encode("utf-8"),
            overwrite=True
        )
        
        logger.info("已通过 WebDAV 推送：%s → %s", book_id, remote_path)
    
    async def _save_to_export_file(self, book_id: str, sync_data: dict) -> None:
        """降级方案：保存为本地导出文件。"""
        import os
        from pathlib import Path
        
        export_dir = Path("exports/pre_render")
        export_dir.mkdir(parents=True, exist_ok=True)
        
        file_path = export_dir / f"{book_id}.json"
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(sync_data, f, ensure_ascii=False, indent=2)
        
        logger.info("已保存到导出文件：%s", file_path)
    
    async def import_from_file(self, file_path: str) -> int:
        """从导出文件导入预渲染数据（Android 端调用）。
        
        Args:
            file_path: JSON 文件路径
            
        Returns:
            导入的章节数量
        """
        from pathlib import Path
        
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{file_path}")
        
        with open(path, "r", encoding="utf-8") as f:
            sync_data = json.load(f)
        
        chapters = sync_data.get("chapters", {})
        count = 0
        
        for url, html in chapters.items():
            if self._pre_executor and self._storage:
                # 写入缓存
                key = f"{self._pre_executor._cache_key_prefix}{url}"
                await self._storage.set(key, json.dumps({
                    "chapter_url": url,
                    "content_html": html,
                    "js_executed": True,
                }))
                count += 1
        
        logger.info("已从文件导入 %d 个章节：%s", count, file_path)
        return count
