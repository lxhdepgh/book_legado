"""全应用背景图（第七十四轮）：服务面 + 界面面护栏。

服务面（``BackgroundService``，零 flet）：

1. 选中的图**拷进数据目录** ``backgrounds/``，库里存的是那份拷贝；
2. 归一：长边压到上限、落成 JPEG（原图动辄几千万像素，不该每次重建都整张解码）；
3. 换第二张时旧的那份被删掉，目录里只剩一张（不留越攒越多的垃圾）；
4. 坏文件不许把已经铺好的偏好改掉；
5. ``current`` 认文件不认键：文件没了就当没有（宁可不铺，也不铺一块裂图）；
6. ``clear`` **只删我们目录里的路径**——键被手工改成别处时不能顺手删用户的文件。

界面面（``NavigationHost`` 那一层 + 外观页 + 装配根接线）：

7. 有图时根是 ``Stack``（图在底、布局在上），没图时**根本不多这一层**；
8. 图的不透明度落在"看得见但压得住正文"的区间（钉语义，不读那个常量本身）；
9. 路径没变时 ``set_background`` 什么都不做——重建整棵根树会掀掉在途渲染；
10. 宽/窄窗重建（``rebuild``）之后背景层还在：缩放一次就把背景弄丢是最难复查的断法；
11. 外观页的选图/清除把成败写在状态行上，并自己读回一次再报成败；
12. 装配根那条订阅真的把偏好变更推到外壳上——设置页、服务、外壳三方互不认识，
    最容易断的就是这一跳接线。

红线核验：``BackgroundService`` 零 flet、外观视图零 ``app.services`` import。
"""
from __future__ import annotations

import ast
import asyncio
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import flet as ft

from app.controllers.settings.appearance_controller import AppearanceController
from app.core.registry import GROUP_READING, ViewRegistry
from app.core.settings import SettingsService
from app.core.storage import StorageService
from app.services.app_paths import AppPaths
from app.services.tools.background_service import (BACKGROUND_IMAGE_KEY,
                                                   BackgroundService)
from app.services.tools.theme_service import ThemeService
from app.views import navigation
from app.views.navigation import NavigationHost
from app.views.settings.appearance_view import AppearanceView


# --------------------------------------------------------------------------- #
# 替身：只换"读写偏好"这一个口子，目录走真 StorageService（指到临时目录）
# --------------------------------------------------------------------------- #
class _RecordingSettings(SettingsService):
    """按键记读写的 SettingsService 子类（服务只用到 get/set 两张口子）。

    必须是**真类子类**：好几条断言读的就是"写了哪几个键"，鸭子对象会把
    "写库 + 发事件"这条真链路整个伪装掉。
    """

    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.writes: list[tuple[str, object]] = []

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value) -> None:
        self.store[key] = value
        self.writes.append((key, value))


def _imported_modules(cls) -> list[str]:
    """一个类所在模块里的 import 语句解析出的全限定模块名（AST，不扫原文）。"""
    path = Path(sys.modules[cls.__module__].__file__)
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"),
                                   filename=str(path))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.append(node.module or "")
    return names


def _make_image(path: Path, size: tuple[int, int]) -> None:
    from PIL import Image

    with Image.new("RGB", size, (200, 30, 90)) as im:
        im.save(path, format="PNG")


# --------------------------------------------------------------------------- #
# 1. 服务面
# --------------------------------------------------------------------------- #
class BackgroundServiceTests(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.storage = StorageService(
            paths=AppPaths(data=self.root, cache=self.root / "cache"))
        self.settings = _RecordingSettings()
        self.service = BackgroundService(storage=self.storage, settings=self.settings)
        self.source = self.root / "原图.png"
        _make_image(self.source, (64, 48))

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_copies_into_data_dir_and_records_preference(self):
        installed = asyncio.run(self.service.install(self.source))
        self.assertTrue(Path(installed).is_file(), "落点必须是个真文件")
        self.assertEqual(Path(installed).parent, self.root / "backgrounds")
        self.assertNotEqual(Path(installed), self.source, "不许就地引用用户选的原图")
        self.assertTrue(self.source.is_file(), "原图不许被动过")
        self.assertEqual(self.settings.writes, [(BACKGROUND_IMAGE_KEY, installed)])
        self.assertEqual(self.service.current, installed)

    def test_normalize_caps_long_edge_and_writes_jpeg(self):
        big = self.root / "四千万像素.png"
        _make_image(big, (4000, 3000))
        installed = asyncio.run(self.service.install(big))
        from PIL import Image

        with Image.open(installed) as im:
            self.assertLessEqual(max(im.size), 2560, "长边没压下来：每次重建都解它")
            self.assertEqual(im.format, "JPEG")

    def test_second_install_removes_the_previous_copy(self):
        first = Path(asyncio.run(self.service.install(self.source)))
        other = self.root / "第二张.png"
        _make_image(other, (32, 32))
        second = Path(asyncio.run(self.service.install(other)))
        self.assertFalse(first.exists(), "换图后旧的那份还留在数据目录里")
        self.assertEqual(sorted(p.name for p in second.parent.iterdir()),
                         [second.name], "目录里只该剩当前这一张")

    def test_picking_the_same_image_twice_does_not_delete_itself(self):
        first = asyncio.run(self.service.install(self.source))
        again = asyncio.run(self.service.install(self.source))
        self.assertEqual(first, again)
        self.assertTrue(Path(again).is_file(), "先落键再删旧，删之前要比一下是不是同一份")

    def test_missing_source_raises_and_leaves_preference_untouched(self):
        asyncio.run(self.service.install(self.source))
        before = dict(self.settings.store)
        with self.assertRaises(FileNotFoundError):
            asyncio.run(self.service.install(self.root / "不存在.png"))
        self.assertEqual(self.settings.store, before, "选失败不许把已经铺好的背景改掉")
        self.assertEqual(len(self.settings.writes), 1, "失败那一次不许写键")

    def test_current_is_empty_when_the_file_disappeared(self):
        installed = Path(asyncio.run(self.service.install(self.source)))
        installed.unlink()          # 用户自己进数据目录把图删了
        self.assertEqual(self.service.current, "", "文件没了还照铺 = 一块裂图")

    def test_clear_empties_preference_and_removes_our_copy(self):
        installed = Path(asyncio.run(self.service.install(self.source)))
        asyncio.run(self.service.clear())
        self.assertEqual(self.settings.store[BACKGROUND_IMAGE_KEY], "")
        self.assertFalse(installed.exists())
        self.assertEqual(self.service.current, "")

    def test_clear_never_deletes_a_path_outside_our_dir(self):
        """键可能指向别处（手工改库/从备份恢复）：清除只许动我们自己目录里的文件。"""
        outside = self.root / "用户自己的图.png"
        _make_image(outside, (16, 16))
        self.settings.store[BACKGROUND_IMAGE_KEY] = str(outside)
        asyncio.run(self.service.clear())
        self.assertTrue(outside.exists(), "删掉用户目录里的原图是不可逆的事故")
        self.assertEqual(self.settings.store[BACKGROUND_IMAGE_KEY], "")

    def test_install_without_pillow_copies_verbatim(self):
        """没装 Pillow：功能不退化成"用不了"，只是不压缩。"""
        with mock.patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
            installed = asyncio.run(self.service.install(self.source))
        self.assertTrue(Path(installed).is_file())
        self.assertEqual(Path(installed).read_bytes(), self.source.read_bytes())
        self.assertEqual(self.service.current, installed)

    def test_service_module_imports_no_flet(self):
        """红线 #6：服务层零 flet（背景图只写文件与偏好，不碰控件）。"""
        offenders = [m for m in _imported_modules(BackgroundService)
                     if m == "flet" or m.startswith("flet.")]
        self.assertEqual(offenders, [], f"背景服务引了 flet：{offenders}")


# --------------------------------------------------------------------------- #
# 2. 界面面：导航外壳那一层
# --------------------------------------------------------------------------- #
class _FakePage:
    """外壳要的三个口子：services / controls / update（计数用来钉"有没有下发"）。"""

    def __init__(self, width: float = 1200.0) -> None:
        self.width = width
        self.services: list = []
        self.controls: list = []
        self.updates = 0

    def add(self, *controls) -> None:
        self.controls.extend(controls)

    def update(self) -> None:
        self.updates += 1


def _registry() -> ViewRegistry:
    registry = ViewRegistry()
    registry.register("reading.find", lambda: ft.Container(),
                      title="找书", group=GROUP_READING)
    return registry


def _host(page=None, background: str = "", width: float = 1200.0) -> NavigationHost:
    return NavigationHost(page, _registry(), width=width,
                          initial_destination="reading.find", background=background)


def _background_layer(root) -> ft.Container | None:
    """根 Stack 里那张图（没有就 None）——按形状认，不读私有字段。"""
    if not isinstance(root, ft.Stack):
        return None
    for child in root.controls or []:
        if isinstance(child, ft.Container) and child.image is not None:
            return child
    return None


class ShellBackgroundLayerTests(unittest.TestCase):
    PATH = str(Path("X:/图/背景.jpg"))
    OTHER = str(Path("X:/图/另一张.jpg"))

    def test_no_image_means_no_extra_layer(self):
        host = _host(background="")
        self.assertIsInstance(host.root, ft.Row, "没背景时不许凭空多一层 Stack")
        self.assertEqual(_background_layer(host.root), None)

    def test_image_wraps_layout_in_a_stack_below_it(self):
        host = _host(background=self.PATH)
        layer = _background_layer(host.root)
        self.assertIsNotNone(layer, "根里没有那张图")
        self.assertEqual(layer.image.src, self.PATH)
        self.assertEqual(layer.image.fit, ft.BoxFit.COVER,
                         "不 COVER 就留边：窗口不是图的形状")
        opacity = layer.image.opacity
        self.assertIsInstance(opacity, float)
        self.assertTrue(0.0 < opacity < 1.0,
                        "图要么完全不透（正文压不住）要么全透明（等于没换图）")
        self.assertEqual(len(host.root.controls), 2, "只该是「图 + 布局」两层")
        # 图必须在布局**下面**：Stack 后画的在上，顺序反了就是把正文盖住。
        self.assertIs(_background_layer(host.root), host.root.controls[0])
        self.assertIsInstance(host.root.controls[1], ft.Row)

    def test_narrow_window_keeps_the_layer_over_the_bar_layout(self):
        host = _host(background=self.PATH, width=400.0)
        self.assertIsNotNone(_background_layer(host.root))
        self.assertIsInstance(host.root.controls[1], ft.Column)

    def test_rebuild_keeps_the_layer(self):
        """宽/窄窗重建最容易把背景弄丢：根换了新的，那一层必须跟着重新垫上。"""
        host = _host(background=self.PATH)
        rebuilt = host.rebuild()
        layer = _background_layer(rebuilt)
        self.assertIsNotNone(layer, "缩放一次窗口背景就没了")
        self.assertEqual(layer.image.src, self.PATH)

    def test_set_background_swaps_the_layer_and_repaints(self):
        page = _FakePage()
        host = _host(page, background=self.PATH)
        before = host.root
        host.set_background(self.OTHER)
        self.assertIsNot(host.root, before, "换了图却没重建根")
        self.assertEqual(_background_layer(host.root).image.src, self.OTHER)
        self.assertEqual(host.background, self.OTHER)
        self.assertGreater(page.updates, 0, "改完控件没下发")
        self.assertEqual(page.controls, [host.root], "新根没挂回页面")

    def test_same_path_does_not_rebuild_the_root(self):
        """路径没变就不许动根：整棵根树重建会掀掉当前视图的在途渲染。"""
        page = _FakePage()
        host = _host(page, background=self.PATH)
        before, updates = host.root, page.updates
        host.set_background(self.PATH)
        self.assertIs(host.root, before)
        self.assertEqual(page.updates, updates)

    def test_clearing_the_image_drops_the_layer(self):
        host = _host(background=self.PATH)
        host.set_background("")
        self.assertIsInstance(host.root, ft.Row)
        self.assertEqual(_background_layer(host.root), None)

    def test_set_background_without_a_page_does_not_crash(self):
        """无头（page=None）没有可刷新的窗口：改状态要成，下发跳过。"""
        host = _host(page=None, background="")
        host.set_background(self.PATH)
        self.assertEqual(host.background, self.PATH)
        self.assertIsNotNone(_background_layer(host.root))


# --------------------------------------------------------------------------- #
# 3. 外观页：那一行的回填
# --------------------------------------------------------------------------- #
def _walk(control):
    yield control
    for attr in ("controls", "content"):
        child = getattr(control, attr, None)
        items = (child if isinstance(child, list)
                 else [child] if child is not None else [])
        for item in items:
            if isinstance(item, ft.Control):
                yield from _walk(item)


def _text_of(view) -> str:
    return " ".join(str(getattr(c, "value", "") or "")
                    for c in _walk(view) if isinstance(c, ft.Text))


class AppearanceViewBackgroundRowTests(unittest.TestCase):

    def setUp(self):
        self.view = AppearanceView()

    def test_view_exposes_a_picker_and_two_callback_slots(self):
        # 名字必须是 image_picker：导航外壳按这个名字把它挂进 page.services，
        # 换名字 = 选择器永远弹不出来（而且只在全量装配时才暴露）。
        self.assertIsInstance(self.view.image_picker, ft.FilePicker)
        self.assertIsNone(self.view.on_pick_background)
        self.assertIsNone(self.view.on_clear_background)

    def test_set_background_shows_file_name_and_reveals_clear(self):
        self.view.set_background(str(Path("X:/数据目录/backgrounds/abc.jpg")))
        # 只读那一行标签，不读整页文本：页面上的说明文字本来就写着"存进应用数据
        # 目录"，拿整页做负向断言会把它自己抓住。盯目录那一段的名字而不是 "X:/"：
        # WindowsPath 会把斜杠换成反斜杠，"X:/" 在两种写法下都不出现，等于没钉。
        label = str(self.view._background_label.value or "")
        self.assertIn("abc.jpg", label, "只该写文件名，整条路径读不出信息")
        self.assertNotIn("数据目录", label)
        self.assertNotIn("backgrounds", label)
        self.assertTrue(self.view._background_thumb.visible)
        self.assertTrue(self.view._clear_background.visible)

    def test_empty_background_hides_the_thumb_and_clear(self):
        self.view.set_background(str(Path("X:/a.jpg")))
        self.view.set_background("")
        self.assertIn("当前没有背景图", _text_of(self.view))
        self.assertFalse(self.view._background_thumb.visible)
        self.assertFalse(self.view._clear_background.visible)

    def test_appearance_view_imports_no_services(self):
        """红线 §7.5：视图层零 app.services import（背景图也不能开这个口子）。

        按 AST 认 import 语句，不扫原文：这两个模块的注释里本来就写着
        "本模块零 import app.services"，扫原文等于把红线写反——改一句注释就能
        让护栏变红，而真的加了 import 反倒可能被注释盖过去。
        """
        offenders = _imported_modules(AppearanceView)
        self.assertEqual(
            [m for m in offenders if m == "app.services" or m.startswith("app.services.")],
            [], f"外观视图直连服务层：{offenders}")


# --------------------------------------------------------------------------- #
# 4. 控制器：选完即生效，读回一次再报成败
# --------------------------------------------------------------------------- #
class _Picked:
    def __init__(self, path: str) -> None:
        self.path = path


class _Awaitable:
    """替 picker 的 future：真 FilePicker.pick_files 返回的就是个 awaitable。"""

    def __init__(self, value) -> None:
        self._value = value

    def __await__(self):
        async def done():
            return self._value
        return done().__await__()


class _BackgroundStub(BackgroundService):
    """只换 install/clear/current 三个口子：不碰文件系统也能测控制器那条编排。"""

    def __init__(self) -> None:
        self.installed: list[str] = []
        self.cleared = 0
        self.fail = ""
        #: 设了就当"读回来的偏好"是这个值（测"install 说成了但读回不对"）。
        self.read_back: str | None = None

    @property
    def current(self) -> str:
        return self._value if self.read_back is None else self.read_back

    @property
    def _value(self) -> str:
        return self.installed and f"{self.installed[-1]}.jpg" or ""

    async def install(self, path):
        if self.fail:
            raise ValueError(self.fail)
        self.installed.append(str(path))
        return self._value

    async def clear(self):
        if self.fail:
            raise ValueError(self.fail)
        self.cleared += 1
        self.installed.clear()


class _Hub:
    """按类型给服务的替身：未提供的类型给 None（真 hub.get 也是拿不到就 None）。

    索引键写的是**类名**：``**services`` 在 Python 里只收字符串关键字，而真
    :meth:`ServiceHub.get` 是按类型索引的，所以这里按 ``stype.__name__`` 对上。
    别拿类对象直接当关键字传——那样永远取不到，"服务在位时该装上背景"那几条
    会一路走"未就绪"分支，测试全绿而功能其实没接。
    """

    def __init__(self, **services) -> None:
        self._services = services
        self.bus = None

    def get(self, stype):
        return self._services.get(getattr(stype, "__name__", None))


class _StatusView:
    """控制器侧测试用的外观视图替身：只记三件事，不建控件树。"""

    def __init__(self):
        self.statuses: list[tuple[str, bool]] = []
        self.backgrounds: list[str] = []
        self.applied: list[tuple[str, str, bool]] = []
        self.previews: list = []
        self.image_picker = None
        self.controller = None
        self.page = None

    def set_status(self, text, error=False):
        self.statuses.append((str(text), bool(error)))

    def set_background(self, path):
        self.backgrounds.append(str(path))

    def set_preferences(self, skin_id, mode, eink):
        self.applied.append((skin_id, mode, eink))

    def apply_preview(self, data):
        self.previews.append(data)


class AppearanceControllerBackgroundTests(unittest.TestCase):

    def setUp(self):
        self.background = _BackgroundStub()
        self.view = _StatusView()
        self.controller = AppearanceController(
            _Hub(BackgroundService=self.background)).attach(self.view)
        # 无头场景没有界面循环：把 ui_call 排进去的协程截下来自己跑。
        # 直接调 _install_background 会绕过"到底排没排"这一跳，排错协程测不出来。
        self.scheduled: list = []
        self.controller.ui_call = self.scheduled.append

    def _run_scheduled(self) -> None:
        self.assertEqual(len(self.scheduled), 1, "收尾协程没排进界面循环 = 点了没反应")
        asyncio.run(self.scheduled.pop())

    def test_attach_reflects_the_stored_image(self):
        self.background.installed.append("X:/a.png")     # 让 current 读得出值
        self.controller.load()
        self.assertIn("X:/a.png.jpg", self.view.backgrounds)

    def test_pick_installs_then_reports_success(self):
        self.view.image_picker = mock.Mock(
            pick_files=mock.Mock(return_value=_Awaitable([_Picked("X:/a.png")])))
        self.controller.pick_background()
        captured = self.view.image_picker.pick_files.call_args
        self.assertFalse(captured.kwargs["allow_multiple"])
        self.assertEqual(captured.kwargs["file_type"], ft.FilePickerFileType.IMAGE)
        self._run_scheduled()
        self.assertEqual(self.background.installed, ["X:/a.png"])
        self.assertEqual(self.view.backgrounds[-1], "X:/a.png.jpg")
        text, is_error = self.view.statuses[-1]
        self.assertFalse(is_error)
        self.assertIn("已换上", text)

    def test_cancel_says_so_and_installs_nothing(self):
        asyncio.run(self.controller._install_background(_Awaitable([])))
        self.assertEqual(self.background.installed, [])
        self.assertFalse(self.view.statuses[-1][1])
        self.assertIn("已取消", self.view.statuses[-1][0])

    def test_failure_goes_on_the_status_line_not_only_in_the_log(self):
        self.background.fail = "图片文件不存在"
        asyncio.run(self.controller._install_background(_Awaitable([_Picked("X:/a.png")])))
        text, is_error = self.view.statuses[-1]
        self.assertTrue(is_error)
        self.assertIn("图片文件不存在", text)

    def test_read_back_mismatch_is_reported_as_failure(self):
        """install 报成功、偏好里读回来是别的路径：这时候说"已换上"就是假话。"""
        self.background.read_back = "X:/别的.jpg"
        asyncio.run(self.controller._install_background(_Awaitable([_Picked("X:/a.png")])))
        self.assertTrue(self.view.statuses[-1][1])

    def test_clear_reflects_empty_state(self):
        self.background.installed.append("X:/a.png")
        self.controller.clear_background()
        self._run_scheduled()
        self.assertEqual(self.background.cleared, 1)
        self.assertEqual(self.view.backgrounds[-1], "")
        self.assertIn("已去掉", self.view.statuses[-1][0])

    def test_missing_service_says_ready_state(self):
        view = _StatusView()
        AppearanceController(_Hub()).attach(view)
        view.image_picker = mock.Mock()
        view.on_pick_background()
        self.assertTrue(view.statuses[-1][1])
        self.assertIn("未就绪", view.statuses[-1][0])

    def test_unmounted_picker_is_reported_instead_of_hanging(self):
        """选择器没挂进 page.services 时 pick_files 的 future 永远不返回：要说清楚。"""
        self.view.image_picker = None
        self.controller.pick_background()
        self.assertTrue(self.view.statuses[-1][1])
        self.assertIn("没挂上窗口", self.view.statuses[-1][0])

    def test_background_is_not_part_of_the_pending_save_round_trip(self):
        """背景图选完即生效，不参与"保存并应用"/"恢复默认"那一条线。"""
        self.background.installed.append("X:/a.png")
        self.controller.load()                        # 让页面上确实挂着那张图
        self.assertEqual(self.view.backgrounds[-1], "X:/a.png.jpg")
        self.controller.reset()
        self.assertEqual(self.view.backgrounds[-1], "X:/a.png.jpg",
                         "恢复默认不许把用户刚选的图清掉")
        self.assertEqual(self.background.cleared, 0)
        self.assertIn("背景图不受影响", self.view.statuses[-1][0])


# --------------------------------------------------------------------------- #
# 5. 装配根那条订阅：设置页 → 外壳那一层
# --------------------------------------------------------------------------- #
class _WiredSettings(SettingsService):
    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.subscribers: list[tuple[frozenset, object]] = []

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value) -> None:
        self.store[key] = value

    def subscribe(self, keys, handler):
        self.subscribers.append((frozenset(keys), handler))
        return lambda: None


class _WiredBackground(BackgroundService):
    def __init__(self, current: str = "") -> None:
        self._current = current

    @property
    def current(self) -> str:
        return self._current


class _HostProbe:
    def __init__(self) -> None:
        self.applied: list[str] = []

    def set_background(self, path) -> None:
        self.applied.append(str(path))


class _RegHub:
    """装配整盘用的最小服务表：`get` 一律报"没注册"。

    视图是轻壳、控制器 attach 时才取服务，所以真入口在这张空表上也建得起来
    ——这正是 :func:`build_app_shell` 在首屏那一下的形状。
    """

    class _Bus:
        def subscribe(self, *a, **k):
            return lambda: None

        def publish(self, *a, **k):
            return None

    def __init__(self) -> None:
        self.bus = self._Bus()

    def get(self, stype):
        raise KeyError(stype)

    def require(self, stype):
        raise KeyError(stype)


class AssemblyWiringTests(unittest.TestCase):

    FIRST_PAINT = str(Path("X:/图/首屏.jpg"))

    def setUp(self):
        import main

        self.main = main
        self.settings = _WiredSettings()
        self.background = _WiredBackground("X:/a.jpg")
        self.hub = mock.Mock()
        self.hub.require = mock.Mock(return_value=self.settings)
        self.hub.get = mock.Mock(return_value=self.background)
        self.host = _HostProbe()
        # 键订阅的处理器本来要编组到界面循环；无头就把这层"编组"摘成直连，
        # 测的是"变更到底推没推到外壳"，不是那条编组本身。
        self.patch_ui = mock.patch.object(
            sys.modules["app.core.engine_worker"], "ui_loop", lambda: None)
        self.patch_host = mock.patch.object(navigation, "current_host", lambda: self.host)

    def _wired_handler(self):
        self.main._wire_background(self.hub)
        self.assertEqual(len(self.settings.subscribers), 1, "没订阅键 = 换图不生效")
        keys, handler = self.settings.subscribers[0]
        self.assertEqual(set(keys), {BACKGROUND_IMAGE_KEY})
        return handler

    def test_initial_path_is_handed_to_the_shell_at_creation(self):
        with self.patch_ui, self.patch_host:
            self.assertEqual(self.main._wire_background(self.hub), "X:/a.jpg")

    def test_preference_change_pushes_the_new_path_into_the_shell(self):
        with self.patch_ui, self.patch_host:
            handler = self._wired_handler()
            self.background._current = "X:/b.jpg"
            asyncio.run(handler(None))
            self.assertEqual(self.host.applied, ["X:/b.jpg"])

    def test_removal_pushes_empty_string(self):
        with self.patch_ui, self.patch_host:
            handler = self._wired_handler()
            self.background._current = ""
            asyncio.run(handler(None))
            self.assertEqual(self.host.applied, [""])

    def test_no_shell_yet_is_not_a_crash(self):
        """首屏那一下外壳还没装配：current_host() 是 None，这一跳要安静跳过。"""
        with self.patch_ui, mock.patch.object(navigation, "current_host", lambda: None):
            handler = self._wired_handler()
            asyncio.run(handler(None))

    def test_the_push_is_marshalled_not_run_inline(self):
        """改控件那一跳必须交给 :func:`call_on_ui` 编组，不能就地跑（契约 §5.2）。

        键订阅的处理器在 EngineHub 守护 loop 上，而 ``set_background`` 要碰控件。
        前面那条"推到外壳"把编组层摘成直连，所以**就地调用照样绿**——断的是线程，
        不是结果。这条把 call_on_ui 换成记账口：没编组=当场就改了控件，看得见。
        """
        handed: list = []
        other_loop = asyncio.new_event_loop()       # 存在、且不是当前跑着的那条
        try:
            with mock.patch.object(sys.modules["app.core.engine_worker"],
                                   "ui_loop", lambda: other_loop), \
                 mock.patch.object(sys.modules["app.core.engine_worker"],
                                   "call_on_ui", handed.append), \
                 self.patch_host:
                handler = self._wired_handler()
                asyncio.run(handler(None))
                self.assertEqual(len(handed), 1, "没把动作交给 call_on_ui")
                self.assertEqual(self.host.applied, [],
                                 "在服务循环上就地改了控件（编组这层是假的）")
                handed[0]()                          # 编组带的确实是要推外壳的那个闭包
        finally:
            other_loop.close()
        self.assertEqual(self.host.applied, ["X:/a.jpg"])

    def test_settings_service_missing_still_returns_initial_and_says_so(self):
        hub = mock.Mock()
        hub.require = mock.Mock(side_effect=KeyError("SettingsService 未注册"))
        hub.get = mock.Mock(return_value=self.background)
        with self.patch_ui, self.patch_host:
            with self.assertLogs(level="WARNING") as caught:
                self.assertEqual(self.main._wire_background(hub), "X:/a.jpg")
        self.assertTrue(any("重开程序" in line for line in caught.output),
                        "订阅没挂上等于'换了图看不到'，日志要说明这一点")

    def test_shell_build_carries_the_initial_image(self):
        """走真入口 :func:`build_app_shell`：形参收下了却没交给外壳 = 首屏没背景。

        这条照着 :class:`AppShellWiringTests` 的教训写——那里 ``shelf_probe`` 被
        形参吞掉、两条落地用例都直接构造宿主，所以生产从没生效而测试全绿。
        只验函数签名等于什么都没验。
        """
        page = _FakePage()
        saved_host = navigation._APP_HOST
        try:
            with mock.patch.object(navigation, "_APP_PAGE", None):
                root = navigation.build_app_shell(page, _RegHub(),
                                                  background=self.FIRST_PAINT)
            host = navigation.current_host()
        finally:
            navigation._APP_HOST = saved_host
        self.assertIsNotNone(host, "外壳没登记：装配根的订阅推不到任何地方")
        self.assertEqual(host.background, self.FIRST_PAINT)
        layer = _background_layer(root)
        self.assertIsNotNone(layer, "首屏根里没有那一层")
        self.assertEqual(layer.image.src, self.FIRST_PAINT)


if __name__ == "__main__":
    unittest.main()
