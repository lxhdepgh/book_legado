"""朗读语音档管理页的离线护栏：CRUD 与"默认档"键名都要钉住。

``ReadAloudService`` 的 ``list_profiles`` / ``add_profile`` / ``remove_profile``
和 ``tts_profiles`` 表（schema v4）早就齐了，但**没有任何界面调用过它们**——表
永远是空的，阅读器朗读条上的音色下拉也就永远只有"内置语音"一项。本文件钉住
新接上的这一段：

1. 清单真的从服务读，空表要说"还没有"，而不是渲染出一片空白；
2. 新建对话框保存的是**四样**（名称 / 引擎 / 地址模板 / 请求头），HTTP 引擎缺
   ``{text}`` 占位必须当场拒——服务侧不校验，存进去就是一条永远不出声的档；
3. "设为默认"落的键是 ``reader.tts_profile``，与阅读器读的那一个**同名**（字体页
   就栽过 font.family / reader.font_family 不同名，见 docs/dev-logs/2026-09-26.md）；
4. 删掉当前默认档要顺手清掉那个键，不然下次同号的档莫名其妙就成了默认；
5. 在途结果回来时页可能已经切走：不许碰控件，也不许把服务侧动作落下。

全程离线：服务是替身（不碰 pyttsx3 / edge-tts / 网络）。
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import flet as ft
import pytest

from app.controllers.tools.tts_controller import TtsProfileController
from app.services.content.read_aloud import ReadAloudService
from app.services.reading.reading_service import ReadingService
from app.views.tools.tts_view import TtsProfileView

HTTP_PROFILE = {
    "id": 7, "name": "书源 TTS",
    "url": "http://127.0.0.1:1/tts?text={text}",
    "headers": {"X-Key": "offline"}, "engine_type": "http",
}
EDGE_PROFILE = {"id": 8, "name": "晓晓", "url": "", "headers": {},
                "engine_type": "edge"}


class _StubTts:
    """语音档表替身：记账 add/remove，返回预置清单，可选"堵着不回"。"""

    def __init__(self, profiles=None, *, hold: bool = False) -> None:
        self.profiles = [dict(p) for p in (profiles or [])]
        self.added: list[tuple] = []
        self.removed: list = []
        self._gate = threading.Event() if hold else None

    async def list_profiles(self):
        if self._gate is not None:
            await asyncio.to_thread(self._gate.wait)
        return [dict(p) for p in self.profiles]

    async def add_profile(self, name, url="", headers=None, engine_type="system"):
        self.added.append((name, url, headers, engine_type))
        return 99

    async def remove_profile(self, profile_id):
        self.removed.append(profile_id)
        return True


class _StubReading:
    """偏好替身：只记键名，因为本页唯一的外溢就是 reader.tts_profile。"""

    def __init__(self, values=None) -> None:
        self.values = dict(values or {})
        self.saved: list[tuple[str, object]] = []

    async def get_reader_preference(self, key, default=None):
        return self.values.get(key, default)

    async def save_reader_preference(self, key, value) -> None:
        self.saved.append((key, value))
        self.values[key] = value


class _FakeHub:
    def __init__(self, services):
        self.bus = None
        self._services = services

    def get(self, stype):
        return self._services[stype]


def _attach(profiles=None, default_id="", *, tts=None, with_service=True,
            hold: bool = False):
    tts = tts if tts is not None else _StubTts(profiles, hold=hold)
    reading = _StubReading({"tts_profile": default_id} if default_id else {})
    services = {ReadAloudService: tts, ReadingService: reading}
    if not with_service:
        services.pop(ReadAloudService)
    view = TtsProfileView()
    controller = TtsProfileController(_FakeHub(services))
    controller.attach(view)
    return SimpleNamespace(view=view, controller=controller, tts=tts,
                           reading=reading)


def _detach(rig) -> None:
    rig.controller.detach()


def _settled(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _rows(rig) -> list[ft.ListTile]:
    return list(rig.view._rows.controls)


def _status(rig) -> str:
    return str(getattr(rig.view._status, "value", "") or "")


def _mounted(rig) -> SimpleNamespace:
    """给控制器接一只假 page：对话框只认 show_dialog / pop_dialog / update。"""
    page = SimpleNamespace(dialogs=[], popped=0, updates=0)

    def show_dialog(dialog):
        page.dialogs.append(dialog)

    def pop_dialog():
        page.popped += 1
        if page.dialogs:
            page.dialogs.pop()

    page.show_dialog = show_dialog
    page.pop_dialog = pop_dialog
    page.update = lambda: setattr(page, "updates", page.updates + 1)
    rig.controller._page = lambda: page
    return page


def _open_editor(rig):
    """点"新建语音档"，返回对话框里的输入控件（按标签取）。"""
    page = _mounted(rig)
    rig.view.on_add()
    assert page.dialogs, "新建按钮没弹出对话框"
    fields = {}
    for control in page.dialogs[-1].content.content.controls:
        label = getattr(control, "label", None)
        if label:
            fields[label] = control
    return page, fields


def _dialog_error(page) -> str:
    """框内那行红字：校验没过时用户只看得到它，状态行在模态框背后。"""
    texts = [c for c in page.dialogs[-1].content.content.controls
             if isinstance(c, ft.Text)]
    return str(texts[-1].value or "") if texts else ""


def _save(rig, page) -> None:
    page.dialogs[-1].actions[-1].on_click(None)    # 那个"保存"按钮


@pytest.fixture()
def rig():
    handle = _attach([HTTP_PROFILE, EDGE_PROFILE], default_id="8")
    try:
        yield handle
    finally:
        _detach(handle)


@pytest.fixture()
def empty_rig():
    handle = _attach([])
    try:
        yield handle
    finally:
        _detach(handle)


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #
def test_the_table_is_read_from_the_service(rig):
    """行必须来自 list_profiles：读不到就是又一次"服务有、界面空"。"""
    assert _settled(lambda: len(_rows(rig)) == 2), "清单没渲染出来"
    titles = [row.title.value for row in _rows(rig)]
    assert titles == ["书源 TTS", "晓晓"]
    # 副标题说清引擎和地址：只有 http 会用 url，别的引擎留空是正常的
    assert "书源 HTTP" in _rows(rig)[0].subtitle.value
    assert "edge 语音" in _rows(rig)[1].subtitle.value
    assert "当前默认：晓晓" in _status(rig), _status(rig)


def test_an_empty_table_says_so(empty_rig):
    assert _settled(lambda: "还没有语音档" in _status(empty_rig)), _status(empty_rig)
    assert _rows(empty_rig) == []


def test_the_page_survives_without_the_service():
    handle = _attach(with_service=False)
    try:
        assert "ReadAloudService 未就绪" in _status(handle)
        page = _mounted(handle)
        handle.view.on_add()          # 没服务连框都不该开：填完才发现存不了更糟
        assert page.dialogs == []
        assert "未就绪" in _status(handle)
        handle.controller.delete_profile("7")
        handle.controller.set_default("7")
    finally:
        _detach(handle)


# --------------------------------------------------------------------------- #
# 新建
# --------------------------------------------------------------------------- #
def test_saving_the_dialog_registers_all_four_fields(rig):
    page, fields = _open_editor(rig)
    fields["名称"].value = "我的 TTS"
    fields["引擎"].value = "http"
    fields["音频地址模板（仅 HTTP 引擎）"].value = HTTP_PROFILE["url"]
    fields["请求头 JSON（可留空）"].value = '{"X-Key": "offline"}'
    _save(rig, page)
    assert _settled(lambda: rig.tts.added), "保存没落到 add_profile"
    assert rig.tts.added[0] == ("我的 TTS", HTTP_PROFILE["url"],
                                {"X-Key": "offline"}, "http")
    assert page.popped == 1, "保存后没关对话框"


def test_a_blank_name_still_gets_a_readable_one(rig):
    """名称留空不该存出一条"(未命名)"：引擎名本身就是最好的默认。"""
    page, fields = _open_editor(rig)
    fields["引擎"].value = "edge"
    _save(rig, page)
    assert _settled(lambda: rig.tts.added)
    assert rig.tts.added[0][0] == "edge 语音"
    assert rig.tts.added[0][3] == "edge"


def test_http_without_a_text_placeholder_is_refused(rig):
    """HTTP 引擎缺 {text}：服务侧原样拼地址，念完第一句就再没声音，所以这里挡。"""
    page, fields = _open_editor(rig)
    fields["名称"].value = "缺占位"
    fields["引擎"].value = "http"
    fields["音频地址模板（仅 HTTP 引擎）"].value = "http://127.0.0.1:1/tts"
    _save(rig, page)
    assert "{text}" in _status(rig), _status(rig)
    assert "{text}" in _dialog_error(page), "话只写在模态框背后，用户看不见"
    assert rig.tts.added == [], "校验没过还存了档"
    assert page.popped == 0, "拒了还把框关了：用户填的东西一起没了"


def test_http_without_any_url_says_which_field_is_missing(rig):
    page, fields = _open_editor(rig)
    fields["引擎"].value = "http"
    _save(rig, page)
    assert "音频地址模板" in _dialog_error(page), _dialog_error(page)
    assert rig.tts.added == []


def test_headers_must_be_a_json_object(rig):
    page, fields = _open_editor(rig)
    fields["引擎"].value = "edge"
    fields["请求头 JSON（可留空）"].value = "X-Key: offline"
    _save(rig, page)
    assert "JSON" in _dialog_error(page), _dialog_error(page)
    assert rig.tts.added == []

    fields["请求头 JSON（可留空）"].value = '["不是对象"]'
    _save(rig, page)
    assert rig.tts.added == [], "数组也算合法 JSON，但不是请求头"


def test_a_failed_save_is_said_out_loud(rig):
    async def broken_add(*_args, **_kwargs):
        raise RuntimeError("表写不进去")

    rig.tts.add_profile = broken_add
    page, fields = _open_editor(rig)
    fields["引擎"].value = "edge"
    _save(rig, page)
    assert _settled(lambda: "新建失败" in _status(rig)), _status(rig)


# --------------------------------------------------------------------------- #
# 默认档 / 删除
# --------------------------------------------------------------------------- #
def test_clicking_a_row_sets_the_default_under_the_reader_key(rig):
    """键名必须是 reader.tts_profile——阅读器读的就是这一个（字体页踩过不同名）。"""
    assert _settled(lambda: len(_rows(rig)) == 2)
    _rows(rig)[0].on_click(None)
    assert _settled(lambda: ("tts_profile", "7") in rig.reading.saved), \
        rig.reading.saved
    assert _rows(rig)[0].selected is True, "设了默认，行却没高亮"


def test_deleting_a_row_calls_the_service(rig):
    """删完要重新拉一次表：留着已删的那一行在界面上，点它等于删第二遍。"""
    assert _settled(lambda: len(_rows(rig)) == 2)
    rig.tts.profiles = [EDGE_PROFILE]        # 替身跟着删，模拟表的真实变化
    _rows(rig)[1].trailing.on_click(None)
    assert _settled(lambda: [str(x) for x in rig.tts.removed] == ["8"]), rig.tts.removed
    assert _settled(lambda: len(_rows(rig)) == 1), "删掉之后清单没重拉"
    assert _rows(rig)[0].title.value == "晓晓"


def test_deleting_the_default_clears_the_preference(rig):
    """默认档被删：偏好还指着 8，下次同号的新档会莫名其妙成默认。"""
    assert _settled(lambda: len(_rows(rig)) == 2)
    _rows(rig)[1].trailing.on_click(None)      # 晓晓（当前默认）
    assert _settled(lambda: ("tts_profile", "") in rig.reading.saved), \
        rig.reading.saved


def test_deleting_some_other_row_keeps_the_default(rig):
    assert _settled(lambda: len(_rows(rig)) == 2)
    _rows(rig)[0].trailing.on_click(None)      # 书源 TTS，不是默认
    assert _settled(lambda: rig.tts.removed == ["7"])
    assert ("tts_profile", "") not in rig.reading.saved, rig.reading.saved


# --------------------------------------------------------------------------- #
# 在途结果不许砸在已经切走的页上
# --------------------------------------------------------------------------- #
def test_a_load_that_comes_back_after_detach_does_not_explode():
    """清单回来的路上用户切了页：不能碰控件，也不能崩在协程里。"""
    tts = _StubTts([HTTP_PROFILE], hold=True)
    rig = _attach(tts=tts)
    view = rig.view
    _detach(rig)
    tts._gate.set()
    time.sleep(0.3)
    assert rig.controller._view is None
    assert rig.controller._profiles == [], "detach 之后不该再把结果写进本地缓存"
    assert view._rows.controls == [], "页都切走了还往旧控件上画"
