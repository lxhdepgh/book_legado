"""TtsProfileController：听书语音档管理页编排（设计 §5.9）。

三件事：清单（``ReadAloudService.list_profiles``）、新建（``add_profile``）、
删除（``remove_profile``），外加"点一行设为默认"——默认档落在偏好键
``reader.tts_profile``，与阅读器朗读条上的音色下拉**同一个键**（键名不同名
就是又一次 font.family / reader.font_family 式的静默断链）。

对话框里的字段按后端实价给：``url`` / ``headers`` 只有 http 后端会读，
所以引擎选 edge/system 时它们留空也无所谓，选 http 时缺一不可——服务侧不校验，
这里不挡就等于存一条永远发不出声的档。
"""
from __future__ import annotations

import json
import logging

import flet as ft

from app.controllers.base import BaseController
from app.services.content.read_aloud import ReadAloudService
from app.services.reading.reading_service import ReadingService

logger = logging.getLogger(__name__)

#: 引擎类型 → 中文标签（与 tts_view / 朗读条同一套叫法）。
ENGINE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("system", "系统 TTS"), ("edge", "edge 语音"), ("http", "书源 HTTP-TTS"),
)
ENGINE_KEYS = tuple(key for key, _label in ENGINE_OPTIONS)


class TtsProfileController(BaseController):
    """语音档 CRUD + 默认档落库。"""

    destination = "tools.tts"

    def on_attach(self) -> None:
        self._tts = self._safe_service(ReadAloudService)
        self._reading = self._safe_service(ReadingService)
        self._profiles: list[dict] = []
        self._default_id = ""
        self._fields: dict = {}

        self._view.on_add = self.show_editor
        self._view.on_delete = self.delete_profile
        self._view.on_set_default = self.set_default
        self.load()

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 清单
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        if self._tts is None:
            self._view.set_status("ReadAloudService 未就绪，无法管理语音档")
            return
        self.ui_call(self._load_async())

    async def _load_async(self) -> None:
        profiles = await self._tts.list_profiles()
        default = ""
        if self._reading is not None:
            default = str(await self._reading.get_reader_preference(
                "tts_profile", "") or "")
        if self._view is None:    # 两次 await 之间页可能已经切走
            return
        self._profiles = list(profiles or [])
        self._default_id = default
        self._view.render_profiles(self._profiles, default_id=default)
        self._view.set_status(self._status_text())

    def _status_text(self) -> str:
        if not self._profiles:
            return ("还没有语音档：朗读只用系统默认的嗓音。"
                    "HTTP-TTS 书源可以在这里登记成一条语音档。")
        chosen = self._find(self._default_id)
        tail = (f"，当前默认：{chosen.get('name')}" if chosen
                else "，当前默认：内置语音（在阅读器朗读条上切）")
        return f"共 {len(self._profiles)} 条语音档{tail}。点一行设为默认。"

    def _find(self, profile_id: str) -> dict | None:
        for profile in self._profiles:
            if str(profile.get("id", "")) == str(profile_id or ""):
                return profile
        return None

    # ------------------------------------------------------------------ #
    # 新建 / 删除
    # ------------------------------------------------------------------ #
    def show_editor(self) -> None:
        if self._tts is None:
            self._view.set_status("ReadAloudService 未就绪，无法新建语音档")
            return
        page = self._page()
        if page is None:
            self._view.set_status("界面未就绪，无法打开新建框")
            return
        self._fields = {
            "name": ft.TextField(label="名称", hint_text="例如：书源自带 TTS"),
            "engine_type": ft.Dropdown(
                label="引擎", value="system", width=220,
                options=[ft.dropdown.Option(key, text)
                         for key, text in ENGINE_OPTIONS]),
            "url": ft.TextField(
                label="音频地址模板（仅 HTTP 引擎）",
                hint_text="https://tts.example.com/read?text={text}"),
            "headers": ft.TextField(
                label="请求头 JSON（可留空）", hint_text='{"Authorization": "Bearer …"}',
                multiline=True, min_lines=2, max_lines=4),
            # 校验失败的话写在框里：状态行在模态框背后，用户看不见就等于"点了没反应"
            "error": ft.Text("", size=12, color=ft.Colors.RED_600),
        }
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("新建语音档"),
            content=ft.Container(
                width=460,
                content=ft.Column(list(self._fields.values()), tight=True,
                                  spacing=8)),
            actions=[
                ft.TextButton("取消", on_click=lambda e: page.pop_dialog()),
                ft.Button("保存", on_click=self._save_from_dialog),
            ],
        )
        page.show_dialog(dialog)

    def _save_from_dialog(self, _event=None) -> None:
        page = self._page()
        engine = str(self._fields["engine_type"].value or "system")
        url = str(self._fields["url"].value or "").strip()
        name = str(self._fields["name"].value or "").strip()
        raw_headers = str(self._fields["headers"].value or "").strip()
        try:
            headers = json.loads(raw_headers) if raw_headers else {}
            if not isinstance(headers, dict):
                raise ValueError("请求头必须是一个 JSON 对象")
        except ValueError as exc:
            self._fail(page, f"请求头不是合法 JSON：{exc}")
            return
        if engine == "http" and not url:
            self._fail(page, "HTTP 引擎要填音频地址模板")
            return
        if engine == "http" and "{text}" not in url:
            # 服务侧原样拼地址，缺占位符只会念第一次就再没声音
            self._fail(page, "HTTP 引擎的音频地址模板必须包含 {text}")
            return
        if page is not None:
            page.pop_dialog()
        self._fields = {}
        default_name = dict(ENGINE_OPTIONS).get(engine, engine)
        future = self.ui_call(self._tts.add_profile(
            name or default_name, url, headers, engine))
        future.add_done_callback(self.on_done(self._after_add))

    def _fail(self, page, message: str) -> None:
        """校验没过就**留着对话框**把话写在框里：只报状态行的话它在模态框背后。"""
        error = self._fields.get("error")
        if error is not None:
            error.value = message
        self._view.set_status(message)
        if page is not None:
            page.update()

    def _after_add(self, future) -> None:
        if self._view is None:
            return
        try:
            future.result()
        except Exception as exc:
            self._view.set_status(f"新建失败：{exc}")
            return
        self.load()

    def delete_profile(self, profile_id) -> None:
        if self._tts is None or profile_id in (None, ""):
            return
        was_default = str(profile_id) == self._default_id
        future = self.ui_call(self._delete_async(profile_id, was_default))
        future.add_done_callback(self.on_done(lambda f: self.load()))

    async def _delete_async(self, profile_id, was_default: bool) -> None:
        await self._tts.remove_profile(profile_id)
        if was_default and self._reading is not None:
            # 删掉的是默认档：偏好还指着那个 id，朗读条下拉会退回"内置语音"，
            # 但键值留着是脏数据，下次加一条同号的档就莫名其妙成了默认。
            await self._reading.save_reader_preference("tts_profile", "")

    # ------------------------------------------------------------------ #
    # 默认档
    # ------------------------------------------------------------------ #
    def set_default(self, profile_id) -> None:
        if profile_id in (None, ""):
            return
        profile = self._find(profile_id)
        if profile is None:
            return
        self._default_id = str(profile_id)
        self._view.render_profiles(self._profiles, default_id=self._default_id)
        self._view.set_status(f"已把「{profile.get('name')}」设为默认朗读语音档")
        if self._reading is not None:
            self.ui_call(self._reading.save_reader_preference(
                "tts_profile", self._default_id))

    def _page(self):
        view = self._view
        if view is None:
            return None
        try:
            return view.page
        except Exception:
            return None
