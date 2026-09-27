"""MediaPlayService：视频书播放（M4 内容形态域，设计 §1.4 / §9.2）。

对应 Legado ``VideoPlayService``：把"正文流里嵌一个 video 块"升级为独立的
播放会话——播放后端抽象、章节列表即播放列表、记忆播放位置。

后端抽象（§9.2 ``VideoBackend``）：

- :class:`FletVideoBackend`：flet_video 控件后端，三端默认主路径。**本类只在
  视图层实例化并注入控件**；服务层零 flet import、不持有控件，仅经鸭子类型
  调控件方法（open/play/pause/seek）。测试用 stub 后端替身。
- :class:`SystemPlayerBackend`：降级后端（Windows ``os.startfile`` / macOS
  ``open`` / Linux ``xdg-open``，经 subprocess 调系统命令，平台分支收敛在后端内）。
  仅当 flet_video 不可用时可选，不进主路径（§1.4 跨平台策略）。

播放位置落 ``StorageService.books`` 表：``play_chapter_index``（播放列表里的第
几条）+ ``play_position``（秒，v4 起）。这两列**不碰** ``chapter_index``——那一列
是阅读进度，正文里嵌视频的小说放一条视频不该把"上次读到第几章"改掉（v7 起因）。
播放状态经 :class:`~app.core.events.MediaPlayStateEvent` 推送视频视图。

视频扩展名集合复用 ``reading_engine.py`` 的 ``_MEDIA_EXTENSIONS["video"]``
（import 复用，不改该文件，契约 §6）。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import Any

from app.core.events import MediaPlayStateEvent
from app.core.lifecycle import Service
from app.core.storage import StorageService

# 视频扩展名集合复用 reading_engine（§1.4：import 复用，不改该文件）。
from app.services.engine.reading_engine import _MEDIA_EXTENSIONS

logger = logging.getLogger(__name__)

#: 视频书判定用的扩展名集合（mp4/m3u8/webm/...）。
VIDEO_EXTENSIONS: set[str] = set(_MEDIA_EXTENSIONS.get("video", set()))


def is_video_url(url: str) -> bool:
    """按扩展名判定是否为视频地址（m3u8/mp4/...）。"""
    path = str(url or "").split("?", 1)[0].split("#", 1)[0]
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return ext in VIDEO_EXTENSIONS


class VideoBackend:
    """视频播放后端抽象基类（§9.2）。

    子类约定 ``name`` 与 ``state``（idle/playing/paused/ended/error）。
    服务层只依赖本接口，不感知 flet_video / 系统播放器等具体实现。
    """

    name = "base"

    def __init__(self) -> None:
        self.state = "idle"
        self.available = True
        self.media_url: str = ""

    async def open(self, media_url: str, headers: dict | None = None) -> None:
        """打开一个媒体地址（与 headers 一并交给播放器）。"""
        raise NotImplementedError

    async def play(self) -> None:
        raise NotImplementedError

    async def pause(self) -> None:
        raise NotImplementedError

    async def seek(self, position: float) -> None:
        """跳到 position 秒。"""
        raise NotImplementedError

    async def stop(self) -> None:
        self.state = "idle"

    async def current_position(self) -> float:
        """播放器当前位置（秒）：播放器不上报时返回 0.0。"""
        return 0.0


def _as_seconds(value) -> float:
    """播放器回上来的位置 → 秒。

    真控件给 ``ft.Duration``（认 ``in_seconds``/``seconds``），测试替身给裸秒数。
    认不出来一律算 0：位置只用来"下次接着放"，报错不如从头放，别把声音打断。
    """
    for attr in ("in_seconds", "seconds"):
        number = getattr(value, attr, None)
        if isinstance(number, (int, float)):
            return float(number)
    return float(value) if isinstance(value, (int, float)) else 0.0


class FletVideoBackend(VideoBackend):
    """flet_video 控件后端（三端主路径）。

    ⚠️ 本类**只在视图层实例化**：视图层创建 flet_video 控件后注入 ``control``，
    服务层既不 import flet、也不构造本类。这里只用鸭子类型调控件的
    ``playlist``/``play()``/``pause()``/``seek()``，保持服务零 flet 依赖。

    两个工厂参数是这条红线的代价，也是两处实错的修正：

    - ``media_factory(url, headers) -> VideoMedia``：0.86 的 ``Video`` **没有
      ``source`` 字段**，往控件上写 ``control.source = url`` 不会下发给客户端，
      换章等于永远停在第一条视频。换片只能改 ``playlist``，而 ``VideoMedia``
      是 flet_video 的类型，服务层造不出来，只能由视图层递进来。
    - ``duration_factory(seconds) -> DurationValue``：``seek`` 收的是
      ``Union[Duration, int]``，而 ``Duration`` 上线是**微秒**整数
      （``flet/messaging/protocol.py`` 的 ``ExtType(3, in_microseconds)``）。
      秒→Duration 的换算要 import flet，所以同样交给视图层；没注入就跳过跳转，
      **不猜单位**——猜错的表现是"接着上次的地方放，结果跳到了片头几毫秒"。

    第三个参数 ``ui_marshal(coroutine) -> awaitable`` 管的是**跑在哪条 loop 上**：
    消费方拿到的是 EngineProxy，服务方法实际在 EngineHub 的 loop 上执行，而
    flet 控件只认界面循环（同一约定见 reading_engine 的 ``_call_ui``）。装配方
    （控制器）把每一步控件操作编组回界面循环并等结果；没注入时就地 await
    （无头测试与"本来就在界面循环上"的调用方不需要它）。
    """

    name = "flet"

    def __init__(self, control: Any = None, media_factory=None,
                 duration_factory=None, ui_marshal=None) -> None:
        super().__init__()
        self._control = control  # flet_video 控件（视图层注入），服务层不感知其类型
        self._media_factory = media_factory
        self._duration_factory = duration_factory
        self._ui_marshal = ui_marshal
        self._intent_with_load = False   # 本次 play() 是否已由换源那条消息带过
        self.available = control is not None

    async def _on_ui(self, step, *args):
        """把一步控件操作挪到界面循环上执行并等它出结果（未注入则就地执行）。"""
        coroutine = step(*args)
        if self._ui_marshal is None:
            return await coroutine
        return await self._ui_marshal(coroutine)

    async def _call(self, method: str, *args):
        """在控件所在的那条 loop 上调一个方法（协程/同步都接）。"""
        target = getattr(self._control, method, None)
        if target is None:
            return None
        result = target(*args)
        return await result if hasattr(result, "__await__") else result

    async def open(self, media_url: str, headers: dict | None = None) -> None:
        self.media_url = str(media_url or "")
        if self._control is None:
            raise RuntimeError("FletVideoBackend 未注入 flet_video 控件（须在视图层装配）")
        if self._media_factory is None:
            raise RuntimeError("FletVideoBackend 未注入 media_factory（VideoMedia 只能视图层造）")
        media = self._media_factory(self.media_url, dict(headers or {}))
        await self._on_ui(self._load, media)
        self.state = "playing"
        self._intent_with_load = True

    async def _load(self, media):
        """换源与"要播"**同一条消息**下发。

        分两步（先 playlist 再 play()）在 web 面上必炸：play() 撞上正在起的 load，
        客户端回 `The element has no supported sources.` /
        `The play() request was interrupted by a new load request.`，面板停在 error、
        位置一直是 0（`.probe/probe_ftv_play.py` 三种发法实测：只有这条不发 play() 的
        与"等 get_duration 再 play()"两种真在放）。
        """
        self._control.playlist = [media]
        self._control.autoplay = True
        return None

    async def play(self) -> None:
        self.state = "playing"
        if self._intent_with_load:
            # 播放意图刚随换源下发过，再补一发 play() 就是上面那次竞态本身。
            self._intent_with_load = False
            return
        await self._on_ui(self._call, "play")

    async def pause(self) -> None:
        self.state = "paused"
        self._intent_with_load = False   # 按过暂停之后，play() 得真发出去
        await self._on_ui(self._call, "pause")

    async def stop(self) -> None:
        """叫停：状态归 idle，控件上落的却是 ``pause()`` 而不是 ``stop()``。

        基类只改状态的话，用户按了"停止"画面还在放、声音还在，所以叫停必须真下发一步。
        不发控件那个 ``stop()``：web 面实测它会把 ``<video>`` 从文档里摘掉，**这个
        播放面就此废掉**——之后再换片，假源照样被要、位置一动不动，并回一发
        "The play() request was interrupted because the media was removed from the
        document."（``.probe/probe_stop_recovery.py`` 三种救法实测：只有"停止用 pause"
        和"stop 后重建控件"能再放起来，重建得视图层配合，不值）。
        位置在叫停前已由服务侧读走（``stop_playback``），重播走 ``open()`` 换片，
        实测从片头正常起播。
        """
        self.state = "idle"
        self._intent_with_load = False
        await self._on_ui(self._call, "pause")

    async def seek(self, position: float) -> None:
        if self._control is None or self._duration_factory is None:
            logger.debug("未注入 duration_factory，跳过 seek(%s)", position)
            return
        # Duration 由视图层工厂造（要 import flet），换算好再上控件。
        value = self._duration_factory(float(position))
        await self._on_ui(self._call, "seek", value)

    async def current_position(self) -> float:
        if self._control is None or not hasattr(self._control, "get_current_position"):
            return 0.0
        return _as_seconds(await self._on_ui(self._call, "get_current_position"))


class SystemPlayerBackend(VideoBackend):
    """系统默认播放器降级后端（os.startfile / open / xdg-open）。

    仅当 flet_video 在某平台不可用时可选，不进主路径（§1.4）。平台分支收敛在
    本后端内：Windows 用 ``os.startfile``，macOS/Linux 经 subprocess 调系统命令。
    """

    name = "system"

    async def open(self, media_url: str, headers: dict | None = None) -> None:
        self.media_url = str(media_url or "")
        url = self.media_url
        try:
            if sys.platform.startswith("win"):
                os.startfile(url)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", url])
            else:
                subprocess.Popen(["xdg-open", url])
        except Exception as exc:  # 无图形环境/无关联播放器：降级为 error
            self.state = "error"
            logger.warning("系统播放器打开失败：%s", exc)
            raise
        self.state = "playing"

    async def play(self) -> None:
        self.state = "playing"

    async def pause(self) -> None:
        self.state = "paused"

    async def seek(self, position: float) -> None:
        # 系统默认播放器不暴露跳转句柄，降级后端忽略 seek（位置仍落库）。
        logger.debug("SystemPlayerBackend 不支持 seek，忽略 position=%s", position)


class MediaPlayService(Service):
    """视频书播放服务：播放列表管理 + 状态机 + 位置落库。"""

    name = "MediaPlayService"
    dependencies: tuple[type[Service], ...] = (StorageService,)

    def __init__(self, storage=None, backend: VideoBackend | None = None,
                 rule_engine=None) -> None:
        super().__init__()
        self._storage = storage
        self._backend = backend  # 测试注入 stub；未注入时默认降级系统播放器
        self._rule_engine = rule_engine
        # 当前播放会话状态。
        self._book_url = ""
        self._chapters: list[Any] = []
        self._index = -1
        self._position = 0.0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            from app.core.storage import StorageService
            self._storage = self.hub.require(StorageService)
        if self._backend is None:
            # 主路径 flet_video 由视图层在打开视频页时经 set_backend() 注入；
            # 服务层默认装配降级系统播放器，保证无 flet 环境也能打开。
            self._backend = SystemPlayerBackend()
        logger.info("MediaPlayService 已启动，后端=%s", self._backend.name)

    async def on_stop(self) -> None:
        try:
            # 生命周期收尾不去问播放器位置：那时控件多半已随窗口销毁，等一次
            # invoke 往返只会在退出路上加等待。位置由界面侧的 stop 记。
            await self.stop_playback(remember_position=False)
        except Exception:  # pragma: no cover
            logger.debug("MediaPlayService stop_playback 异常")
        self._chapters = []
        self._index = -1

    def set_backend(self, backend: VideoBackend) -> None:
        """视图层注入实际播放后端（FletVideoBackend 在视图层实例化后传入）。"""
        self._backend = backend

    # ------------------------------------------------------------------ #
    # 播放会话
    # ------------------------------------------------------------------ #
    async def open_book(self, book_url: str, chapters: list[Any]) -> tuple[int, float]:
        """打开视频书：章节列表即播放列表，恢复上次播放位置。

        ``chapters`` 为章节对象/字典列表（取 ``url``/``name``/``index``）。
        返回 (恢复的章节索引, 恢复的播放位置秒)。
        """
        self._book_url = str(book_url or "")
        self._chapters = list(chapters or [])
        index, position = await self._restore_position(self._book_url)
        self._index = index if 0 <= index < len(self._chapters) else 0
        self._position = position
        self._publish_state("idle")
        return self._index, self._position

    async def play_chapter(self, index: int, *, resume: bool = False) -> None:
        """播放播放列表第 index 章。

        ``resume=True`` 接着 :meth:`open_book` 记下的位置跳过去（"上次没看完那章"），
        否则从片头开始。跳转放在 open+play **之后**：媒体还没换上就 seek，播放器
        要么忽略要么跳到上一条视频的中间。
        """
        if not (0 <= index < len(self._chapters)):
            raise IndexError(f"播放列表越界：{index}/{len(self._chapters)}")
        self._index = index
        chapter = self._chapters[index]
        media_url = self._chapter_url(chapter)
        headers = self._chapter_headers(chapter)
        backend = self._require_backend()
        await backend.open(media_url, headers)
        await backend.play()
        start_at = self._position if resume else 0.0
        if start_at > 0:
            await backend.seek(start_at)
        self._position = start_at
        await self._save_position(index, start_at)
        self._publish_state("playing")

    async def pause(self) -> None:
        await self._require_backend().pause()
        await self._remember_position()
        self._publish_state("paused")

    async def resume_playback(self) -> None:
        """继续播放当前章。

        名字带 `_playback` 是硬要求：`resume()` 会**盖掉** Service 基类的生命周期
        `resume()`，届时 hub 逆序停止/仪表盘的「恢复」都落到播放面上，
        服务永远停不掉（状态卡在 RUNNING）。见 tests/test_core_lifecycle.py 的护栏。
        """
        await self._require_backend().play()
        self._publish_state("playing")

    async def seek(self, position: float) -> None:
        """跳到 position 秒，并立即落库。"""
        position = max(0.0, float(position))
        await self._require_backend().seek(position)
        self._position = position
        await self._save_position(self._index, position)
        self._publish_state("playing")

    async def mark_ended(self) -> None:
        """播放器上报"这一条放完了"：状态归 ended，**位置清 0** 而不是记到片尾。

        记到片尾的话，下次「接着上次看」一进去就立刻到底，看起来像播放器坏了。
        叫 ``mark_ended`` 不叫 ``ended``：读起来像状态谓词，而它是状态**变更**。
        """
        self._position = 0.0
        await self._save_position(self._index, 0.0)
        self._publish_state("ended")

    async def stop_playback(self, *, remember_position: bool = True) -> None:
        """收起播放（后端 stop + 状态归 idle）。

        同理不能叫 `stop()`：那会盖掉基类的生命周期 `stop()`，hub 逆序停止时
        这个服务永远停不掉。

        ``remember_position``：位置在叫停**之前**读——播放器 stop 之后多半报 0，
        那样"接着看"就永远回到片头。生命周期收尾（``on_stop``）走 False，见那里。
        """
        if self._backend is not None:
            if remember_position:
                await self._remember_position()
            try:
                await self._backend.stop()
            except Exception:  # pragma: no cover
                logger.debug("播放后端 stop 异常")
        self._publish_state("idle")

    async def _remember_position(self) -> None:
        """问播放器要一次当前位置并落库；播放器不给（或报错）就维持已知值。"""
        getter = getattr(self._backend, "current_position", None)
        if getter is None:
            return
        try:
            position = float(await getter())
        except Exception:
            logger.debug("播放后端 current_position 异常")
            return
        if position <= 0:
            return
        self._position = position
        await self._save_position(self._index, position)

    # ------------------------------------------------------------------ #
    # 播放位置落库（books 表 play_chapter_index + play_position）
    # ------------------------------------------------------------------ #
    async def _save_position(self, index: int, position: float) -> None:
        conn = self._storage.db if self._storage is not None else None
        if conn is None or not self._book_url:
            return
        now = time.time()
        conn.execute(
            "INSERT INTO books(book_url, play_chapter_index, play_position,"
            " updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(book_url) DO UPDATE SET "
            " play_chapter_index=excluded.play_chapter_index,"
            " play_position=excluded.play_position,"
            " updated_at=excluded.updated_at",
            (self._book_url, int(index), float(position), now))

    async def _restore_position(self, book_url: str) -> tuple[int, float]:
        conn = self._storage.db if self._storage is not None else None
        if conn is None or not book_url:
            return 0, 0.0
        row = conn.execute(
            "SELECT play_chapter_index, play_position FROM books WHERE book_url=?",
            (book_url,)).fetchone()
        if row is None:
            return 0, 0.0
        return int(row[0] or 0), float(row[1] or 0.0)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _require_backend(self) -> VideoBackend:
        if self._backend is None:
            raise RuntimeError("MediaPlayService 未装配播放后端")
        return self._backend

    @staticmethod
    def _chapter_url(chapter: Any) -> str:
        if isinstance(chapter, dict):
            return str(chapter.get("url") or chapter.get("chapterUrl") or "")
        return str(getattr(chapter, "url", "") or getattr(chapter, "chapterUrl", "") or "")

    @staticmethod
    def _chapter_headers(chapter: Any) -> dict:
        if isinstance(chapter, dict):
            return dict(chapter.get("headers") or {})
        return dict(getattr(chapter, "headers", None) or {})

    def _publish_state(self, state: str) -> None:
        if self.bus is None:
            return
        self.bus.publish(MediaPlayStateEvent(
            state=state, book_url=self._book_url, chapter=self._index))
