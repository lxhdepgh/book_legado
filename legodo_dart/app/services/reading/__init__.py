"""M2 阅读域服务层（services/reading/）。

六个服务均继承 :class:`app.core.lifecycle.Service`，零 flet 依赖，服务间只经
EventBus / 显式注入 / ServiceHub.get() 协作：

- :mod:`reading_service`   ReadingService：阅读上下文/进度/搜索换源编排；
- :mod:`bookmark_service`  BookmarkService：用户书签 CRUD；
- :mod:`content_search`    ContentSearchService：书内 FTS5 全文检索；
- :mod:`book_cache_service` BookCacheService：整书离线缓存（旧 WholeBookCacheService 外壳）；
- :mod:`download_service`  DownloadService：下载/缓存任务队列与进度事件；
- :mod:`image_book_service` ImageBookService：image 类型书服务能力（视图 M4 接）。
"""
