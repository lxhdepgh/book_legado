"""ReadAloudBar：阅读器**顶部**的朗读迷你播放条（设计 §1.1 / §7.2「特殊形态」清单）。

第二十九轮把它从窗口底部挪到工具栏正下方（用户："朗读功能放到区域上方，和书内
搜索、设置放在一起"）：它和工具栏那几颗动作键是同一块"阅读时手要够到的控制区"，
沉在底部时离自己的入口有一屏远。收起/展开由 ``ReaderView`` 的工具栏那颗按钮管，
本文件只管这一条里面有什么。

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
    """顶部朗读条：播放/停止 + 当前章节 + 语速 + 连读下一章开关。"""

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
        # 宽度按中文实测：音色下拉 120px 时"内置语音"被截成"内置语"（第二十八轮
        # 截图），下拉框的箭头和左右内边距各吃掉一大截，四个汉字要 150px 才放得下。
        self._voice = ft.Dropdown(
            width=150, dense=True, value=BUILTIN_VOICE,
            tooltip="音色 / 语音档（在 配置 → 更多工具 → 朗读 里登记）",
            options=[ft.dropdown.Option(BUILTIN_VOICE, "内置语音")],
            on_select=lambda e: self._fire(self.on_voice_changed, e.control.value))
        self._rate = ft.Dropdown(
            width=110, dense=True, value=DEFAULT_RATE,
            tooltip="语速（仅系统 TTS 后端生效）",
            options=[ft.dropdown.Option(key, text) for key, text in (
                ("slow", "慢速"), ("normal", "正常"),
                ("fast", "快速"), ("very_fast", "很快"))],
            on_select=lambda e: self._fire(self.on_rate_changed, e.control.value))
        self._follow = ft.Checkbox(
            label="连读下一章", value=True,
            on_change=lambda e: self._fire(self.on_follow_changed, e.control.value))

        # 四颗**平铺**的分栏格子，不是"一行六件"，也不是"两格里套一条 wrap 行"：
        # 一行排完要 ~620px，344 那一档实测把"连读下一章"整只挤出屏幕（第七十七轮像素）。
        # 为什么格子**里面**不能再放 wrap Row：同一位置三臂实测——格子里套 wrap 行，
        # 音色+语速排完就换行，右边还剩 400px 空白；把那条 wrap 换成不换行 Row，三件
        # 立刻排回一行（`out_cell77g_bar_A/B_1200.png`）。而格子本身量到的是 665px，
        # 也就是**该给的宽给了**，是 wrap 行在格子里自己塌了。所以降级只走 col，
        # 控件各占一格，不在格内再造会塌的行。
        # 第一格里那条不换行 Row 可以带 `expand` 的标签（客户端契约：wrap × expand
        # 才画成灰块，第七十六轮量的）。
        # col 的窄档配比**算不出来**，只能读回来：格子宽 = 该档栏宽 × col/12，外面还要
        # 扣朗读条自己的 padding 与栅格间距。三档实测（`.probe/probe_readaloud_cell77.py`：
        # 四格各涂一色、逐列扫跨度，另放 5:7 两块纯色当尺子对照，量得比值 1.405/1.417/1.414
        # 对 1.400 → 读数可信）：
        #   客户区 1184（lg）：474 / 186 / 186 / 282 → **一行**，每格 ≥ 自己那位定宽；
        #   客户区 344（xs） ：304 / 304 / 150＋150 → 三行，全够；
        #   客户区 304（xs）：264 / 264 / 130＋130 → 三行，最紧那格 130 ≥ 语速 110、连读 122。
        # 中途给过 音色 7 / 语速 5：304 那一档语速格量到 **107 < 110**，下拉右端被裁 3px
        # （第七十七轮像素）。所以 xs 档让音色独占一行、语速与连读并排：语速格 107 → 130，
        # 连读格从独占一行（264）让成并排（130），但 130 ≥ 它的 122，**行数一行没多**。
        self.content = ft.ResponsiveRow([
            ft.Container(
                col={"xs": 12, "lg": 5},
                content=ft.Row([self._toggle_btn, self._stop_btn, self._label],
                               spacing=4,
                               vertical_alignment=ft.CrossAxisAlignment.CENTER)),
            ft.Container(col={"xs": 12, "lg": 2}, content=self._voice),
            ft.Container(col={"xs": 6, "lg": 2}, content=self._rate),
            ft.Container(col={"xs": 6, "lg": 3}, content=self._follow),
        ], columns=12, spacing=4, run_spacing=2)

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
