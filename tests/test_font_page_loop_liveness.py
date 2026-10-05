"""字体页的编排不许把界面循环冻住（第七十六轮 A · 004 待修清单 16）。

判据钉的是**心跳没断供**，不是"这次调用返回了"：死锁的表现正是那条循环上再没有一拍，
而 `asyncio.wait_for` 的超时同样挂在那条循环上——堵住了连超时都不会响（第七十五轮
实测如此），所以秒表必须由**另一条线程**拿着。

替身服务每次读取都要界面循环跳够 ``BEATS_PER_READ`` 拍才回：协程本就跑在那条循环上，
`asyncio.sleep` 靠它推进，循环一堵这里就永远回不来——"编排还在途"与"循环在跳"
被绑成同一件事，判据因此不依赖墙钟时长（跨机不可复现），只依赖**断供有没有发生过**。
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.controllers.tools.font_controller import FontController
from app.core.engine_worker import set_ui_loop, ui_loop
from app.services.tools.font_service import FontService

BEAT_SECONDS = 0.01
#: 一次列表读取要跳够这么多拍才回（≈30ms），只为把"循环在跑"变成可数的读数。
BEATS_PER_READ = 3
#: 第二次读取的拍数（≈0.3s）：比第一次慢，才留得住"第一条落值时第二条还在途"。
USER_BEATS = 30
#: 心跳断供超过这个秒数就算"循环被冻住"。健康读数是本档的百分之一量级，
#: 而真死锁是无穷——两头都离阈值很远。
FREEZE_SECONDS = 1.0
#: 对照臂里故意堵循环的秒数（必须明显大于 FREEZE_SECONDS）。
CONTROL_BLOCK_SECONDS = 2.0

SYSTEM_FONTS = [{"family": "宋体", "source": "system"}]
USER_FONTS = [{"family": "霞鹜文楷", "source": "user", "id": 7}]


class _SlowFontService(FontService):
    """不碰磁盘的字体服务：两次读取各要界面循环跳 BEATS_PER_READ 拍才回，
    并且都要等主线程开闸（见 ``_Gate``）才交结果。

    必须是 ``FontService`` 的子类——父类改了这两个方法的签名，这里要一起红。
    """

    def __init__(self, rig: "_UIRig") -> None:
        super().__init__()
        self._rig = rig
        self.calls: list[str] = []
        #: 第二次读取比第一次慢这么多拍。真死锁的形状就是"第一条落值时第二条还在途"
        #: （004 第 16 条：先派发的先落值，回调必然跑在第二条之前）；两臂同速时这条
        #: 形状复现不出来——实测同速下变异臂八次里只有一次真锁住。
        self.user_beats = USER_BEATS

    async def scan_system_fonts(self):
        self.calls.append("scan")
        await self._rig.count_beats(BEATS_PER_READ)
        await self._rig.gates["scan"].wait()
        return [dict(f) for f in SYSTEM_FONTS]

    async def list_user_fonts(self):
        self.calls.append("user")
        await self._rig.count_beats(self.user_beats)
        await self._rig.gates["user"].wait()
        return [dict(f) for f in USER_FONTS]


class _FontView:
    """字体页视图替身：只实现真 ``FontView`` 有的那三个写入口。"""

    def __init__(self, rig: "_UIRig") -> None:
        self._rig = rig
        self.page = None          # 取不到 page 时 flush 静默跳过：这里不下发
        self.renders: list[tuple] = []
        self.status: list[str] = []

    def set_current(self, family: str) -> None:
        self._rig.current.append(family)

    def render_fonts(self, system_fonts, user_fonts, current) -> None:
        self.renders.append((list(system_fonts), list(user_fonts), current))
        self._rig.rendered.set()

    def set_status(self, text: str) -> None:
        self.status.append(text)


class _Gate:
    """主线程放行、界面循环上可 ``await`` 的闸门（默认开着，开时不占拍）。

    对照臂靠它把两件事钉死，与线程快慢无关：
    - ``scan`` 闸在登记完成回调之前一直关着。``concurrent.futures.Future`` 的
      ``add_done_callback`` 在登记那一刻 future 已经算完时会当场在主线程调用回调，
      那一臂冻住的是主线程而不是界面循环（实测 20 次跑撞上一次，读数 0.016s）；
    - ``user`` 闸整臂都不开：跨 future 等的那一条永远落不了值，循环必然堵到
      ``second.result`` 的超时为止。
    """

    def __init__(self) -> None:
        self._ev = threading.Event()
        self._ev.set()

    def close(self) -> None:
        self._ev.clear()

    def open(self) -> None:
        self._ev.set()

    async def wait(self) -> None:
        while not self._ev.is_set():
            await asyncio.sleep(BEAT_SECONDS)


class _FakeHub:
    bus = None

    def __init__(self, svc) -> None:
        self._svc = svc

    def get(self, stype):
        return self._svc if stype is FontService else None


class _UIRig:
    """另一条线程上的真界面循环 + 心跳（含断供读数）+ 真控制器 + 替身视图。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.beats = 0
        self.max_gap = 0.0
        self.rendered = threading.Event()
        self.gates = {"scan": _Gate(), "user": _Gate()}
        self.current: list[str] = []
        self.service = _SlowFontService(self)
        self.view = _FontView(self)
        self.controller = FontController(_FakeHub(self.service))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self._saved_loop = None
        self._last_tick: float | None = None

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def __enter__(self) -> "_UIRig":
        self._thread.start()
        self._ready.wait(5)
        self._saved_loop = ui_loop()
        set_ui_loop(self.loop)
        self.loop.call_soon_threadsafe(self._tick)
        return self

    def __exit__(self, *_exc) -> None:
        self.controller.detach()
        set_ui_loop(self._saved_loop)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            self.loop.close()

    def _tick(self) -> None:
        now = time.monotonic()
        if self._last_tick is not None:
            self.max_gap = max(self.max_gap, now - self._last_tick)
        self._last_tick = now
        self.beats += 1
        self.loop.call_later(BEAT_SECONDS, self._tick)

    async def count_beats(self, wanted: int) -> int:
        """在界面循环上等够 ``wanted`` 拍——这条循环不跳就永远回不来。"""
        start = self.beats
        while self.beats - start < wanted:
            await asyncio.sleep(BEAT_SECONDS)
        return self.beats - start

    def enter_font_page(self) -> int:
        """走真路径进页（attach 里就发起加载），返回发起那一刻的拍数。"""
        self.max_gap = 0.0
        started = self.beats
        self.controller.attach(self.view)
        return started

    def wire_view(self) -> None:
        """接视图并跑真 ``on_attach``，只把进页那下自动加载按住。"""
        self.controller.load_fonts = lambda: None
        self.controller.attach(self.view)

    def report(self, seconds: float, started: int) -> dict:
        """主线程读数：编排回没回、这段时间跳了多少拍、最大断供多少秒。"""
        came_back = self.rendered.wait(seconds)
        return {"came_back": came_back,
                "beats": self.beats - started,
                "max_gap": self.max_gap}


@pytest.fixture()
def rig() -> "_UIRig":
    with _UIRig() as created:
        yield created


def test_font_page_load_does_not_starve_the_ui_loop(rig: _UIRig) -> None:
    """进字体页这一趟，心跳一拍都不能断供。

    红法（第七十五轮实测必现死锁）：两次读取各自 ``ui_call``，第一次的完成回调在循环
    线程上 ``user_future.result()`` 等第二次——回调一堵，第二次永远落不了值，心跳停住，
    ``rendered`` 永远等不到。
    """
    started = rig.enter_font_page()
    reading = rig.report(6.0, started)

    assert reading["came_back"], "字体页编排没回来：界面循环多半被完成回调堵死了"
    assert reading["max_gap"] < FREEZE_SECONDS, (
        f"心跳断供 {reading['max_gap']:.3f}s——编排期间界面循环被同步堵住了")
    assert reading["beats"] >= 2 * BEATS_PER_READ, (
        f"编排全程只跳了 {reading['beats']} 拍，不到两次读取所需的 "
        f"{2 * BEATS_PER_READ} 拍：读数不是从这条循环上取的")


def test_font_page_renders_both_lists_when_it_arrives(rig: _UIRig) -> None:
    """两次读取都到齐才渲染，落的是两边的真内容（不是只有系统那半）。"""
    started = rig.enter_font_page()
    assert rig.report(6.0, started)["came_back"] is True
    assert rig.service.calls == ["scan", "user"], "两次读取没都发起"
    assert rig.view.renders == [(SYSTEM_FONTS, USER_FONTS, "")], (
        f"字体页渲染落的是 {rig.view.renders}")


def test_the_instrument_does_read_a_frozen_loop(rig: _UIRig) -> None:
    """对照臂：就地重放第七十五轮那条写法，仪器必须读出"心跳断供"。

    这根不是走过场：正臂那句"没断供"只有在这台仪器量得到断供时才算数。重放用的是
    真服务替身、真 ``on_done`` 包装、真 ``_render``（视图也真接着）——堵循环的那一步
    就是当年那一行。
    """
    rig.wire_view()
    rig.gates["scan"].close()            # 第一次读取等主线程放行，见 ``_Gate``
    rig.gates["user"].close()            # 这一臂整程都不开：那条永远落不了值
    first = rig.controller.ui_call(rig.service.scan_system_fonts())
    second = rig.controller.ui_call(rig.service.list_user_fonts())

    def old_shape(future) -> None:
        rig.controller._render(
            system=list(future.result() or []),
            # ↓ 当年那一步：完成回调跑在循环线程上，却同步等另一条 future 落值。
            user=list(second.result(timeout=CONTROL_BLOCK_SECONDS) or []))

    first.add_done_callback(rig.controller.on_done(old_shape))
    started = rig.beats
    rig.max_gap = 0.0
    rig.gates["scan"].open()

    reading = rig.report(3.5, started)
    assert reading["max_gap"] >= FREEZE_SECONDS, (
        f"堵了 {CONTROL_BLOCK_SECONDS}s 的循环只量到 {reading['max_gap']:.3f}s "
        "断供——心跳计数器没接在这条循环上")
    assert reading["came_back"] is False, (
        "对照臂里字体页照旧渲染了：这一臂没冻住那条循环")

