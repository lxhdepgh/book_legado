"""TtsProfileView：听书语音档管理页（设计 §5.9 / §1.1）。

``ReadAloudService`` 的 ``list_profiles`` / ``add_profile`` / ``remove_profile``
建好后**零调用方**（表 ``tts_profiles`` 一直是空的），所以朗读条上的音色下拉
永远只有"内置语音"一项——听书功能看着有，实际上只有系统默认嗓音一条路。
本页补的就是这条缺失的承接面：语音档清单 + 新建对话框 + 删除 + 默认档切换。

一条语音档 = 一个 TTS 引擎的接法（对齐 Legado 的 tts 引擎表）：

- ``engine_type``：走哪个后端（system / edge / http），服务侧不可用时自动回退；
- ``url`` + ``headers``：**只有 http 后端用**（含 ``{text}`` 占位的音频地址模板）；
  edge 的嗓音名、系统的语速都不在这张表里（表结构 v4 就没有这一列）。

红线（§7.5）：本文件不 import 任何 ``app.services.*``；数据与动作经控制器。
"""
from __future__ import annotations

import flet as ft

#: engine_type → 中文标签（与 read_aloud_bar 同一套叫法）。
ENGINE_LABELS: dict[str, str] = {
    "system": "系统 TTS", "edge": "edge 语音", "http": "书源 HTTP",
}


def engine_label(engine_type: str) -> str:
    return ENGINE_LABELS.get(str(engine_type or ""), str(engine_type or ""))


class TtsProfileView(ft.Column):
    """语音档清单：新增 / 删除 / 设为默认。"""

    def __init__(self) -> None:
        super().__init__(spacing=8, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_add = None          # () -> None（打开新建对话框）
        self.on_delete = None       # (profile_id) -> None
        self.on_set_default = None  # (profile_id) -> None

        self._title = ft.Text("朗读语音档", size=20, weight=ft.FontWeight.BOLD)
        self._hint = ft.Text(
            "语音档决定朗读用哪个引擎：系统 TTS / edge 语音 / 书源 HTTP-TTS（要填音频地址模板）。",
            size=12, color=ft.Colors.GREY_700)
        self._add_btn = ft.FilledButton(
            "新建语音档", icon=ft.Icons.ADD,
            on_click=lambda e: self._fire(self.on_add))
        self._status = ft.Text("", size=12, color=ft.Colors.GREY_600)
        self._rows = ft.ListView(expand=True, spacing=2)

        self.controls = [
            self._title,
            self._hint,
            self._add_btn,
            ft.Container(expand=True, content=self._rows),
            self._status,
        ]

    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    def set_status(self, text: str) -> None:
        self._status.value = text or ""

    def render_profiles(self, profiles: list[dict], default_id: str = "") -> None:
        """渲染语音档行：名字 + 引擎 + 地址；点行设为默认，尾部删除。

        ``default_id`` 是当前生效档（``reader.tts_profile``）；"" 表示用内置语音，
        所以每一行都不得不用点击来取消默认——清空由朗读条下拉选"内置语音"做。
        """
        rows = []
        for profile in profiles or []:
            pid = str(profile.get("id", ""))
            name = str(profile.get("name") or "(未命名)")
            engine = engine_label(str(profile.get("engine_type") or "system"))
            url = str(profile.get("url") or "")
            subtitle = f"{engine}" + (f" · {url}" if url else "")
            rows.append(ft.ListTile(
                key=pid or None,
                selected=(bool(default_id) and pid == default_id),
                title=ft.Text(name, weight=ft.FontWeight.W_500),
                subtitle=ft.Text(subtitle, size=12, color=ft.Colors.GREY_700,
                                 max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                trailing=ft.IconButton(
                    ft.Icons.DELETE_OUTLINE, tooltip="删除",
                    on_click=lambda e, i=pid: self._fire(self.on_delete, i)),
                on_click=lambda e, i=pid: self._fire(self.on_set_default, i)))
        self._rows.controls = rows
