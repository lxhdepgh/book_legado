"""VideoPlayBar：阅读器里的视频书面板（设计 §1.4 / §9.2）。

``MediaPlayService`` 早就有播放列表、状态机、位置落库，但**界面一处都没接**：
正文里的视频块由本视图直接 ``import flet_video`` 自建控件（绕过服务），于是
没有播放列表、没有"接着上次看"、切页也停不下来。这一条把缺失的承接面补上：
一个播放器 + 播控 + 播放列表，动作全部经回调交给控制器，状态由控制器按
:class:`~app.core.events.MediaPlayStateEvent` 写回来。

形态上的三个决定（都是"为什么不内嵌"的答案）：

- **一个播放面**：正文里的视频块渲染成「在本页播放」条目（见
  ``ReaderView._video_control``），播放器只有这一个。两处各自放一个 ``ftv.Video``
  的话，"正在放的是哪一条"就没人说得清了；
- **播放器懒建**：``flet_video`` 是原生插件，读小说的人不该在打开阅读器时就付它
  的装配代价，第一次真播才建（:meth:`ensure_player`）；建不出来就明说没装组件，
  **不悄悄把视频丢给系统默认播放器**（那等于凭空开一个外部窗口）；
- **没有进度条**：位置只在做"下次接着看"时用（服务侧读 :meth:`current_position`
  落库），面板上不画进度条——画一条不会自己走的条比没有更骗人。

红线（§7.5）：本文件不 import 任何 ``app.services.*``。``flet_video`` 只在这里 import
（服务层的 ``FletVideoBackend`` 靠 :meth:`make_video_media` / :meth:`video_duration`
两个工厂拿类型，见 media_play.py 的类注释）。
"""
from __future__ import annotations

from typing import Any

import flet as ft

#: 服务态 → (图标, 提示色, 切换按钮的提示)。与 MediaPlayService 的 state 串一致：
#: idle / playing / paused / ended / error。
_STATE_META: dict[str, tuple[str, object, str]] = {
    "idle": (ft.Icons.PLAY_ARROW_OUTLINED, ft.Colors.ON_SURFACE_VARIANT, "播放"),
    "playing": (ft.Icons.PAUSE_OUTLINED, ft.Colors.PRIMARY, "暂停"),
    "paused": (ft.Icons.PLAY_ARROW_OUTLINED, ft.Colors.PRIMARY, "继续"),
    "ended": (ft.Icons.REPLAY, ft.Colors.ON_SURFACE_VARIANT, "重播"),
    "error": (ft.Icons.ERROR_OUTLINE, ft.Colors.RED_400, "重试"),
}
_DEFAULT_META = _STATE_META["idle"]

#: ``on_complete`` 里"这一条确实放完了"的取值。插件上线的是字符串（``"True"``/``"False"``），
#: 单元测里递的是真 ``bool``，两种都认。
_COMPLETED_VALUES = {"true", "1", "yes"}


def _as_completed(data) -> bool:
    """``data=True`` → 真放完了；``None``/``False``/``"False"`` → 只是"重新开始了"。"""
    if isinstance(data, bool):
        return data
    return str(data or "").strip().lower() in _COMPLETED_VALUES


class VideoPlayBar(ft.Container):
    """视频面板：播放器 + 上一章/播放暂停/下一章/停止 + 播放列表入口。"""

    def __init__(self) -> None:
        super().__init__(
            visible=False, padding=ft.Padding(10, 6, 10, 6),
            bgcolor=ft.Colors.SURFACE_CONTAINER)

        # -- 回调槽（Controller 填充） --
        self.on_play_index = None       # (index: int) -> None（播放列表里选一条）
        self.on_toggle = None           # () -> None（播放/暂停当前条）
        self.on_stop = None             # () -> None（停止，保留面板）
        self.on_prev = None             # () -> None
        self.on_next = None             # () -> None
        self.on_close = None            # () -> None（收起面板：控制器负责叫播放器停）
        self.on_open_playlist = None    # () -> None（播放列表弹层）
        #: 播放器自己上报的两条：客户端放完了 / 放坏了。回调由 flet 的事件派发线程
        #: 送进来，一律原样转给控制器——面板不判断"该不该连播"。
        self.on_ended = None            # () -> None（on_complete：当前条放完）
        self.on_failed = None           # (detail: str) -> None（on_error：放坏了）

        self._state = "idle"
        self._items: list[dict] = []
        self._current = -1
        #: 懒建的播放器控件（第一次真播才有；建不出来是 None，面板报没装组件）。
        self._player: Any = None
        self._player_failed = ""

        self._title = ft.Text("视频", size=13, weight=ft.FontWeight.W_600,
                              expand=True, overflow=ft.TextOverflow.ELLIPSIS)
        self._state_label = ft.Text("", size=11,
                                    color=_DEFAULT_META[1])
        self._toggle_btn = ft.IconButton(
            _DEFAULT_META[0], tooltip=_DEFAULT_META[2],
            on_click=lambda e: self._fire(self.on_toggle))
        self._stop_btn = ft.IconButton(
            ft.Icons.STOP, tooltip="停止播放", disabled=True,
            on_click=lambda e: self._fire(self.on_stop))
        self._prev_btn = ft.IconButton(
            ft.Icons.SKIP_PREVIOUS, tooltip="上一个", disabled=True,
            on_click=lambda e: self._fire(self.on_prev))
        self._next_btn = ft.IconButton(
            ft.Icons.SKIP_NEXT, tooltip="下一个", disabled=True,
            on_click=lambda e: self._fire(self.on_next))
        self._player_host = ft.Container(visible=False, height=260,
                                         alignment=ft.Alignment.CENTER)

        self.content = ft.Column([
            ft.Row([
                ft.Icon(ft.Icons.MOVIE_OUTLINED, size=18),
                self._title,
                self._state_label,
                ft.IconButton(ft.Icons.PLAYLIST_PLAY, tooltip="播放列表",
                              on_click=lambda e: self._fire(self.on_open_playlist)),
                ft.IconButton(ft.Icons.CLOSE, icon_size=18, tooltip="收起播放",
                              on_click=lambda e: self._fire(self.on_close)),
            ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            self._player_host,
            ft.Row([self._prev_btn, self._toggle_btn, self._next_btn,
                    self._stop_btn], spacing=0),
        ], spacing=4)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 播放器装配（控制器在派发播放动作之前调）
    # ------------------------------------------------------------------ #
    def ensure_player(self) -> bool:
        """建出播放器控件；建不出来返回 False（并把原因写到面板上）。"""
        if self._player is not None:
            return True
        try:
            import flet_video as ftv
        except Exception as exc:
            self._player_failed = f"本机没有可用的视频播放组件：{exc}"
            self._player_host.visible = True
            self._player_host.content = ft.Text(
                self._player_failed, size=12, color=ft.Colors.RED_400)
            return False
        self._player = ftv.Video(
            playlist=[], autoplay=False, aspect_ratio=16 / 9,
            controls=ftv.AdaptiveVideoControls(),
            on_complete=self._completed_handler,
            on_error=self._error_handler)
        self._player_host.visible = True
        self._player_host.content = self._player
        return True

    # ------------------------------------------------------------------ #
    # 播放器事件甄别（实测出来的两条伪事件，见各自主释）
    # ------------------------------------------------------------------ #
    def _completed_handler(self, event=None) -> None:
        """``on_complete`` 只在**真放到片尾**时才算数。

        0.86 的 web 面在建控件后约 40 毫秒就发过一发 ``data=False`` 的 completed
        （换源时 media_kit 也会补 ``completed=false`` 表示"重新开始了"），
        而 ``data=True`` 才是"这一条放完了"（实测 6 秒假片：``False`` 在 +0.04s、
        ``True`` 在 +6.03s，见 ``.probe/probe_complete_source.py``）。照单全收的话，
        连播会在起播那一瞬间把整本翻到最后一页。
        """
        if not _as_completed(getattr(event, "data", None)):
            return
        callback = self.on_ended
        if callback is not None:
            callback()

    def _error_handler(self, event=None) -> None:
        """``on_error`` 丢掉"空 src"那条：它是**建控件自带**的，不是用户那本坏了。

        控件一上来没有片可放，web 面的 ``<video>`` 就回
        ``MEDIA_ELEMENT_ERROR: Empty src attribute``——同一条探针里两个形状的控件
        （传 ``playlist=[]`` 与不传）都在 +0.04s 报过它。此时控制器刚把状态写成
        playing，面板会凭空红成"播放失败"。真坏源报的是别的话（"no supported sources"、
        ``FFmpegDemuxer``），照原样递上去。
        """
        detail = str(getattr(event, "data", "") or "").strip()
        if "empty src" in detail.lower():
            return
        callback = self.on_failed
        if callback is not None:
            callback(detail)

    @property
    def player_control(self):
        return self._player

    @property
    def player_error(self) -> str:
        """``ensure_player()`` 建不出来时的原话（空串＝没建过/建好了）。"""
        return self._player_failed

    @staticmethod
    def make_video_media(url: str, headers: dict):
        """``VideoMedia`` 工厂：服务层的后端拿不到 flet_video 的类型，由这里递。"""
        import flet_video as ftv
        return ftv.VideoMedia(resource=str(url or ""),
                              http_headers=dict(headers or {}) or None)

    @staticmethod
    def video_duration(seconds: float):
        """秒 → ``ft.Duration``。

        ``seek`` 收的是 ``Union[Duration, int]``，而 Duration 上线是**微秒**整数，
        所以不能把秒数裸传过去（表现是"接着上次看"却跳到片头几毫秒）。
        """
        return ft.Duration(seconds=int(float(seconds)))

    # ------------------------------------------------------------------ #
    # 渲染接口（控制器调用）
    # ------------------------------------------------------------------ #
    def set_state(self, state: str, chapter: str = "", detail: str = "") -> None:
        """按服务态刷新：图标/颜色跟着走，文字说清"在放哪一条 + 为什么"。"""
        self._state = str(state or "idle")
        icon, color, toggle_tip = _STATE_META.get(self._state, _DEFAULT_META)
        self._toggle_btn.icon = icon
        self._toggle_btn.tooltip = toggle_tip
        self._stop_btn.disabled = self._state not in {"playing", "paused"}
        parts = [p for p in (chapter, detail) if p]
        self._state_label.value = " · ".join(parts)
        self._state_label.color = color

    def set_playlist(self, items: list[dict], current: int = -1) -> None:
        """回填播放列表与当前条：``items`` 每项 ``{url,title}``。

        上下曲按钮的可用性在这里算——列表只有一条时按"下一个"会越界，
        与其点了报错，不如一开始就是灰的。
        """
        self._items = [dict(item) for item in items or []]
        self._current = current if 0 <= current < len(self._items) else -1
        self._title.value = (self._items[self._current].get("title")
                             or f"视频 {self._current + 1}") if self._current >= 0 \
            else "未选择视频"
        self._prev_btn.disabled = self._current <= 0
        self._next_btn.disabled = not (0 <= self._current < len(self._items) - 1)

    def build_playlist_dialog(self, on_pick, on_close) -> ft.Control:
        """播放列表弹层：控件由视图搭，show/pop 归控制器（§7.2）。

        高度按条数收（一屏放不下才封顶到 320 起滚动）：钉死 320 的话，两集的
        书下面挂一大片空白，界面探针截图里看着像弹层坏了。
        """
        rows = [ft.ListTile(
            title=ft.Text(str(item.get("title") or item.get("url") or f"视频 {n + 1}"),
                          overflow=ft.TextOverflow.ELLIPSIS),
            leading=ft.Icon(ft.Icons.PLAY_ARROW if n == self._current
                            else ft.Icons.MOVIE_OUTLINED),
            selected=n == self._current,
            data=n,
            on_click=lambda e, index=n: on_pick(index))
            for n, item in enumerate(self._items)]
        return ft.AlertDialog(
            title=ft.Text(f"播放列表（{len(rows)} 条）"),
            content=ft.Column(rows or
                              [ft.Text("这一本还没有读到带视频的章节",
                                       color=ft.Colors.GREY_500)],
                              tight=True, scroll=ft.ScrollMode.AUTO,
                              height=min(320, 64 * max(1, len(rows)) + 16),
                              width=420),
            actions=[ft.TextButton("关闭", on_click=lambda e: on_close())])

    # ------------------------------------------------------------------ #
    @property
    def items(self) -> list[dict]:
        return [dict(item) for item in self._items]

    @property
    def current(self) -> int:
        return self._current

    @property
    def state(self) -> str:
        return self._state
