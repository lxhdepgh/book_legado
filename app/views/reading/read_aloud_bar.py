"""ReadAloudBar：阅读器底部的朗读迷你播放条（设计 §1.1 / §7.2「特殊形态」清单）。

``ReadAloudService`` 早就有 speak/stop/状态事件，但**界面一处都没接**：用户听不到
书，也停不下来。这里补的正是那条缺失的承接面——播放条本身不含业务，状态与章节
由控制器按 :class:`~app.core.events.ReadAloudStateEvent` 写入。

能力边界（照服务侧的实价写，不吹）：

- 系统 TTS 后端**不上报朗读位置**（``ReadAloudStateEvent.position`` 恒为 0），
  所以这里没有进度条、没有逐句高亮，只有"正在朗读哪一章"；
- 朗读按**章**为粒度：放完一章由控制器接下一章（连读开关）；
- 语速档位只对系统 TTS 后端有效（edge / HTTP 后端不接这个参数），所以提示语速
  的那句"仅系统 TTS 后端生效"是**界面自己承认的限制**，不是客套话；念的时候标签
  上也跟着显示实际在用的后端，免得用户对着不出声的 edge 后端找语速旋钮；
- 音色下拉的选项来自 ``tts_profiles`` 表（在 配置 → 更多工具 → 朗读 里登记），
  一项 = 一个 TTS 引擎；``url`` / ``headers`` 只对 HTTP 引擎有意义。选"内置语音"
  表示不用任何一条语音档。

红线（§7.5）：本文件不 import 任何 ``app.services.*``。
"""
from __future__ import annotations

import flet as ft

#: 服务态 → (图标, 提示色)。与 ReadAloudService 的 state 串一致：
#: idle / speaking / error（服务侧只有停，没有暂停态）。
_STATE_META: dict[str, tuple[str, object]] = {
    "idle": (ft.Icons.PLAY_ARROW_OUTLINED, ft.Colors.ON_SURFACE_VARIANT),
    "speaking": (ft.Icons.PAUSE_CIRCLE_OUTLINED, ft.Colors.PRIMARY),
    "error": (ft.Icons.ERROR_OUTLINE, ft.Colors.RED_400),
}
_DEFAULT_META = _STATE_META["idle"]

#: 语速档位 → pyttsx3 的 rate（词/分钟）。档位是给人看的，绝对值只在系统 TTS
#: 后端有意义：edge / HTTP 后端不吃这个参数（服务侧原样透传，不假装支持）。
RATE_PRESETS: dict[str, int] = {
    "slow": 140, "normal": 185, "fast": 240, "very_fast": 300,
}
DEFAULT_RATE = "normal"
#: 音色下拉里"不启用任何语音档"这一项的 key。真实语音档的 key 是数据库 id
#: （纯数字），所以这个哨兵不会撞车；控制器认它等于"没有语音档"。
BUILTIN_VOICE = "builtin"
#: 后端名 → 中文标签（与 tts_profiles.engine_type 同名）。
_BACKEND_LABELS: dict[str, str] = {
    "system": "系统 TTS", "edge": "edge 语音", "http": "书源 HTTP",
}


def backend_label(name: str) -> str:
    return _BACKEND_LABELS.get(str(name or ""), str(name or ""))


class ReadAloudBar(ft.Container):
    """底部朗读条：播放/停止 + 当前章节 + 语速 + 连读下一章开关。"""

    def __init__(self) -> None:
        super().__init__(
            visible=False, padding=ft.Padding(10, 4, 10, 4),
            bgcolor=ft.Colors.SURFACE)

        # -- 回调槽（Controller 填充） --
        self.on_toggle = None        # () -> None（开始/停止朗读本章）
        self.on_stop = None          # () -> None（只停，不切换）
        self.on_follow_changed = None  # (bool) -> None（连读下一章）
        self.on_rate_changed = None    # (str) -> None（语速档位 key）
        self.on_voice_changed = None   # (str) -> None（语音档 id；BUILTIN_VOICE = 不用）

        self._state = "idle"
        self._chapter = ""
        self._detail = ""
        self._backend = ""
        self._voice_name = ""
        self._toggle_btn = ft.IconButton(
            _DEFAULT_META[0], tooltip="朗读本章",
            on_click=lambda e: self._fire(self.on_toggle))
        self._stop_btn = ft.IconButton(
            ft.Icons.STOP, tooltip="停止朗读", disabled=True,
            on_click=lambda e: self._fire(self.on_stop))
        self._label = ft.Text("朗读", size=12, expand=True,
                              color=_DEFAULT_META[1])
        self._voice = ft.Dropdown(
            width=120, dense=True, value=BUILTIN_VOICE,
            tooltip="音色 / 语音档（在 配置 → 更多工具 → 朗读 里登记）",
            options=[ft.dropdown.Option(BUILTIN_VOICE, "内置语音")],
            on_select=lambda e: self._fire(self.on_voice_changed, e.control.value))
        self._rate = ft.Dropdown(
            width=96, dense=True, value=DEFAULT_RATE,
            tooltip="语速（仅系统 TTS 后端生效）",
            options=[ft.dropdown.Option(key, text) for key, text in (
                ("slow", "慢速"), ("normal", "正常"),
                ("fast", "快速"), ("very_fast", "很快"))],
            on_select=lambda e: self._fire(self.on_rate_changed, e.control.value))
        self._follow = ft.Checkbox(
            label="连读下一章", value=True,
            on_change=lambda e: self._fire(self.on_follow_changed, e.control.value))

        self.content = ft.Row(
            [self._toggle_btn, self._stop_btn, self._label,
             self._voice, self._rate, self._follow],
            spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 渲染接口（控制器调用）
    # ------------------------------------------------------------------ #
    def set_state(self, state: str, chapter: str = "", detail: str = "",
                  backend: str = "") -> None:
        """按服务态刷新：图标/提示色跟着走，文字说清"哪一章 + 为什么 + 谁在念"。"""
        self._state = str(state or "idle")
        self._chapter = str(chapter or "")
        self._detail = str(detail or "")
        # 控制器给的是后端引擎名（system/edge/http），翻成中文是这一层的事
        self._backend = backend_label(backend) or self._backend
        icon, color = _STATE_META.get(self._state, _DEFAULT_META)
        self._toggle_btn.icon = icon
        self._toggle_btn.tooltip = "停止朗读" if self._state == "speaking" else "朗读本章"
        self._stop_btn.disabled = self._state != "speaking"
        parts = [p for p in (self._chapter, self._detail) if p]
        if not parts:
            parts = ["未开始朗读" if self._state != "speaking" else "正在朗读"]
        if self._backend and self._state == "speaking":
            parts.append(self._backend)
        self._label.value = " · ".join(parts)
        self._label.color = color

    def set_rate(self, rate: str) -> None:
        """回填语速档位（启动时读偏好；表外的键按"没存过"处理，保持当前值）。"""
        if rate in RATE_PRESETS:
            self._rate.value = rate

    def set_voice_options(self, profiles: list[dict], current: str = "") -> None:
        """回填音色下拉：第一项恒为"内置语音"，其余来自 tts_profiles。

        ``current`` 是语音档 id 的字符串形式；对不上任何一项（比如那条档已被
        删掉）就退回内置语音，而不是留一个下拉里根本不存在的值。
        """
        options = [ft.dropdown.Option(BUILTIN_VOICE, "内置语音")]
        names: dict[str, str] = {BUILTIN_VOICE: ""}
        for profile in profiles or []:
            key = str(profile.get("id", ""))
            if not key:
                continue
            name = str(profile.get("name") or f"语音档 {key}")
            options.append(ft.dropdown.Option(key, name))
            names[key] = name
        self._voice.options = options
        self._voice.value = current if current in names else BUILTIN_VOICE
        self._voice_name = names.get(str(self._voice.value), "")

    @property
    def voice(self) -> str:
        """当前选中的语音档 id；``BUILTIN_VOICE`` 表示不用任何语音档。"""
        return str(self._voice.value or BUILTIN_VOICE)

    @property
    def voice_name(self) -> str:
        return self._voice_name

    @property
    def rate(self) -> str:
        return str(self._rate.value or DEFAULT_RATE)

    @property
    def rate_value(self) -> int:
        """当前档位的绝对语速（表外的值回落到默认档）。"""
        return RATE_PRESETS.get(self.rate, RATE_PRESETS[DEFAULT_RATE])

    @property
    def state(self) -> str:
        return self._state
