"""ReadingService：阅读上下文 + 进度 + 搜索/换源编排（M2，契约 §3）。

承接旧 ReadingView 的状态字段收敛：

- 阅读会话（目录列表 + 视口坐标）由迁入的 :class:`ReadingSession` 持有，
  :class:`ReadingPosition` 连续滚动定位模型**逻辑零改动**（自 old_code 机械迁入，
  仅去掉对 bookshelf.json 的依赖——进度改落 SQLite ``books`` 表结构化列）；
- 进度 save/restore 走 StorageService ``books`` 表，发布 ``ProgressSavedEvent``；
- 阅读器布局偏好（字体/主题/滚动）并入 SettingsService（键前缀 ``reader.``）；
- 找书/换源复用 :class:`~app.services.search_model.SearchModel` 链路，发布
  ``SearchFinishedEvent``；书架/notShelf 语义**组合**旧
  :class:`~app.services.source_service.SourceService`（未外壳化，用组合而非继承，
  且不改动该文件）。

红线：本模块零 flet import；不引入新 JS 引擎。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.core.events import (
    ProgressSavedEvent,
    ReadingSessionChangedEvent,
    SearchFinishedEvent,
)
from app.core.lifecycle import Service
from app.core.text_layout import chapter_paragraphs, chapter_text_length
from app.services.block_pages import SiteBlockedError
from app.services.engine.rule_engine import (
    ChapterItem,
    RequestConnectionError,
    RequestTimeoutError,
    UnsupportedRule,
)
from app.services.engine.reading_engine import (
    ChapterContent,
    display_blocks,
    display_document,
)
from app.services.reading_cache import ReadingCache
from app.services.search_model import SearchModel, SearchBookStore

# 启动拓扑依赖（契约 §1）：经 ServiceHub 拓扑排序启动；运行期协作均经 on_start
# 的 hub.require 解析或构造期注入，故此处 import 仅用于拓扑声明。
from app.services.engine.rule_engine_service import RuleEngineService

logger = logging.getLogger(__name__)

# 阅读器布局偏好在 SettingsService 中的键前缀（契约 §3：键名沿用旧 reader-layout.json）。
_READER_PREFIX = "reader."

# 换源扫描（对齐 Legado ChangeBookSourceViewModel：mapParallel(32) + 每源 withTimeout）。
# 引擎会话是池化的，所以超时用 asyncio.wait_for 兜在调用点，而非新建短命引擎。
# 并发数**不照抄 32**：参考实现那 32 路摊在 9 条真实 OS 线程上，我们的执行域只有
# 一条事件循环线程（app/core/engine_worker.py），32 路只是 32 个协程。实测 32 源
# 回环扫描的墙钟-并发曲线：1 路 5758 / 2 路 5447 / 4 路 5302 / 8 路 5927 / 16 路
# 5899 / 32 路 6523 ms——最低点在 4，之后只涨不降（见 §6.6）。取 8 而不是 4：
# 回环没有网络段，真实网络上多出来的路数换的是等待重叠，这张表读出来的是**下限**。
SWITCH_CONCURRENCY = 8
SWITCH_TIMEOUT = 10
#: 一次"超时"里，本线程用掉的 CPU 占墙钟的比例高于此值 ⇒ 判"本机繁忙"。
SWITCH_BUSY_RATIO = 0.5
SWITCH_PUBLISH_INTERVAL = 0.5

#: 搜索种子库的保留期（Legado：`searchBookDao.clearExpired(now - 1 天)`）。
SEARCH_STORE_TTL_MS = 24 * 60 * 60 * 1000

# 章节后台预取（对齐旧 ReadingView.PREFETCH_CHAPTER_COUNT / PREFETCH_CACHE_LIMIT）：
# 读到章尾才现取下一章，弱网下就是一次可感知的停顿；预取把后面几章提前拉回来。
PREFETCH_CHAPTER_COUNT = 10
#: 预取正文的内存预算。超预算只是"这次不再留副本"，落盘缓存照写，等于退回旧行为。
PREFETCH_CACHE_LIMIT = 64 * 1024 * 1024


class ReaderSessionError(RuntimeError):
    """``reader.*`` 的调用顺序错了：动作要的是某一本书，但会话不是它。

    网关把异常名折进错误信封（``CODE_ENGINE`` + ``"ReaderSessionError: ..."``），
    客户端据此重开——它比"返回空目录"好查得多：空目录看起来像书坏了。
    """


def _local_source_for(origin: str, book):
    """本地书（``localsource://`` 伪源 / ``file://`` 书地址）的合成源对象。

    本地书没有规则源，``SourceService.load_source`` 一定查不到；查不到不等于书坏了，
    这里补一个只带 ``bookSourceUrl`` 的对象，让 reader-cache 的目录/正文键
    （导入时用的就是那个常量）命得中。非本地书返回 None，让上层照旧报"书源缺失"。
    """
    from app.services.content.local_book_service import (
        is_local_source_url, make_local_source)

    book_url = str(getattr(book, "url", "") or "")
    if is_local_source_url(origin) or book_url.startswith("file://"):
        return make_local_source()
    return None


def _is_local_source(source) -> bool:
    from app.services.content.local_book_service import is_local_source_url
    return is_local_source_url(getattr(source, "bookSourceUrl", ""))


#: 换源失败原因里唯一一条"不是书源的错"：本机把时间花在替别的源解析上。
SWITCH_BUSY_REASON = "本机繁忙"


def _switch_cpu_share(wall_start: float, cpu_start: float) -> float:
    """这次等待里，本线程真正用掉的 CPU 占墙钟的比例。"""
    wall = time.perf_counter() - wall_start
    if wall <= 0:
        return 0.0
    return max(0.0, min(1.0, (time.thread_time() - cpu_start) / wall))


def _switch_starved(wall_start: float, cpu_start: float) -> bool:
    """一次"超时"该不该改记成本机排队。

    ``wait_for`` 读的是墙钟，而换源扫描的并发只是同一条事件循环线程上的几个协程：
    一个源被取出的那刻起，它的预算就在被别的源的解析烧掉。实测零延迟回环上单源孤立
    跑一次 155.5ms，同一批源 32 路并发时把预算压到 2s，32 个源里有 19 个被掐断——
    它们自己的活只值 155ms，其余全是排队。
    把这些算成"书源超时"，用户看到的就是一屏"全都失败"，
    而真实网络只要再叠 1–2s 就会开始把健康源报成故障源。
    ``time.thread_time()`` 只累计**本线程**用掉的 CPU：等待期间 CPU÷墙钟高，
    说明线程一直在算（多半是别人的书在解析）；低才是真的在对端上耗着。
    """
    return _switch_cpu_share(wall_start, cpu_start) >= SWITCH_BUSY_RATIO


def _switch_failure_reason(exc: Exception) -> str:
    """把一次书源搜索失败归一成用户看得懂的短原因。"""
    if isinstance(exc, RequestTimeoutError):
        return "超时"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "超时"
    if isinstance(exc, UnsupportedRule):
        return "规则不支持"
    if isinstance(exc, SiteBlockedError):
        # 本机安全软件或站点反爬给了拦截页，书源规则本身没问题。
        return "被拦截"
    if isinstance(exc, RequestConnectionError):
        return "网络错误"
    if "UnsupportedJavaApiError" in str(exc):
        return "接口未模拟"
    return "规则或解析错误"


# --------------------------------------------------------------------------- #
# 连续滚动定位模型（自 old_code/app/services/reading_progress.py 迁入）。
# 仅删除依赖 bookshelf.json 的 ReadingProgressStore（M2 进度改走 SQLite books 表）；
# #88 在此基础上加了章内段序号（chapter_paragraph / ChapterViewport.paragraph_start）。
# --------------------------------------------------------------------------- #
def _paragraph_or_none(value) -> int | None:
    """段序号只在"存了且非负"时才算数：其余一律 None（= 未知，回退裸像素）。"""
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None


@dataclass(frozen=True)
class ReadingPosition:
    source_url: str
    book_url: str
    chapter_url: str = ""
    chapter_name: str = ""
    chapter_index: int = 0
    chapter_offset: float = 0.0
    chapter_ratio: float = 0.0
    character_offset: int = 0
    #: 章内自然段序号——跨装尺唯一不变的那个数（#88）。None = 这条记录没存过段号
    #: （v8 之前的老库、或旧 bookshelf.json 迁进来的进度），续读只能退回裸像素。
    chapter_paragraph: int | None = None
    font_size: int = 16
    updated_at: int = 0

    @classmethod
    def from_reading(cls, source_url, book_url, reading):
        value = reading or {}
        return cls(
            source_url=str(source_url or ""), book_url=str(book_url or ""),
            chapter_url=str(value.get("chapter_url") or ""),
            chapter_name=str(value.get("chapter_name") or ""),
            chapter_index=int(value.get("chapter_index", 0) or 0),
            chapter_offset=max(0.0, float(value.get("scroll_offset", 0) or 0)),
            chapter_ratio=max(0.0, min(
                1.0, float(value.get("scroll_ratio", 0) or 0))),
            character_offset=max(0, int(value.get("chapter_pos", 0) or 0)),
            chapter_paragraph=_paragraph_or_none(value.get("chapter_paragraph")),
            font_size=max(1, int(value.get("font_size", 16) or 16)),
            updated_at=int(value.get("last_read_at", 0) or 0),
        )

    def as_reading(self):
        return {
            "chapter_url": self.chapter_url,
            "chapter_name": self.chapter_name,
            "chapter_index": self.chapter_index,
            "scroll_offset": self.chapter_offset,
            "scroll_ratio": self.chapter_ratio,
            "chapter_pos": self.character_offset,
            "chapter_paragraph": self.chapter_paragraph,
            "font_size": self.font_size,
            "last_read_at": self.updated_at,
        }


@dataclass
class ChapterViewport:
    chapter: object
    document_start: float
    text_length: int
    #: 这一章的第一段在**整篇文档**里的段序号（连续阅读时前面还挂着别的章）。
    #: 落库的段序号必须是章内的，所以像素段号要减掉这个起点（#88）。
    paragraph_start: int = 0
    #: 本章每段段首在章内字符流里的偏移（``text_layout.paragraph_offsets``）。
    #: 空 = 这一章没算过坐标（老调用方、只挂控件不读正文的替身），那时字符偏移
    #: 退回旧的像素插值口径——那条口径 #88 实测连同尺差 7 段，只当兜底不当真相。
    paragraph_offsets: tuple[int, ...] = ()


class ReadingSession:
    """Single authority for visible chapter and chapter-local coordinates."""

    def __init__(self):
        self.reset()

    def reset(self, source_url="", book_url="", chapters=None, position=None):
        self.source_url = str(source_url or "")
        self.book_url = str(book_url or "")
        self.chapters = list(chapters or [])
        self.viewports: dict[str, ChapterViewport] = {}
        self.document_extent = 0.0
        self.position = position or ReadingPosition(
            self.source_url, self.book_url)

    def bind(self, source_url, book_url, chapters, reading=None):
        position = (reading if isinstance(reading, ReadingPosition) else
                    ReadingPosition.from_reading(source_url, book_url, reading))
        self.reset(source_url, book_url, chapters, position)

    def mount(self, chapter, document_start, text_length, paragraph_start=0,
              paragraph_offsets=()):
        self.viewports[str(chapter.url)] = ChapterViewport(
            chapter=chapter,
            document_start=max(0.0, float(document_start or 0)),
            text_length=max(0, int(text_length or 0)),
            paragraph_start=max(0, int(paragraph_start or 0)),
            paragraph_offsets=tuple(int(item) for item in (paragraph_offsets or ())))

    def track(self, document_offset, document_extent, font_size,
              paragraph_index=None):
        self.document_extent = max(0.0, float(document_extent or 0))
        offset = max(0.0, float(document_offset or 0))
        ordered = sorted(
            self.viewports.values(), key=lambda item: item.document_start)
        if not ordered:
            return self.position, None
        selected = ordered[0]
        selected_index = 0
        for index, viewport in enumerate(ordered):
            if viewport.document_start > offset:
                break
            selected, selected_index = viewport, index
        end = (ordered[selected_index + 1].document_start
               if selected_index + 1 < len(ordered)
               else self.document_extent)
        extent = max(0.0, end - selected.document_start)
        local_offset = max(0.0, offset - selected.document_start)
        ratio = min(1.0, local_offset / extent) if extent else 0.0
        chapter = selected.chapter
        # 视图给的是**整篇**段号：连续阅读时前面还挂着别的章，起点要减掉才是本章内的
        # 段号（不减，重开时这个数会指到别人的章里去，#88 取证二）。
        local_paragraph = _paragraph_or_none(paragraph_index)
        if local_paragraph is not None:
            local_paragraph = max(0, local_paragraph - selected.paragraph_start)
        # 字符偏移的**正解**：知道落在第几段，就按段首偏移算（章内字符流是两侧的
        # 同一把尺，见 app.core.text_layout）。量不到段号时才退回旧的像素插值——
        # 那一格是估算不是事实，#88 取证一实测它连同差 7 段。
        if local_paragraph is not None and selected.paragraph_offsets:
            character_offset = self._character_offset_at(
                selected.paragraph_offsets, local_paragraph, ratio)
        else:
            character_offset = int(selected.text_length * ratio)
        self.position = ReadingPosition(
            source_url=self.source_url, book_url=self.book_url,
            chapter_url=str(chapter.url or ""),
            chapter_name=str(chapter.name or ""),
            chapter_index=int(chapter.index or 0),
            chapter_offset=local_offset, chapter_ratio=ratio,
            character_offset=character_offset,
            chapter_paragraph=local_paragraph,
            font_size=int(font_size), updated_at=int(time.time()))
        return self.position, chapter

    @staticmethod
    def _character_offset_at(offsets, paragraph: int, ratio: float) -> int:
        """段号 → 章内字符偏移：段首是事实，段内那一截按像素比例估且**不越过段尾**。

        越过段尾就不是估算误差了——它会把人送进下一段，两侧于是各认一个段。
        最后一段没有下界（后面没有别的段可减），那一格就只认段首，不猜。
        """
        anchor = max(0, min(int(paragraph), len(offsets) - 1))
        start = int(offsets[anchor])
        span = (int(offsets[anchor + 1] - offsets[anchor]) - 1
                if anchor + 1 < len(offsets) else 0)
        return max(0, start + int(span * max(0.0, min(1.0, float(ratio)))))

    def enter(self, chapter, font_size, offset=0.0, ratio=0.0,
              character_offset=0, paragraph=None):
        self.position = ReadingPosition(
            source_url=self.source_url, book_url=self.book_url,
            chapter_url=str(chapter.url or ""),
            chapter_name=str(chapter.name or ""),
            chapter_index=int(chapter.index or 0),
            chapter_offset=max(0.0, float(offset or 0)),
            chapter_ratio=max(0.0, min(1.0, float(ratio or 0))),
            character_offset=max(0, int(character_offset or 0)),
            chapter_paragraph=_paragraph_or_none(paragraph),
            font_size=int(font_size), updated_at=int(time.time()))
        return self.position


# --------------------------------------------------------------------------- #
# ReadingService
# --------------------------------------------------------------------------- #
class ReadingService(Service):
    """阅读上下文、进度持久化、搜索/换源编排。"""

    name = "ReadingService"
    # 契约 §1：ReadingService 依赖 RuleEngineService（经 hub 取池化引擎）。
    dependencies: tuple[type[Service], ...] = (RuleEngineService,)

    def __init__(self, rule_engine=None, storage=None, settings=None,
                 source_service=None, cache: ReadingCache | None = None,
                 search_store: SearchBookStore | None = None) -> None:
        super().__init__()
        # 协作者全部可注入（测试替身）；未注入时 on_start 经 hub 解析。
        self._engine = rule_engine
        self._storage = storage
        self._settings = settings
        self._source_service = source_service
        self._cache = cache
        self._search_store = search_store
        # 当前阅读会话状态（同步直达属性，供 EngineProxy 透传）。
        self._current_source = None
        self._current_book = None
        self._session = ReadingSession()
        # 章节预取：内存副本 + 在途请求表 + 后台任务（换书/关会话一起作废）。
        self._prefetch_cache: dict[tuple[str, str, str], ChapterContent] = {}
        self._content_flight: dict[tuple[str, str, str], asyncio.Future] = {}
        self._prefetch_task: asyncio.Task | None = None
        # 长驻搜索模型（searchId/页码/generation 的唯一持有者）。
        self._search_model: SearchModel | None = None

    # ------------------------------------------------------------------ #
    # 生命周期：经 hub 补齐未注入的基础设施
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            from app.core.storage import StorageService
            self._storage = self.hub.require(StorageService)
        if self._settings is None and self.hub is not None:
            from app.core.settings import SettingsService
            self._settings = self.hub.require(SettingsService)
        if self._engine is None and self.hub is not None:
            from app.services.engine.rule_engine_service import RuleEngineService
            self._engine = self.hub.require(RuleEngineService)
        # M3 集成点：未注入 _source_service 时经 hub 取外壳化 SourceService
        # （懒 import 避免与 sources 包循环依赖）；M2 服务测试用注入替身不受影响。
        if self._source_service is None and self.hub is not None:
            from app.services.sources.source_service import SourceService
            self._source_service = self.hub.require(SourceService)
        if self._cache is None and self._storage is not None:
            # ReadingCache 会在给定根下追加 "reader-cache"，故这里给缓存根目录，
            # 落地磁盘布局与旧行为一致：<cache>/reader-cache/。
            self._cache = ReadingCache(self._storage.paths.cache)
        # M6：未注入换源种子 store 时默认落主库 search_books 表（契约 §3）；
        # 主库未就绪则保持 None（SearchModel 不写种子，业务不受影响）。
        if self._search_store is None and self._storage is not None:
            self._search_store = SearchBookStore(conn=self._storage.db)
        if self._search_store is not None:
            # Legado App.kt 启动即 clearExpired(now - 1 天)：搜索结果只当短期种子。
            try:
                self._search_store.clear_expired(
                    int(time.time() * 1000) - SEARCH_STORE_TTL_MS)
            except Exception as exc:  # 清理失败不影响读种子/搜索
                logger.warning("搜索结果过期清理失败：%s", exc)
        logger.info("ReadingService 已启动")

    async def on_stop(self) -> None:
        self._cancel_prefetch()
        self._prefetch_cache.clear()
        if self._search_model is not None:
            await self._search_model.close()
            self._search_model = None
        self._current_source = None
        self._current_book = None
        self._session = ReadingSession()

    # ------------------------------------------------------------------ #
    # 同步直达属性（契约 §3：current_book / current_session 经 EngineProxy 透传）
    # ------------------------------------------------------------------ #
    @property
    def current_book(self):
        return self._current_book

    @property
    def current_session(self) -> ReadingSession:
        return self._session

    @property
    def current_source(self):
        return self._current_source

    def _publish(self, event):
        if self.bus is None:
            return []
        return self.bus.publish(event)

    # ------------------------------------------------------------------ #
    # 打开 / 关闭会话
    # ------------------------------------------------------------------ #
    async def open_book(self, source, book, shelve: bool = True,
                        resume: bool = True) -> ReadingSession:
        """启动阅读会话：加载目录、恢复进度、（按需）入书架。

        ``book`` 允许是搜索结果/书架里的字典快照——UI 侧流转的一直是快照，
        这里统一还原成 BookItem，否则后续 ``getattr(book, "url")`` 全取到空串，
        目录、进度、书架记录会一起哑掉。
        """
        book = as_book_item(book)
        self._reset_for_new_book()
        if source is None:
            # 书架开书只有记录，没有源对象：按记录的 origin 反查书源。
            origin = (str(getattr(book, "origin", "") or "")
                      or str(getattr(book, "source_url", "") or ""))
            if origin and self._source_service is not None \
                    and hasattr(self._source_service, "load_source"):
                try:
                    source = await self._source_service.load_source(origin)
                except Exception as exc:
                    logger.warning("按 origin 反查书源失败（%s）：%s", origin, exc)
            if source is None:
                # 反查仍为空 ≠ 书坏了：本地书压根没有规则源，SourceService 里查不到。
                source = _local_source_for(origin, book)
        self._current_source = source
        self._current_book = book
        chapters = await self.load_chapters(book)
        position = await self.restore_position(book) if resume else None
        self._session = ReadingSession()
        self._session.bind(
            getattr(source, "bookSourceUrl", "") or "",
            getattr(book, "url", "") or "",
            chapters, position)
        # notShelf 语义委托旧 SourceService（组合，不改其文件）。
        if self._source_service is not None:
            try:
                self._source_service.add_to_bookshelf(source, book, shelved=shelve)
            except Exception as exc:  # 书架写入失败不阻断阅读
                logger.warning("入书架失败（已忽略）：%s", exc)
        self._publish(ReadingSessionChangedEvent(
            book_url=getattr(book, "url", "") or "",
            chapter_index=self._session.position.chapter_index))
        return self._session

    async def close_book(self, save_progress: bool = True) -> None:
        """结束会话：落盘进度；notShelf 退出语义委托旧 SourceService。"""
        if save_progress and self._current_book is not None:
            position = self._session.position
            await self.save_progress(position.chapter_index, position)
        self._reset_for_new_book()
        self._current_source = None
        self._current_book = None
        self._session = ReadingSession()

    # ------------------------------------------------------------------ #
    # 试读收尾（Legado「返回时提示放入书架」，§1.5）
    #
    # 全部依赖当前会话的 (source_url, book_url)：close_book 一清会话就找不回
    # 那条暂存记录了，所以调用方必须先收尾记录再关会话。
    # ------------------------------------------------------------------ #
    def _session_key(self) -> tuple[str, str]:
        """当前会话的 (书源 URL, 书籍 URL)；属性优先，缺源对象时退回会话。"""
        source_url = (str(getattr(self._current_source, "bookSourceUrl", "") or "")
                      or self._session.source_url)
        book_url = (str(getattr(self._current_book, "url", "") or "")
                    or self._session.book_url)
        return source_url, book_url

    def staged_record(self) -> dict | None:
        """当前书若是试读暂存（Legado notShelf）给出书架里的活记录，否则 None。

        在架的书（含从书架点开续读的）返回 None——它们退出时不该被问一遍。
        """
        service = self._source_service
        source_url, book_url = self._session_key()
        if service is None or not book_url:
            return None
        record = service.find_bookshelf_item(
            {"source_url": source_url, "url": book_url})
        if record is None or service.is_shelved(record):
            return None
        return record

    async def promote_staged_book(self) -> bool:
        """试读转正：暂存记录原地变书架记录，进度/变量/目录缓存一律不动。"""
        service = self._source_service
        source_url, book_url = self._session_key()
        if service is None or not book_url:
            return False
        return bool(await service.promote_book(source_url, book_url))

    async def discard_staged_book(self) -> bool:
        """丢弃试读：记录、进度与缓存一起清掉（没入架的书不该留下任何痕迹）。

        进度落在 ``books`` 表、不随书架记录消失，缓存落在 reader-cache 目录，
        所以只删书架行等于删了个寂寞——下次启动它还赖在续读列表里。
        """
        service = self._source_service
        source_url, book_url = self._session_key()
        if service is None or not book_url:
            return False
        removed = bool(await service.unshelve_book(source_url, book_url))
        if self._storage is not None:
            try:
                self._storage.execute(
                    "DELETE FROM books WHERE book_url=?", (book_url,))
            except Exception as exc:  # 孤立的进度行是垃圾，不值得为它中断退出
                logger.debug("清理试读进度失败：%s", exc)
        if self._cache is not None:
            try:
                self._cache.remove_book(source_url, book_url)
            except Exception as exc:
                logger.debug("清理试读缓存失败：%s", exc)
        return removed

    # ------------------------------------------------------------------ #
    # 目录 / 正文管线（经注入引擎，复用 reader-cache）
    # ------------------------------------------------------------------ #
    async def load_chapters(self, book, force_refresh: bool = False) -> list[ChapterItem]:
        source = self._current_source
        source_url = getattr(source, "bookSourceUrl", "") or ""
        book_url = getattr(book, "url", "") or ""
        if self._cache is not None and not force_refresh:
            cached = self._cache.load_catalog(source_url, book_url)
            if cached:
                return cached
        if _is_local_source(source):
            # 本地书的目录/正文只活在 reader-cache 里。缓存丢了不能去问规则引擎——
            # 那是一条抓 file:// 的网络请求，失败还会被说成网络问题。
            from app.services.content.local_book_service import (
                LocalBookCacheMissing)
            raise LocalBookCacheMissing(
                f"本地书「{getattr(book, 'name', '') or book_url}」的目录缓存已丢失，"
                f"请在书架重新导入该文件")
        chapters = await self._engine.chapters(source, book)
        if self._cache is not None:
            self._cache.save_catalog(source_url, book_url, chapters)
        return chapters

    async def load_content(self, chapter, append: bool = False,
                           restore_offset: float = 0.0) -> ChapterContent:
        """取一章正文：预取副本 > 磁盘缓存（追加路径不读它）> 联网。

        ``append=True`` 是"读到底接下一章"那条臂：磁盘缓存里存的是上一轮读到
        的内容，续读要的是最新正文，所以跳过缓存**读**、照常写。
        """
        source, book = self._current_source, self._current_book
        key = self._content_key(source, book, chapter)
        prefetched = self._prefetch_cache.get(key)
        if prefetched is not None:
            return prefetched
        if self._cache is not None and not append:
            cached = self._cache.load_content(key[0], key[1], key[2])
            if cached is not None:
                self._remember_document(key, cached)
                return cached
        return await self._request_chapter(
            chapter, key, source, book, self._next_chapter_url(chapter))

    # ------------------------------------------------------------------ #
    # 章节预取（后台拉回后面几章：章尾续读不该再等一次网络）
    # ------------------------------------------------------------------ #
    async def prefetch_chapters(self, chapter) -> None:
        """挂出一章后起一轮后台预取；本协程不等网络，只负责把任务排上。"""
        self._cancel_prefetch()
        book = self._current_book
        if book is None:
            return
        self._prefetch_task = asyncio.ensure_future(
            self._prefetch_ahead(chapter, book))

    async def _prefetch_ahead(self, chapter, book) -> None:
        """顺序预取后续章节：一次只要一章，别和读者正在翻的那章抢请求。

        这一轮随时会被下一轮顶掉（换章）或作废（换书/退出会话）。被顶掉的轮子
        不碰已经在途的请求——它跑完照样进缓存，正是"翻到这一章立刻接上"的数据。
        """
        source = self._current_source
        for candidate in self._readable_after(chapter, PREFETCH_CHAPTER_COUNT):
            if (self._prefetch_task is not asyncio.current_task()
                    or self._current_book is not book
                    or self._current_source is not source):
                return
            key = self._content_key(source, book, candidate)
            if key in self._prefetch_cache:
                continue
            try:
                await self._request_chapter(
                    candidate, key, source, book,
                    self._next_chapter_url(candidate))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 预取失败绝不影响当前章节：真翻到这一章时照常请求。
                # 这里直接收轮而不是跳过：源连不通时后面九次只会连着撞墙，
                # 还把读者的那一章请求挤在后面排队。
                logger.debug("预取 %s 失败（已忽略）：%s",
                             getattr(candidate, "name", ""), exc)
                return

    def stop_prefetch(self) -> None:
        """停掉后台预取（阅读器切走/退出时调用；副本留着，回来还能用）。"""
        self._cancel_prefetch()

    def _cancel_prefetch(self) -> None:
        """停掉在途预取轮。

        任务挂在引擎循环上，而这里可能跑在别的 loop 上（测试里 ``on_stop`` 是
        新起的临时 loop），所以取消要投回任务自己的 loop，别跨线程直接下手。
        """
        task = self._prefetch_task
        self._prefetch_task = None
        if task is None or task.done():
            return
        loop = task.get_loop()
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None      # 调用方在普通线程上（测试收尾、detach）：走投递
        try:
            if current is loop:
                task.cancel()
            elif not loop.is_closed():
                loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            pass    # 那条 loop 已经关了：没人可通知，轮子也随之作废

    def _reset_for_new_book(self) -> None:
        """换书：上一本的预取轮与内存副本作废（在途请求不动，跑完照常进磁盘缓存）。"""
        self._cancel_prefetch()
        self._prefetch_cache.clear()

    def _readable_after(self, chapter, limit: int) -> list[ChapterItem]:
        """目录里 ``chapter`` 之后可读的 ``limit`` 章（跳过卷标题与空 URL）。"""
        chapters = self._session.chapters
        start = int(getattr(chapter, "index", 0) or 0) + 1
        for index, item in enumerate(chapters):
            if item is chapter or str(item.url) == str(chapter.url):
                start = index + 1
                break
        ahead = []
        for item in chapters[start:]:
            if item.is_volume or not item.url:
                continue
            ahead.append(item)
            if len(ahead) >= limit:
                break
        return ahead

    async def _request_chapter(self, chapter, key, source, book,
                               next_url: str = "") -> ChapterContent:
        """同一章同时只有一个请求：读者翻到的正是预取在跑的那章时直接接上结果。"""
        pending = self._content_flight.get(key)
        if pending is None:
            pending = asyncio.ensure_future(
                self._fetch_remote(chapter, key, source, book, next_url))
            self._content_flight[key] = pending
            pending.add_done_callback(
                lambda task, key=key: self._forget_flight(key, task))
        # shield：等待这一章的那一趟被取消（换章/切页），不该把另一趟预取
        # 正在等的请求一起带走——请求跑完进缓存才是对双方都划算的结果。
        return await asyncio.shield(pending)

    async def _fetch_remote(self, chapter, key, source, book,
                            next_url: str = "") -> ChapterContent:
        if _is_local_source(source):
            # 本地书的正文在导入时已逐章落盘；走到这里只剩"缓存被清了"一种可能。
            # 绝不能派引擎去抓 file:// —— 那是一条必然失败的联网请求，
            # 报错还会被当成网络问题（与 load_chapters 同一条理由）。
            from app.services.content.local_book_service import (
                LocalBookCacheMissing)
            raise LocalBookCacheMissing(
                f"本地书「{getattr(book, 'name', '') or getattr(chapter, 'name', '')}」"
                f"的正文缓存已丢失，请在书架重新导入该文件")
        document = await self._engine.content_document(source, chapter, next_url)
        if book is not None and self._cache is not None:
            self._cache.save_content(key[0], key[1], key[2], document)
        self._remember_document(key, document)
        return document

    def _forget_flight(self, key, task) -> None:
        if self._content_flight.get(key) is task:
            self._content_flight.pop(key, None)
        if not task.cancelled():
            # 没人等在结果上时也要取走异常，否则 asyncio 记一条警告。
            task.exception()

    def _remember_document(self, key, document) -> None:
        """按预算保留正文副本：超了只丢副本，磁盘缓存照写，等于退回旧行为。"""
        if key in self._prefetch_cache:
            return
        size = self._document_size(document)
        used = sum(self._document_size(item)
                   for item in self._prefetch_cache.values())
        if used + size > PREFETCH_CACHE_LIMIT:
            return
        self._prefetch_cache[key] = document

    @staticmethod
    def _document_size(document) -> int:
        return sum(len(block.value) if isinstance(block.value, bytes)
                   else len(str(block.value).encode("utf-8"))
                   for block in getattr(document, "blocks", None) or [])

    def _next_chapter_url(self, chapter) -> str:
        for index, item in enumerate(self._session.chapters):
            if item.url == chapter.url and index + 1 < len(self._session.chapters):
                return self._session.chapters[index + 1].url
        return ""

    @staticmethod
    def _content_key(source, book, chapter) -> tuple[str, str, str]:
        """正文身份键：(书源, 书, 章)。换源/换书后的同名章节不是同一份内容。"""
        return (str(getattr(source, "bookSourceUrl", "") or ""),
                str(getattr(book, "url", "") or ""),
                str(getattr(chapter, "url", "") or ""))

    # ------------------------------------------------------------------ #
    # 进度持久化（SQLite books 表结构化列，§3.9）
    # ------------------------------------------------------------------ #
    async def save_progress(self, chapter_index: int, position: ReadingPosition) -> None:
        """ReadingPosition 落 books 表结构化列，并发布 ProgressSavedEvent。"""
        conn = self._storage.db if self._storage is not None else None
        book = self._current_book
        book_url = position.book_url or getattr(book, "url", "") or ""
        source_url = (position.source_url
                      or getattr(self._current_source, "bookSourceUrl", "") or "")
        now = time.time()
        if conn is not None and book_url:
            conn.execute(
                "INSERT INTO books(book_url, source_url, name, author, kind,"
                " cover_url, chapter_index, scroll_offset, scroll_ratio,"
                " chapter_pos, chapter_paragraph, font_size, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(book_url) DO UPDATE SET "
                " source_url=excluded.source_url, name=excluded.name,"
                " author=excluded.author, kind=excluded.kind,"
                " cover_url=excluded.cover_url, chapter_index=excluded.chapter_index,"
                " scroll_offset=excluded.scroll_offset, scroll_ratio=excluded.scroll_ratio,"
                " chapter_pos=excluded.chapter_pos,"
                " chapter_paragraph=excluded.chapter_paragraph,"
                " font_size=excluded.font_size,"
                " updated_at=excluded.updated_at",
                (book_url, source_url,
                 getattr(book, "name", "") or "",
                 getattr(book, "author", "") or "",
                 getattr(book, "kind", "") or "",
                 getattr(book, "cover_url", "") or "",
                 int(chapter_index),
                 float(position.chapter_offset),
                 float(position.chapter_ratio),
                 int(position.character_offset),
                 position.chapter_paragraph,
                 int(position.font_size), now))
        self._publish(ProgressSavedEvent(
            book_url=book_url, chapter_index=int(chapter_index),
            position=position.as_reading()))

    async def restore_position(self, book) -> ReadingPosition | None:
        """从 books 表恢复续读位置；无记录返回 None。"""
        conn = self._storage.db if self._storage is not None else None
        book_url = getattr(book, "url", "") or ""
        if conn is None or not book_url:
            return None
        row = conn.execute(
            "SELECT source_url, book_url, chapter_index, scroll_offset,"
            " scroll_ratio, chapter_pos, font_size, updated_at,"
            " chapter_paragraph"
            " FROM books WHERE book_url=?", (book_url,)).fetchone()
        if row is None:
            return None
        return ReadingPosition(
            source_url=row[0] or "", book_url=row[1] or book_url,
            chapter_index=int(row[2] or 0),
            chapter_offset=float(row[3] or 0.0),
            chapter_ratio=float(row[4] or 0.0),
            character_offset=int(row[5] or 0),
            font_size=int(row[6] or 16),
            updated_at=int(row[7] or 0),
            chapter_paragraph=_paragraph_or_none(row[8]))

    # ------------------------------------------------------------------ #
    # 阅读器布局偏好（并入 SettingsService，§3.9）
    # ------------------------------------------------------------------ #
    async def save_reader_preference(self, key: str, value) -> None:
        if self._settings is not None:
            self._settings.set(_READER_PREFIX + str(key), value)

    async def get_reader_preference(self, key: str, default=None):
        if self._settings is None:
            return default
        return self._settings.get(_READER_PREFIX + str(key), default)

    # ------------------------------------------------------------------ #
    # 找书编排（复用 SearchModel + RuleEngineService.search）
    # ------------------------------------------------------------------ #
    def _ensure_search_model(self) -> SearchModel:
        """长驻搜索模型：searchId/页码/generation 由模型内部维护（Legado 语义）。

        每次新建模型会把分页与取消语义全部丢掉，所以这里进程内只留一个实例。
        ``on_update`` 让结果**逐源**推给界面，而不是等全部源跑完才一次性出。
        """
        if self._search_model is None:
            async def search_one(source, key: str, page: int):
                return await self._engine.search(source, key, page)

            self._search_model = SearchModel(
                search_one, store=self._search_store,
                on_update=self._publish_search_partial)
        return self._search_model

    def _publish_search_partial(self, books) -> None:
        """SearchModel 每合并一个源就推一次增量快照（finished=False）。"""
        self._publish(SearchFinishedEvent(
            search_id=str(self._search_model.search_id if self._search_model else ""),
            items=[_book_item_dict(item) for item in books],
            finished=False))

    @staticmethod
    def searchable_sources(sources, scope: str | None = None) -> list:
        """按搜索范围过滤：只留已启用且配了搜索规则的书源。

        ``scope`` 取书源分组名，或两个哨兵：``__all__``（空值同义）= 全部可搜源，
        ``__ungrouped__`` = 一个组名都没有的那些源。
        """
        scope = str(scope or "").strip()
        result = []
        for source in sources or []:
            if not getattr(source, "enabled", False):
                continue
            if not str(getattr(source, "searchUrl", "") or "").strip():
                continue
            # "未分组"不能豁免在外层守卫里：排掉它等于什么组都不筛、与 __all__
            # 混成一件事（inner 那一支根本到不了）。哨兵只豁免 __all__。
            if scope and scope != _SCOPE_ALL:
                group = str(getattr(source, "bookSourceGroup", "") or "")
                if scope == _SCOPE_UNGROUPED:
                    # 判"有没有组"按**切完 strip 再丢空**，不按原串 truthiness：
                    # bookSourceGroup=" " 在分组下拉里不算一个组（那边同样切丢），
                    # 按原串判会让这条源既不属于任何组、也不属于"未分组"，两头都搜不到。
                    if [g for g in (p.strip() for p in group.split(",")) if g]:
                        continue
                elif scope not in [g.strip() for g in group.split(",")]:
                    continue
            result.append(source)
        return result

    @staticmethod
    def scope_table(sources) -> list[dict]:
        """搜索范围下拉的那张表：两个哨兵 + 排序去重的分组名，各带可搜源数。

        放在服务侧而不是客户端侧，是因为"这个范围里有几条源"这个问题只有
        :meth:`searchable_sources` 一个答案来源；客户端各自数一遍，就是各自
        抄一遍那条规则（停用 / 没配 searchUrl 的源进不进数，两侧会飘）。
        分组名的切法与 :meth:`searchable_sources` 判定"未分组"用的是同一条
        （strip 后丢空），所以这里列出的组与"未分组"那一格不会重叠、不会漏。
        """
        groups = sorted({g.strip() for source in sources or []
                         for g in str(getattr(source, "bookSourceGroup", "") or "")
                         .split(",") if g.strip()})
        return [{"scope": scope,
                 "count": len(ReadingService.searchable_sources(sources, scope))}
                for scope in [_SCOPE_ALL, _SCOPE_UNGROUPED, *groups]]

    async def search_books(self, keyword: str, sources, scope=None,
                           search_id: int = 0):
        """多源找书：复用 SearchModel 的 generation/分页语义，完成后发事件。

        同一个 ``search_id`` 再调一次即翻页（模型内页码自增）；换 ``search_id``
        即取消上一轮并回到第 1 页。结果逐源经 ``finished=False`` 事件推出，
        本轮扫完再发一条 ``finished=True`` 收口。
        """
        model = self._ensure_search_model()
        pool = self.searchable_sources(sources, scope)
        items = await model.search(int(search_id), str(keyword), pool)
        self._publish(SearchFinishedEvent(
            search_id=str(search_id),
            items=[_book_item_dict(item) for item in items],
            finished=True))
        return items

    async def stop_search(self) -> None:
        """停止当前搜索：取消在飞任务并自增 generation（结果不再回界面）。"""
        model = self._search_model
        if model is not None:
            await model.cancel()

    # ------------------------------------------------------------------ #
    # 分类浏览（explore）：书源分类清单 + 分页书单
    # ------------------------------------------------------------------ #
    async def explore_categories(self, source) -> list[dict]:
        """解析书源 exploreUrl（静态与 @js: 动态都走引擎），返回字典快照。"""
        categories = await self._engine.explore_categories(source)
        return [{"title": getattr(c, "title", ""),
                 "url": getattr(c, "url", "")} for c in categories or []]

    async def explore_books(self, source, category, page: int = 1) -> list[dict]:
        """分类下一页书单；``category`` 可为字典快照或 ExploreCategory。"""
        from app.services.engine.rule_engine import ExploreCategory
        if isinstance(category, dict):
            category = ExploreCategory(
                str(category.get("title") or ""), str(category.get("url") or ""))
        books = await self._engine.explore(source, category, page)
        return [_book_item_dict(book) for book in books or []]

    # ------------------------------------------------------------------ #
    # 协议门面：只吃 JSON 的找书面（设计 §4.2，G2）
    # ------------------------------------------------------------------ #
    async def _source_by_url(self, source_url: str):
        """``bookSourceUrl`` → BookSource；取不到就当场说清是哪一步没了。

        跨进程那一侧报不出 BookSource 对象（那是本进程的模型），只报得到 url。
        把"换回来"这件事放在服务里而不是网关里，是为了让网关继续只认 action 表，
        也为了让这三条门面在进程内测试里就能被跑到。
        """
        if self._source_service is None:
            raise RuntimeError("SourceService 未就绪：按 url 取不到书源")
        source = await self._source_service.load_source(str(source_url or ""))
        if source is None:
            raise ValueError(f"书源不存在：{source_url}")
        return source

    async def browse_categories(self, source_url: str) -> list[dict]:
        """:meth:`explore_categories` 的按 url 版（入参与返回都是纯 JSON）。"""
        return await self.explore_categories(await self._source_by_url(source_url))

    async def browse_books(self, source_url: str, category: dict,
                           page: int = 1) -> list[dict]:
        """:meth:`explore_books` 的按 url 版；``category`` 用 browse_categories 回的那一条。"""
        return await self.explore_books(
            await self._source_by_url(source_url), category,
            page=max(1, int(page or 1)))

    async def search_in_scope(self, keyword: str, scope: str = "",
                              search_id: int = 0) -> list[dict]:
        """:meth:`search_books` 的门面：``sources`` 换成"当前全部书源"。

        ``scope`` 的取值口径沿用控制器那套（分组名 / ``__all__`` / ``__ungrouped``），
        过滤仍然走 :meth:`searchable_sources`——这里只补"从哪儿拿源"这一件事，
        不另起一套范围语义。返回按 ``_book_item_dict`` 归一，与
        ``SearchFinishedEvent`` 的 ``items`` 是**同一个形状**：同一批结果走两条路
        回客户端却有两种字段，客户端就得写两套 fromJson（设计 §4.5 点名要避免的债）。
        """
        if self._source_service is None:
            raise RuntimeError("SourceService 未就绪：搜索没有源可查")
        items = await self.search_books(
            str(keyword or ""), await self._source_service.list_sources(),
            scope=scope or None, search_id=int(search_id or 0))
        return [_book_item_dict(item) for item in items or []]

    async def search_scopes(self) -> list[dict]:
        """:meth:`scope_table` 的门面：源从 ``SourceService.list_sources()`` 现拿。

        客户端只拿这张表画下拉、只把 ``scope`` 那一格原样传回 ``search.start``，
        所以"哪些范围可选、各带几条可搜源"始终只有服务端一份算法。
        """
        if self._source_service is None:
            raise RuntimeError("SourceService 未就绪：算不出搜索范围")
        return self.scope_table(await self._source_service.list_sources())

    # ------------------------------------------------------------------ #
    # 换源（先本地种子、后联网扫描；见 docs/找书与换源逻辑对齐.md）
    # ------------------------------------------------------------------ #
    async def switch_seeds(self, item) -> list[dict]:
        """换源对话框打开时的本地种子：之前搜索落库的同名书（不联网，秒开）。

        站点可达性未知，只当候选——真正的核对由 :meth:`switch_source` 联网完成。
        """
        record = as_book_item(item)
        if self._search_store is None:
            return []
        try:
            stored = self._search_store.list_by_name_author(
                str(record.name or ""), str(record.author or ""))
        except Exception as exc:  # 种子只是加速，读不到不影响换源
            logger.warning("换源种子读取失败：%s", exc)
            return []
        return [_book_item_dict(book) for book in stored or []]

    async def switch_source(self, item, candidates, keyword: str = "",
                           on_progress=None) -> list:
        """换源联网核对 + 候选排序。

        ``candidates`` 为按优先级排好序的书源列表（同站同域名在前、已判失效在
        后，排序在调用侧 ``BookshelfController._switch_candidates`` 里做）。
        扫描是**有界并发 + 每源超时**（Legado 的 `mapParallel(threadCount=32)` +
        `withTimeout`，我们按实测拐点收：见 ``SWITCH_CONCURRENCY``）：逐源串行的话，
        上千个书源里只要有几个慢站就把整轮拖到用户以为卡死；单源失败只计入
        ``failures``，不让整轮中止。
        ``on_progress(completed, total, failures, starved)`` 用 0.5s 节流回调，
        供界面刷进度；``starved`` 是"本机繁忙"的源数——那些不是书源的错，
        不能和 ``failures`` 混成一个数（见 :func:`_switch_starved`）。
        命中判定用**书名包含**（Legado 默认不校验作者，作者只当加分项），
        最后按 (书名精确 > 作者对得上 > 书源优先级 > 站名) 排序返回
        ``{"source_url", "source_name", "book": 快照}`` 列表。
        """
        key = str(keyword or getattr(item, "name", "") or
                  (item.get("name") if isinstance(item, dict) else "") or "")
        wanted = key.casefold()
        sources = list(candidates or [])
        if not wanted or not sources:
            return []
        queue: asyncio.Queue = asyncio.Queue()
        for source in sources:
            queue.put_nowait(source)
        total = queue.qsize()
        matches: list[tuple] = []
        failures: dict[str, int] = {}
        state = {"completed": 0, "published_at": time.monotonic()}

        def publish(force: bool = False) -> None:
            if on_progress is None:
                return
            now = time.monotonic()
            if not force and now - state["published_at"] < SWITCH_PUBLISH_INTERVAL:
                return
            state["published_at"] = now
            # "本机繁忙"不是书源的错，不能混进"失败"这个数里：混了以后界面写着
            # "38 个失败"，用户去删的却是 38 个好源。
            starved = failures.get(SWITCH_BUSY_REASON, 0)
            try:
                on_progress(state["completed"], total,
                            sum(failures.values()) - starved, starved)
            except Exception as exc:  # 进度只是反馈，不能带崩扫描
                logger.debug("换源进度回调失败：%s", exc)

        async def worker() -> None:
            while True:
                try:
                    source = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                name = str(getattr(source, "bookSourceUrl", "") or "")
                wall_start = time.perf_counter()
                cpu_start = time.thread_time()
                try:
                    books = await asyncio.wait_for(
                        self._engine.search(source, key, 1), SWITCH_TIMEOUT)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # 静默吞掉失败会让"找不到书"和"书源坏了"看起来一样。
                    reason = _switch_failure_reason(exc)
                    if reason == "超时" and _switch_starved(wall_start, cpu_start):
                        reason = SWITCH_BUSY_REASON
                    failures[reason] = failures.get(reason, 0) + 1
                    logger.debug("换源扫描 %s 失败（%s，线程占用 %.0f%%）：%s",
                                 name, reason,
                                 100 * _switch_cpu_share(wall_start, cpu_start), exc)
                else:
                    if books and self._search_store is not None:
                        # 落库与本轮命中无关：这次没对上的结果，下次换源还用得上。
                        try:
                            self._search_store.insert(books)
                        except Exception:
                            pass
                    matches.extend(
                        (source, book) for book in books or []
                        if wanted in str(book.name or "").casefold())
                state["completed"] += 1
                publish(state["completed"] == total)

        workers = [asyncio.ensure_future(worker())
                   for _ in range(min(SWITCH_CONCURRENCY, total))]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            # 取消/异常要连带停掉其余 worker，否则它们继续联网并写回已消失的列表。
            for task in workers:
                task.cancel()
            raise
        return self.rank_switch_matches(item, matches)

    @staticmethod
    def rank_switch_matches(item, matches: list[tuple]) -> list[dict]:
        """Legado 换源列表排序：书名对得上最前，其次作者，再次书源优先级。"""
        record = item if isinstance(item, dict) else _book_item_dict(item)
        wanted_name = str(record.get("name") or "").casefold()
        wanted_author = str(record.get("author") or "").casefold()

        def rank(pair):
            source, book = pair
            exact_name = str(book.name or "").casefold() == wanted_name
            same_author = bool(wanted_author) and wanted_author in str(
                book.author or "").casefold()
            return (not exact_name, not same_author,
                    str(getattr(source, "bookSourceUrl", "") or ""),
                    str(getattr(source, "bookSourceName", "") or "").casefold())

        return [{
            "source_url": str(getattr(source, "bookSourceUrl", "") or ""),
            "source_name": str(getattr(source, "bookSourceName", "") or ""),
            "book": _book_item_dict(book),
        } for source, book in sorted(matches, key=rank)]

    async def apply_source_switch(self, record, source, book) -> bool:
        """换源写回：委托旧 SourceService.switch_bookshelf_source。"""
        if self._source_service is None:
            return False
        try:
            return bool(self._source_service.switch_bookshelf_source(
                record, source, book))
        except Exception as exc:
            logger.warning("换源写回失败：%s", exc)
            return False

    # ------------------------------------------------------------------ #
    # 阅读面（§9 门 G3：协议面上的 reader.*）
    #
    # 这一族门面的共同点：**身份全部按 url 显式传进来**。会话态
    # (``_current_source`` / ``_current_book``) 是进程内单例上的属性，跨进程那侧
    # 看不见它，也不该靠"上一次调用留了什么"来猜这一章属于哪本书——那是
    # ``refactor-regression-orphan-methods`` 那一族最难查的形状（服务在跑、方法在、
    # 接线断在隐式状态上）。
    # ------------------------------------------------------------------ #
    async def open_by_url(self, source_url: str, book, shelve: bool = True,
                          resume: bool = True) -> dict:
        """开会话的 JSON 面：回会话快照，**目录不在这一发里**（几千章另说）。

        ``chapters`` 那条走 ``reader.toc``：目录是行列表，投影白名单管得住形状，
        但把 3,000 行挂在"点开一本书"这一发上是 G2 那笔载荷账的原样重演。
        """
        source = await self._source_by_url(source_url)
        session = await self.open_book(source, book, shelve=bool(shelve),
                                       resume=bool(resume))
        position = session.position
        return {
            "book": _book_item_dict(as_book_item(book)),
            "sourceUrl": str(position.source_url or ""),
            "bookUrl": str(position.book_url or ""),
            "chapterCount": len(session.chapters),
            "position": position.as_reading(),
        }

    async def close_by_url(self, source_url: str, book_url: str,
                           save_progress: bool = True) -> dict:
        """结束会话：先收试读收尾要的那条记录，再关（关完 ``_session_key`` 就空了）。

        要求 url 对上才关：会话是单例，若客户端 A 关的是 B 那本书，就会把 A
        自己那一份进度落到别人的行上——而 ``books`` 是 ``ON CONFLICT DO UPDATE``。
        """
        if not self._session_matches(source_url, book_url):
            return {"closed": False, "staged": None}
        staged = self.staged_record()
        await self.close_book(save_progress=bool(save_progress))
        return {"closed": True, "staged": _book_item_dict(staged) if staged else None}

    async def chapter_rows(self, source_url: str, book_url: str,
                           refresh: bool = False) -> list[dict]:
        """目录行：按 url 取书、按 url 取源，返回**纯 dict 行**（投影吃的是行列表）。

        ``book`` 用书架/搜索快照里那条；这里只需要它的 url/name/author 去问目录，
        所以取不到完整快照时按 ``book_url`` 造一条最小 BookItem——引擎按 url 走规则。
        """
        self._require_session(source_url, book_url)
        source = await self._source_by_url(source_url)
        book = self._book_for(source_url, book_url)
        chapters = await self.load_chapters(book, force_refresh=bool(refresh))
        return [_chapter_row(chapter) for chapter in chapters or []]

    async def chapter_document(self, source_url: str, book_url: str,
                               chapter_url: str) -> dict:
        """一章正文的 JSON 面：出**可显示形态**的块，并顺手起一轮预取。

        为什么在服务侧就渲染掉 ``<usehtml>``：那一步会改字数，而章内字符偏移是
        flet 与 Dart 比对翻页落点的尺（见 ``app.core.text_layout`` 抬头）。留到
        客户端各自渲染，两侧数出来的就不是同一串字。

        二进制块不走这一发（设计 §4.4）：值不是 str 时只报 ``binary``，
        内容由 ``/media`` 那条路取——把 JPEG 塞进 JSON 信封会同时炸内存和延迟。

        必须先 ``reader.open``：``load_content`` 的缓存键取自会话上的
        ``(source, book)``，会话不是这本书时键就错了——会把 A 书的正文当成
        B 书的一章去读缓存、去发请求。所以这里显式检查，而不是靠调用顺序。
        """
        self._require_session(source_url, book_url)
        source = await self._source_by_url(source_url)
        book = self._book_for(source_url, book_url)
        chapter = await self._chapter_by_url(book_url, chapter_url, source)
        document = await self.load_content(chapter)
        await self.prefetch_chapters(chapter)
        shown = display_document(document)
        paragraphs = chapter_paragraphs(shown.blocks)
        return {
            "chapterUrl": str(chapter_url or ""),
            "chapterIndex": int(getattr(chapter, "index", 0) or 0),
            "chapterName": str(getattr(chapter, "name", "") or ""),
            "title": str(getattr(shown, "title", "") or ""),
            "imageStyle": str(getattr(shown, "image_style", "") or ""),
            "blocks": [_block_row(block) for block in shown.blocks],
            "paragraphCount": len(paragraphs),
            "totalChars": chapter_text_length(paragraphs),
        }

    async def read_position(self, source_url: str, book_url: str) -> dict | None:
        """续读位置的 JSON 面：没有记录就是 None（不是"位置为 0"）。

        **回的是存档形状**：``books`` 表按 book_url 存 chapter_index/偏移/段号，
        没有 chapter_url 这一列（设计 §3.9 的口径），所以 ``chapter_url`` 照会是
        空串——续读的那一章由 ``chapter_index`` 在目录里定位。这不是缺陷，两侧
        （flet 的 ``restore_position``、Dart 的 ``reader.position.get``）读到的
        是同一份存档，别再各写一套"猜章"的逻辑。
        """
        position = await self.restore_position(self._book_for(source_url, book_url))
        return position.as_reading() if position is not None else None

    async def save_position(self, source_url: str, book_url: str,
                            position_json: dict) -> dict:
        """落一篇阅读位置：客户端报什么段、什么偏移，这里**按章内字符流核一遍**。

        三件事必须在这一发里做完，否则两侧各写各的：

        1. **章按 index 认**：``books`` 不存 chapter_url，而会话的缓存键按 url 走，
           所以落库前把客户端报的 url 换成目录里那一条（index 对不上就退回
           会话当前位置那一章），否则下一次 ``reader.content`` 会读到另一章。
        2. **补得回来的字段补回来**：``save_progress`` 的 name/author 取自会话上的
           book，因此这一发要求会话就是这本书（见 :meth:`_require_session`）——
           否则会把 A 书的名字写到 B 书那一行，而 ``books`` 是
           ``ON CONFLICT DO UPDATE``，写坏是静默的。
        3. **收口客户端的估算**：偏移越界、段号超出段数、负数，都在落库前夹回，
           并回一份落库后的形状（客户端据此知道自己存进去的是什么）。
        """
        self._require_session(source_url, book_url)
        incoming = dict(position_json or {})
        position = ReadingPosition.from_reading(source_url, book_url, incoming)
        position = self._anchored(position)
        position = self._clamped(position)
        await self.save_progress(position.chapter_index, position)
        # 落库的那一份同时写回会话：``reader.close(save_progress=true)`` 落的是
        # **会话上**那份，而跨进程客户端的位置只在这发里出现过。不写回，退出那一发
        # 就把刚存的落点换成会话默认值（chapter 0、偏移 0）——G3 收样本的活臂现场撞到的。
        self._session.position = position
        return position.as_reading()

    def _anchored(self, position: ReadingPosition) -> ReadingPosition:
        """chapter_url 按 chapter_index 从目录里重新取；目录里没有这个 index 就不动。"""
        chapters = self._session.chapters
        index = int(position.chapter_index or 0)
        if not chapters or not 0 <= index < len(chapters):
            return position
        chapter = chapters[index]
        if str(chapter.url or "") == str(position.chapter_url or ""):
            return position
        return ReadingPosition(
            source_url=position.source_url, book_url=position.book_url,
            chapter_url=str(chapter.url or ""), chapter_name=str(chapter.name or ""),
            chapter_index=index, chapter_offset=position.chapter_offset,
            chapter_ratio=position.chapter_ratio,
            character_offset=position.character_offset,
            chapter_paragraph=position.chapter_paragraph,
            font_size=position.font_size, updated_at=position.updated_at)

    def _clamped(self, position: ReadingPosition) -> ReadingPosition:
        """偏移与段号按本章的可显示自然段收口；这一章不在缓存里时**不动值**。

        "不动值"是有意的：客户端可能在翻到一章的很远位置后才落进度，而预取窗口
        只有几章。宁可存一个未核过的偏移，也不要为了核它去联网拉一整章——
        落点读回来时按 :func:`app.core.text_layout.paragraph_at_offset` 还会再收口一次。
        """
        key = (position.source_url, position.book_url, position.chapter_url)
        document = self._prefetch_cache.get(key)
        if document is None and self._cache is not None:
            document = self._cache.load_content(*key)
        if document is None:
            return position
        paragraphs = chapter_paragraphs(display_blocks(document))
        total = chapter_text_length(paragraphs)
        index = position.chapter_paragraph
        if index is not None:
            index = max(0, min(int(index), max(0, len(paragraphs) - 1)))
        return ReadingPosition(
            source_url=position.source_url, book_url=position.book_url,
            chapter_url=position.chapter_url, chapter_name=position.chapter_name,
            chapter_index=position.chapter_index,
            chapter_offset=position.chapter_offset,
            chapter_ratio=position.chapter_ratio,
            character_offset=max(0, min(int(position.character_offset), total)),
            chapter_paragraph=index,
            font_size=position.font_size, updated_at=position.updated_at)

    def _session_matches(self, source_url: str, book_url: str) -> bool:
        """当前会话是不是这一本书（按 ``_session_key`` 那对 url 比）。"""
        current_source, current_book = self._session_key()
        return (str(book_url or "") == current_book
                and (not current_source or not source_url
                     or str(source_url or "") == current_source))

    def _require_session(self, source_url: str, book_url: str) -> None:
        """会话不是这本书就抛：协议面的每个动作都吃 url，隐式状态不能替它猜。"""
        if not self._session_matches(source_url, book_url):
            raise ReaderSessionError(
                f"当前会话不是这本书，先调 reader.open：source={source_url!r} "
                f"book={book_url!r}（会话={self._session_key()!r}）")

    def _book_for(self, source_url: str, book_url: str):
        """当前会话就是这本书则复用会话对象，否则按 url 造一条最小 BookItem。

        不能直接返回 None：``load_chapters`` 与进度落盘都要读 ``book.url``，
        拿到 None 会一路取空串，目录与进度会静默对不上号。
        """
        current = self._current_book
        if (current is not None and str(getattr(current, "url", "") or "") ==
                str(book_url or "")):
            return current
        record = None
        if self._source_service is not None:
            try:
                record = self._source_service.find_bookshelf_item(
                    {"source_url": source_url, "url": book_url})
            except Exception as exc:      # 查不到书架记录不影响按 url 开书
                logger.debug("书架记录反查失败 %s：%s", book_url, exc)
        merged = dict(record or {})
        merged.setdefault("url", str(book_url or ""))
        merged.setdefault("origin", str(source_url or ""))
        return as_book_item(merged)

    async def _chapter_by_url(self, book_url: str, chapter_url: str, source):
        """按 url 找回目录里那一章（会话里的行与客户端传回的 url 同一个来源）。"""
        chapters = await self.load_chapters(self._book_for(
            str(getattr(source, "bookSourceUrl", "") or ""), book_url))
        for chapter in chapters or []:
            if str(chapter.url or "") == str(chapter_url or ""):
                return chapter
        raise ValueError(f"目录里没有这一章：{chapter_url}")


def _chapter_row(chapter) -> dict:
    """目录行的显式表：16 个字段一个个点名（与 ChapterItem 的字段一一对应），
    不靠 dataclass 反射上协议面。

    ``start``/``end``/``*_fragment_id``/``variables`` 是正文切片与逐源变量，
    客户端要拿它们做的是"这一章的 url 带不带片段锚点"，而目录那一屏只读
    名字/序号/是否卷标题/是否 VIP/字数。白名单在 action 表里（``reader.toc``
    的 ``fields``），这里给全是为了 ``reader.toc`` 之外的调用方不丢字段。
    """
    return {
        "name": str(chapter.name or ""),
        "url": str(chapter.url or ""),
        "index": int(chapter.index or 0),
        "isVolume": bool(chapter.is_volume),
        "isVip": bool(chapter.is_vip),
        "isPay": bool(chapter.is_pay),
        "tag": str(chapter.tag or ""),
        "wordCount": str(chapter.word_count or ""),
        "baseUrl": str(chapter.base_url or ""),
        "bookUrl": str(chapter.book_url or ""),
        "resourceUrl": str(chapter.resource_url or ""),
        "start": chapter.start,
        "end": chapter.end,
        "startFragmentId": chapter.start_fragment_id,
        "endFragmentId": chapter.end_fragment_id,
        "variables": dict(chapter.variables or {}),
    }


def _block_row(block) -> dict:
    """正文块上协议面的形状：文本给原文，媒体给地址，二进制只给"我是二进制"。

    ``url`` 与 ``value`` 分两格是有原因的：受保护图在引擎里会被换成字节串好让 flet
    显示（见 ``ContentBlock.url`` 那一格），此时 ``value`` 是 None 而地址还在——
    跨进程那侧（``Image.network``/``media_kit``）用的就是这一格加 ``headers``。
    字节本身按设计 §4.4 不过协议面。
    """
    value = getattr(block, "value", "")
    is_binary = isinstance(value, (bytes, bytearray))
    kind = str(getattr(block, "kind", "") or "")
    raw_url = str(getattr(block, "url", "") or "")
    return {
        "kind": kind,
        "value": None if is_binary else str(value or ""),
        "url": (raw_url or ("" if is_binary or kind == "text" else str(value or ""))),
        "binary": bool(is_binary),
        "title": str(getattr(block, "title", "") or ""),
        "headers": dict(getattr(block, "headers", None) or {}),
    }


#: 搜索范围特殊值（与找书页面的范围选择器一致）。
_SCOPE_ALL = "__all__"
_SCOPE_UNGROUPED = "__ungrouped__"


def as_book_item(book):
    """字典快照 → BookItem；已是对象则原样返回。

    UI 与事件之间流转的一律是字典快照（红线：事件不携带可变对象），而引擎/
    书架侧按属性读取，所以进服务边界必须先还原，否则 getattr 全取空串。
    """
    if not isinstance(book, dict):
        return book
    from dataclasses import fields

    from app.services.engine.rule_engine import BookItem
    known = {f.name for f in fields(BookItem)}
    payload = {key: value for key, value in book.items() if key in known}
    payload.setdefault("name", "")
    return BookItem(**payload)


def _book_item_dict(item) -> dict:
    """BookItem -> 不可变快照字典（事件载荷不得携带可变模型引用）。

    整字段快照而不是挑几个字段：换源/开读要从快照原样还原 BookItem，漏掉的
    ``toc_url``/``last_chapter``/``kind`` 会让目录、漫画通道和书架记录变哑。
    """
    from dataclasses import asdict, is_dataclass

    if item is None:
        return {}
    if is_dataclass(item):
        return asdict(item)
    return {
            "name": getattr(item, "name", "") or "",
            "author": getattr(item, "author", "") or "",
            "url": getattr(item, "url", "") or "",
            "cover_url": getattr(item, "cover_url", "") or "",
            "kind": getattr(item, "kind", "") or "",
            "origin": getattr(item, "origin", "") or "",
            "origin_name": getattr(item, "origin_name", "") or "",
        }
