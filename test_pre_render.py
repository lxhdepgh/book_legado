"""测试预渲染缓存读取和解析功能。"""
import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent / "legodo_flet"))

async def main():
    from app.core.storage import StorageService
    from app.services.engine.rule_pre_executor import RulePreExecutor
    
    # 创建 StorageService
    storage = StorageService()
    await storage.on_start()
    
    # 创建 RulePreExecutor
    executor = RulePreExecutor(browser_service=None, storage=storage)
    await executor.on_start()
    
    # 测试读取缓存
    test_url = "https://www.qbmfxs.com/book_1/65596/51.html"
    
    print(f"Test URL: {test_url}")
    cached_html = await executor.get_cached_template(test_url)
    
    if cached_html:
        print(f"[OK] Cache hit! HTML length: {len(cached_html)} chars")
        
        # Test parsing
        from app.services.reading.reading_service import ReadingService
        
        # Create a mock chapter object
        class MockChapter:
            url = test_url
            title = ""
            name = "Chapter 51"
        
        chapter = MockChapter()
        
        # Call _parse_static_html
        document = await ReadingService._parse_static_html(None, cached_html, chapter)
        
        print(f"[OK] Parse success!")
        print(f"  - Title: {document.title}")
        print(f"  - Blocks: {len(document.blocks)}")
        if document.blocks:
            first_block = document.blocks[0].value
            print(f"  - First block ({len(first_block)} chars): {repr(first_block[:60])}")
        total_chars = sum(len(b.value) for b in document.blocks)
        print(f"  - Total chars: {total_chars}")
    else:
        print("[FAIL] Cache miss")
    
    await storage.on_stop()

if __name__ == "__main__":
    asyncio.run(main())
