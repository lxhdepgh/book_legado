"""跨语言契约守卫：Dart 客户端与 Python 网关**不许各自漂**。

为什么要有这一支（设计 §4.1 / §6.3 / §10）：信封、错误码、窄窗阈值这三样今天各有两份
手写实现。flet 时代它们只有一份（Python），拆成两个进程之后第一次出现"同一件事写在
两种语言里"，而这类漂移的表现不是报错，是**客户端把"没连上"渲染成"书架是空的"**。

规矩：这一支只读 Dart 源文件里的**字面量**，不 import 任何东西到 Python 侧来"转换"——
一旦这里开始解析 Dart 的类型系统，守卫自己就成了第二个要维护的实现。
Python 侧那张表**从 AST/源文本抽**，不在这里手抄：手抄等于同一句话抄第三遍，
flet 改了字样两份抄本跟着改，比对就永远绿。

flutter_client 目录不存在时（比如 CI 只跑 Python 面）**跳过而不是失败**：
G1 之前这条守卫不该把全库回归拦住。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.gateway.methods import BY_ACTION
from app.gateway.protocol import ERROR_CODES, PROTOCOL_VERSION
from app.services.content.local_book_service import (
    LOCAL_BOOK_SOURCE_URL, is_local_source_url)
from app.themes.builtin_themes import BUILTIN_SKINS
from app.views.navigation import NARROW_WIDTH_THRESHOLD

ROOT = Path(__file__).resolve().parents[1]
DART_LIB = ROOT / "flutter_client" / "lib"


def _dart_files() -> list[Path]:
    if not DART_LIB.is_dir():
        return []
    return sorted(DART_LIB.rglob("*.dart"))


def _read(rel: str) -> str:
    path = DART_LIB / rel
    if not path.is_file():
        pytest.skip(f"没有 {path.relative_to(ROOT)}（G1 的 Dart 工程还不存在）")
    return path.read_text(encoding="utf-8")


def _literal(text: str, pattern: str, label: str) -> str:
    found = re.search(pattern, text)
    if found is None:
        pytest.fail(f"Dart 侧找不到 {label}（模式 {pattern!r}）——守卫要读的是字面量，"
                    "改名/挪文件请把这一支一起改")
    return found.group(1)


def test_flutter_client_exists_and_pinned_files_are_named() -> None:
    """正对照：守卫真的读到了源文件。

    没有这一条，上面所有 `_read` 都能"合理地"skip，于是这套守卫可以在
    Dart 工程整个被删掉之后仍然全绿——记忆里 `guard-tests-assert-absence` 说的
    就是这种"绿着的空仪器"。
    """
    files = _dart_files()
    assert files, f"{DART_LIB} 里没有 .dart 文件"
    for rel in ("core/rpc/envelope.dart", "app/shell.dart"):
        assert (DART_LIB / rel).is_file(), rel


def test_protocol_version_is_one_number_two_places() -> None:
    dart = _literal(_read("core/rpc/envelope.dart"),
                    r"const int kProtocolVersion\s*=\s*(\d+)", "kProtocolVersion")
    assert int(dart) == PROTOCOL_VERSION, (
        f"Dart kProtocolVersion={dart} 而 Python PROTOCOL_VERSION={PROTOCOL_VERSION}；"
        "版本不符时客户端会拒启动，两侧必须一起提")


def _enum_names(body: str) -> list[str]:
    """枚举常量表：只取 `enum X { … ;` 到**第一个分号**之前那一段。

    分号之后是成员（`wireName` 里那个 switch），非贪婪地取到第一个 `}` 会把
    switch 的分支一起当成常量——`_ => name` 就是这么混进来的。
    """
    start = body.index("enum EngineFailureKind")
    region = body[start:body.index(";", start)]
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", region)
    return [n for n in names if n not in {"enum", "EngineFailureKind"}]


def test_error_code_vocabulary_matches() -> None:
    """六种码：Python 表里的每一格都得在 Dart 枚举里，且线上拼写一致。

    Dart 侧多出来的 `transport` 是**故意的**（服务端根本没机会回信封的那些情况），
    所以这里只朝一个方向比，并单独钉住"多出来的只有 transport"。
    """
    names = _enum_names(_read("core/rpc/envelope.dart"))
    assert len(names) == len(set(names)), f"枚举常量重复：{names}"
    # 正对照：仪器接上了才谈得上"缺一格就是红"。
    assert "transport" in names, f"枚举常量没读全，实际读到 {names}"

    # wireName：badRequest → bad_request，其余同名。守卫自己复刻这一条规则，
    # 是因为它就是 Dart 里那行 switch 的语义；下一支单独钉那条 switch 的存在。
    wire = {n.replace("badRequest", "bad_request") for n in names}
    assert set(ERROR_CODES) <= wire, set(ERROR_CODES) - wire
    extra = wire - set(ERROR_CODES)
    assert extra == {"transport"}, f"Dart 侧多出的码必须只有 transport，实际 {extra}"


def test_bad_request_wire_spelling_is_written_out_in_dart() -> None:
    """`bad_request` 这个线上拼写必须**以字符串形式**存在于 Dart。

    上一支自己复刻了命名规则，所以它照不出 Dart 那行 switch 被删掉的情况——
    删掉之后 Dart 会发出 `badRequest`，服务端表外降级成 engine。这一支读的是那个串。
    """
    body = _read("core/rpc/envelope.dart")
    assert "'bad_request'" in body, "Dart 的 wireName 里必须有 'bad_request' 这个字面量"
    assert "EngineFailureKind.badRequest =>" in body, "wireName 的分支写法变了"


def test_narrow_width_threshold_is_one_number_two_places() -> None:
    dart = _literal(_read("app/shell.dart"),
                    r"const double kNarrowWidthThreshold\s*=\s*([\d.]+)",
                    "kNarrowWidthThreshold")
    assert float(dart) == NARROW_WIDTH_THRESHOLD, (
        f"Dart={dart} 而 Python NARROW_WIDTH_THRESHOLD={NARROW_WIDTH_THRESHOLD}；"
        "同一份书单在两边不是同一个形状，R4 那组对照数就作废")


def test_bookshelf_row_wording_is_read_from_python_into_dart() -> None:
    """书架那三处文案（副标题的连法、"读到 X"前缀、状态行那一句）**以 flet 源码为准**。

    为什么单独立一支：G1 的整条比对链是 `.probe/g1_flet_projection.py` 抄 flet 的
    `_row`/`_load`，Dart 也抄同一段，测试再拿两边对两边。flet 那边改了字样而这两份
    拷贝没跟上，比的就不是同一件事——而表现是**全绿**：靶子和代码一起动，误差被
    消掉了。这一支读的是 flet 源文件里的字面量，Python 改了它就把 Dart 一起拽红。

    方向也是定的：先从 Python 抽，再要求 Dart 含同一个串。抽不到就 fail，
    不许"抽不到 = 这项不查"（记忆里 `guard-tests-assert-absence` 那一族）。
    """
    view = (ROOT / "app/views/reading/bookshelf_view.py").read_text(encoding="utf-8")
    controller = (ROOT / "app/controllers/bookshelf_controller.py")\
        .read_text(encoding="utf-8")

    separator = _literal(view, r'subtitle = "([^"]*)"\.join', "副标题的分隔符")
    read_prefix = _literal(view, r'f"([^"{]*)\{chapter_name\}"', "副标题的读到前缀")
    # `_literal` 只回第一组，所以状态行那一句的两头分两次抽。
    status_open = _literal(controller, r'set_status_text\(f"([^"{]*)\{',
                           "状态行的前半句")
    status_close = _literal(controller, r'set_status_text\(f"[^"{]*\{[^}]*\}([^"{]*)"\)',
                            "状态行的后半句")

    item = _read("features/bookshelf/data/bookshelf_item.dart")
    screen = _read("features/bookshelf/presentation/bookshelf_screen.dart")
    assert f"'{separator}'" in item, (
        f"flet 的 `_row` 用 {separator!r} 连接副标题，Dart 的 `subtitle` 里没有这个串")
    assert f"'{read_prefix}$" in item, (
        f"flet 那一格拼的是 {read_prefix!r}+章名，Dart 拼的前缀不是这一串")
    # 状态行只比两头：中间那个"多少本"在两边各自是 len(self._records) 与 items.length，
    # 拿整串来比就成了要求两种语言用同一个变量名。
    assert f"'{status_open}${{" in screen and f"}}{status_close}'" in screen, (
        f"flet 的状态行是 {status_open!r}…{status_close!r}，Dart 那屏不是这一句")


_DART_EXTENSIONS = (".dart", ".py", ".json", ".yaml", ".site")


def _action_literals(text: str) -> list[str]:
    """只扫**调用位上的** action 字面量（``.call('…'`` 那一族），其余一律不算调用点。

    这一条口径在第八十九轮（G3-C3）从"认字面量、排掉几种邻居"改成"只认调用位"，
    因为要排掉的邻居一直在长：

    - `import 'router.dart'`：文件名也是 `小写.小写`（后缀排掉，这一条留着，
      调用位上也可能会出现带后缀的字面量）；
    - `Key('bookshelf.refresh')`：控件键与 action 同形；
    - `'reader.open': 60`：`action_budgets.dart` 那张**预算表**——G3-B 现场抓到的：
      只登记、界面还没接的动作一进预算表就被读成"客户端在调"，
      "每一发都有人调"那半边当场失去牙齿（记忆 `measurement-corpus-must-not-be-self-proving`
      的同形：仪器自己造出了它要读的那个信号）；
    - `heroTag: 'reader.prev'`（G3-C3 的阅读屏： FloatingActionButton 要 heroTag，
      它与控件键同值，但不长在 `Key(` 后面，那一条前缀排不掉）；
    - `static const String kFontSizeMode = 'reader.font_size'`（同轮：排版偏好的
      两个**模式名**，Dart 侧自己的命名空间，压根不过协议面）。

    后三条的形状不同、道理相同：**字面量出现在声明位/命名参数位就不是调用点**。
    逐个加前缀是打地鼠（`Key(` 那条已经被 `heroTag` 顶穿一次），改成"必须跟在调用动词
    之后"是一句话的口径，而且它更贴判据本来的问法——问的是客户端**在不在调**这一发。

    **段里允许下划线**（`source.batch_enable`）：G2-E 补的——原来那版只认 `[a-zA-Z]*`，
    两条带下划线的动作对扫描器完全隐形，读出来是"没调"，而 Dart 那边其实早就在调。

    代价写在这里：谁把动作名提成常量再 `call(kAction)`，这一版就看不见它——那一条
    会当"没人调"报红。这是有意的：调用点要能被这张表数出来，就得留在字面量那一档。
    """
    shape = re.compile(r"(?:\bcall\(|callFor\(|_call\(|invoke\()\s*"
                       r"'([a-z][a-zA-Z_]*\.[a-zA-Z][a-zA-Z_0-9]*)'")
    return [m for m in shape.findall(text)
            if not any(m.endswith(ext) for ext in _DART_EXTENSIONS)]


def test_widget_key_lookalikes_are_not_mistaken_for_actions() -> None:
    """"只认调用位"这只手必须**窄**到只认调用位：五种同形邻居都排掉，真调用点照扫。

    正对照：拿一行真调用点喂同一个解析器，它必须被扫出来。否则"排邻居"其实是
    "整个不扫"，前一支就永远绿着（记忆里 `guard-tests-assert-absence` 那一族）。

    第 4 行 `source.batch_enable` 是 G2-E 的形状（段里带下划线）；第 5-6 行是 G3-B 的
    预算表 + 同一发动作被真调（这行是这一支的牙齿：排掉预算表不许顺手把 reader 整族排掉）；
    第 7-8 行是 G3-C3 阅读屏带来的两种新形状——`heroTag` 顶穿过 `Key(` 那条前缀，
    模式常量 `kFontSizeMode` 是第三种，两者都必须在"只认调用位"这一条口径下自动出局。
    """
    sample = ("  final a = client.call('bookshelf.list');\n"
              "  key: const Key('bookshelf.refresh'),\n"
              "  import 'router.dart';\n"
              "  await client.call('source.batch_enable', payload: x);\n"
              "  'reader.open': 60,\n"
              "  await client.call('reader.open', payload: x);\n"
              "  heroTag: 'reader.prev',\n"
              "  static const String kFontSizeMode = 'reader.font_size';\n")
    assert _action_literals(sample) == [
        "bookshelf.list", "source.batch_enable", "reader.open"], _action_literals(sample)


#: 协议面先落地、Dart 侧还没人落的那几发：每条**必须写明是哪一门欠的**。
#:
#: 这一张表是"欠据"，不是逃生口——下面那条判据比的是**键集相等**，所以
#: ① 新加动作既没接 Dart 又不具名 → 红；② Dart 接上了却没把这一格划掉
#: → 一样红。两头都咬，欠据就悄悄不成长期账单。
#:
#: 第八十八轮（G3-C2）把那 13 条 reader 欠据划完了；第九十轮（G3-D1）把
#: `engine.webContentReady` 从"反向通道例外"那一张表里挪了出来——它的 Dart 调用点在
#: `core/webview/webview_host.dart` 那条链上（宿主答回去用的就是这一发），
#: 所以它现在与其余各发一样**必须有调用点**，那张例外表删掉了。
#: 口径提醒：这里数的"Dart 侧有人落"是**协议面在 Dart 有实现**（`core/rpc/` 那三个文件
#: 里的字面量），不是"界面调了它"；后者由 widget/golden 测试那一面负责（G3-C3）。
#:
#: 第九十三轮（G3-D3）把换源那一族五发登记上表，第九十四轮在同一门的第二发提交里划掉：
#: 五个字面量都落在 `core/rpc/rpc_engine_repository.dart`（当场数过：58 发全部有调用点，
#: `never_called` 为空），换源屏那一面另有 `test/source_switch_screen_test.dart` 接住。
#: 划掉的判据是 `core/rpc/` 里出现这五个字面量，不是"屏幕看着像"。
#: 第九十六轮（G4-B 前半）把订阅面那九发登记上表，第九十七轮在同一门的后半发里划掉：
#: 同样是九个字面量都落在 `core/rpc/rpc_engine_repository.dart` 的九个方法里（当场数过：
#: 58 发全部有调用点），界面那一面另有 `test/rss_screen_test.dart` + `rss_projection_test.dart`。
#: 表现在是空的，而**判据照跑**——它的作用是下一门（漫画媒体 / 朗读 / 工具域）
#: 落地时的同一张嘴，不是历史装饰。
_UNWIRED_PENDING_GATE: dict[str, str] = {}


def _dart_action_calls() -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for path in _dart_files():
        for name in _action_literals(path.read_text(encoding="utf-8")):
            used.setdefault(name, []).append(
                str(path.relative_to(DART_LIB)).replace("\\", "/"))
    return used


def test_action_table_and_dart_callers_cover_each_other() -> None:
    """两个方向都查：客户端**只调登记过的**，登记过的**每一发都有客户端在调**。

    前半个方向：表外动作在服务端回 `bad_request`，用户看到的是"点了没反应"。
    后半个方向是 G2-E 才补上的，查"服务端建好了、Dart 一行都没调"——
    那种动作不会报错，只会让某一屏永远缺一块（`refactor-regression-orphan-methods`
    的同一条：接线断了，测试全绿）。今天允许的例外只有一类，而且必须**具名**：
    `_UNWIRED_PENDING_GATE` 那几张欠据（协议面先行、写明哪一门来接）。
    加动作时忘了接界面，这里当场红。
    """
    used = _dart_action_calls()
    assert used, "Dart 里一个 action 都没扫到——仪器没接上"
    unknown = {name: where for name, where in used.items() if name not in BY_ACTION}
    assert not unknown, f"客户端在调用没登记过的动作：{unknown}"

    never_called = sorted(set(BY_ACTION) - set(used))
    pending = sorted(_UNWIRED_PENDING_GATE)
    # 这一条是**双向**咬的：动作从表上没了、欠据还挂着，或者界面接上了、欠据没划掉，
    # 两个方向都会让这两个集合不相等（后一个方向的牙齿由变异核验钉着，
    # 见 ``.probe/g3b_mutation_check.py`` 的"表上删掉一条 reader 动作"那一发）。
    assert never_called == pending, (
        f"这些动作登记在服务端却没有 Dart 调用点，与欠据表不符：{never_called}\n"
        f"（该在的是 {pending}；接上界面就把 _UNWIRED_PENDING_GATE 里那一格划掉，"
        f"加了动作还没接就补一张具名欠据）")


#: Dart 那条默认水池（`RpcClient.timeout` 的默认值）。表外的动作都走它。
_DART_DEFAULT_POOL_SECONDS = 30


def _dart_default_pool_seconds() -> int:
    body = _read("core/rpc/rpc_client.dart")
    found = re.search(r"this\.timeout = const Duration\(seconds: (\d+)\)", body)
    if found is None:
        pytest.fail("Dart 侧读不到 RpcClient 的默认预算——构造签名变了，"
                    "守卫与 `action_budgets.dart` 的那句“表外走默认水池”一起失效")
    return int(found.group(1))


def test_client_budget_covers_exactly_the_long_actions() -> None:
    """客户端预算表必须**正好**覆盖服务端预算比默认水池长的那些动作，秒数逐条相等。

    漏一条长的：客户端先到点，用户看到"超时"而引擎还在后面把活干完并落库——
    于是"导入失败"的屏底下其实导入了（`action_budgets.dart` 文件头那条）。
    多一条短的：只是白等，不红；所以这里比的是**键集相等**，不是子集。
    """
    pool = _dart_default_pool_seconds()
    assert pool == _DART_DEFAULT_POOL_SECONDS, (
        f"Dart 默认水池是 {pool}s 而守卫按 {pool} 反推服务端表，"
        "改了水池请连这条一起改（它是判据的前提，不是判据本身）")

    expected = {action: int(method.timeout)
                for action, method in BY_ACTION.items()
                if method.timeout > pool}
    assert expected, "服务端一条比默认水池长的动作都没有——这条守卫成了空仪器"

    body = _read("core/rpc/action_budgets.dart")
    got = dict((re.findall(r"'([a-z][a-zA-Z_]*\.[a-zA-Z_]+)':\s*(\d+)", body)))
    got = {action: int(seconds) for action, seconds in got.items()}
    assert got == expected, (f"客户端预算表与服务端 timeout 列对不上：\n"
                             f"  少：{sorted(set(expected) - set(got))}\n"
                             f"  多：{sorted(set(got) - set(expected))}\n"
                             f"  秒数不同：{sorted((a, got[a], expected[a]) for a in set(got) & set(expected) if got[a] != expected[a])}")


# -------------------------------------------------------------------- #
# 外观那一屏的五张表：皮肤 / 模式 / 哨兵 / 检测九态 / 事件名
# -------------------------------------------------------------------- #

_SKIN_ENTRY = re.compile(
    r"AppSkin\(\s*id:\s*'([^']+)',\s*"
    r"name:\s*'([^']+)',\s*"
    r"description:\s*'([^']+)',\s*"
    r"primary:\s*0x[0-9A-Fa-f]{2}([0-9A-Fa-f]{6})\)")


def test_skin_table_is_one_list_two_places() -> None:
    """五格皮肤：id、显示名、说明、主色**和顺序**都只许有一份。

    顺序也算内容：flet 那个下拉按 `BUILTIN_SKINS` 的声明序建
    （`appearance_view.py:116-117`），换序等于换用户第一眼看到的皮肤。
    主色两边不是同一种写法（`#3F51B5` 对 `0xFF3F51B5`），所以比的是去掉 alpha 的六位。
    """
    dart = _read("features/settings/data/theme_preferences.dart")
    got = _SKIN_ENTRY.findall(dart)
    assert got, "Dart 侧一条皮肤都没抽到——`kAppSkins` 的写法变了，仪器没接上"

    want = [(skin.id, skin.name, skin.description,
             skin.light.primary.lstrip("#").upper())
            for skin in BUILTIN_SKINS.values()]
    assert [(i, n, d, p.upper()) for i, n, d, p in got] == want, (
        f"Dart kAppSkins 与 Python BUILTIN_SKINS 不一致：\n"
        f"  Dart={got}\n  Python={want}")


def test_theme_mode_wiring_and_labels_match_appearance_view() -> None:
    """三个模式：线上值与中文标签都照 `appearance_view.py` 的那三行 Radio。

    这一屏最坏的漂移不是标签错，是**线上值**错——`mode` 存进设置的是那三个串，
    Dart 发 `dark-mode` 之类过去，服务端表外归一成 system，用户看到的是"切深色没反应"。
    """
    view = _py_source("app/views/settings/appearance_view.py")
    pairs = re.findall(r'ft\.Radio\(value="([^"]+)", label="([^"]+)"\)', view)
    assert len(pairs) == 3, f"appearance_view 的模式 Radio 不是三条，读到 {pairs}"

    enum_body = _read("features/settings/data/theme_preferences.dart")
    region = enum_body[enum_body.index("enum ThemeModeKind"):]
    region = region[:region.index(";")]
    names = [n for n in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", region)
             if n not in {"enum", "ThemeModeKind"}]
    assert sorted(names) == sorted(value for value, _ in pairs), (
        f"Dart 的 ThemeModeKind 常量与 flet 的 Radio value 不同一套：{names} vs {pairs}")

    for value, label in pairs:
        assert f"ThemeModeKind.{value} => '{label}'," in enum_body, (
            f"flet 那一格写的是 {value!r}→{label!r}，Dart 的 label 分支不是这一句")


def _py_source(rel: str) -> str:
    path = ROOT / rel
    if not path.is_file():
        pytest.fail(f"flet 侧源文件不见了：{rel}（守卫读的是它的字面量，挪了请一起改）")
    # **utf-8-sig 不是讲究**：`app/` 底下当场数过有 8 份文件带 BOM（`app/views/reading/`
    # 那一族全在里面）。按 utf-8 读会把 U+FEFF 留在第一行开头，`ast.parse` 当场
    # SyntaxError——那条红看起来像"flet 源坏了"，其实是仪器读错了字节序。
    return path.read_text(encoding="utf-8-sig")


def _cjk_literals_from(node: ast.AST) -> list[str]:
    '''从一个 AST 节点往下抽所有**含汉字**的字符串常量（出现序、去重）。

    **裸表达式**位置的字符串常量一律不收：那是 docstring（函数自己的、以及里面
    嵌套函数/类的），不是要上屏的话。只按"含不含换行"过滤是不够的——
    `"""把 pending 持久化到 SettingsService。"""` 就是单行的，第一次跑就撞上去了。
    （这一份 docstring 用三单引号就是被这一行逼的：例子里那对三双引号会提前收掉它。）
    '''
    docstring_ids = {id(child.value) for child in ast.walk(node)
                     if isinstance(child, ast.Expr)
                     and isinstance(child.value, ast.Constant)
                     and isinstance(child.value.value, str)}
    out: list[str] = []
    for const in (n for n in ast.walk(node)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)):
        value = const.value
        if id(const) in docstring_ids:
            continue
        if re.search(r"[一-鿿]", value) and value not in out:
            out.append(value)
    return out


def _cjk_literals_in_function(py_rel: str, func: str) -> list[str]:
    """某个 Python 函数里所有**含汉字**的字符串常量（出现序、去重）。

    从 AST 抽而不是在这里手写：手抄就是把同一句话抄第三遍，
    flet 改了字样两份抄本一起跟着改，比对永远绿（记忆里 `guard-tests-assert-absence`）。
    """
    tree = ast.parse(_py_source(py_rel))
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == func), None)
    if node is None:
        pytest.fail(f"{py_rel} 里没有函数 {func}——守卫要读的位置挪了")
    return _cjk_literals_from(node)


def _cjk_literals_in_module(py_rel: str) -> list[str]:
    """整份模块的字面量：flet 那些对话框把文案写在控件构造处，没有函数可点名。"""
    return _cjk_literals_from(ast.parse(_py_source(py_rel)))


def _dart_literals(rel: str) -> str:
    """Dart 文件里**字符串字面量的内容**（注释一律剥掉），每条一行。

    为什么不能拿整份文件当文本比 substring：注释里会复述上屏的话
    （`/// 还欠着没补验正文的源条数。`），于是界面那句真串被改错了，注释还留着原话，
    守卫照样绿。第一发变异就是这么打不红它的——仪器读错了对象，不是读到了 0 条。
    """
    src = _read(rel)
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch == "/" and src[i + 1:i + 2] == "/":
            newline = src.find("\n", i)
            i = n if newline == -1 else newline
            continue
        if ch == "/" and src[i + 1:i + 2] == "*":
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch in ("'", '"'):
            quote = ch * 3 if src[i:i + 3] in ("'''", '"""') else ch
            body, j = [], i + len(quote)
            while j < n:
                if src[j] == "\\":
                    body.append(src[j:j + 2])
                    j += 2
                    continue
                if src.startswith(quote, j):
                    break
                body.append(src[j])
                j += 1
            out.append("".join(body))
            i = j + len(quote)
            continue
        i += 1
    assert out, f"{rel} 里一个字符串字面量都没抽到——扫描器坏了，别信这条守卫的绿"
    return "\n".join(out)


def _cjk_runs(literals: list[str]) -> list[str]:
    """把片段切成**连续汉字串**（≥2 字），按出现序去重。

    为什么切：相邻的 f-string 会被解析器并成一个常量（`f"…{total}，"` 与
    `f"阻断 {…}"` 并出 `，阻断 `），而 Dart 那边同一句话写在两个字面量里，
    整片段逐字比会在"缝"上假红。切成语词再比，查的是**用词**漂移
    （"补验正文"改成"深检"就红）；整句逐字那关在投影表那一臂里做
    （同一份载荷、两边各渲染一次，见 `tests/gateway/projection/`）。
    """
    runs: list[str] = []
    for piece in literals:
        for run in re.findall(r"[\u4e00-\u9fff]{2,}", piece):
            if run not in runs:
                runs.append(run)
    return runs


def test_check_summary_line_uses_the_same_words_in_both_languages() -> None:
    """检测收尾那一句的**每个语词**都得在 Dart 的那一句里出现（用词表由 Python 的 AST 给）。

    flet 那句是 `检测完成：可用 U/T，阻断 B（快检 S 源、补验正文 D 源，待深检 P 源）`，
    四个数各来自不同的表（summary / tiering / deep_queue），所以整串比不了；
    这里比语词：`_summary_line` 改一个字，Dart 那一屏当场红。
    **整句逐字**的比对在投影表那一臂（同一份载荷两边各渲染一次）。

    比的是 Dart 的**字面量**而不是整份文件：变异核验第一发就撞见这句——
    `check_models.dart:137` 的注释里也写着"补验正文"，拿整文件 substring 比，
    把界面那句改错了注释还替它顶着，守卫不响。
    """
    fragments = _cjk_literals_in_function(
        "app/controllers/source/check_controller.py", "_summary_line")
    words = _cjk_runs(fragments)
    assert len(words) >= 4, (f"从 `_summary_line` 只抽到 {fragments} → {words}——"
                             "抽得太少就是仪器没接上，这条守卫不许靠“少抽”变绿")

    dart = _dart_literals("features/check/data/check_models.dart")
    missing = [word for word in words if word not in dart]
    assert not missing, (f"flet 的 `_summary_line` 用这些词，Dart 那一句里没有：{missing}\n"
                         "改文案要两边各改一次，漏一边就是同一轮检测在两个界面说两种话")


def test_scope_sentinels_and_labels_are_one_table_two_places() -> None:
    """两个哨兵的线上值 + 它们的中文标签：服务端定值，客户端只定显示。

    哨兵拼错的后果不是报错：分组名里确实没有 `__ungrouped`（少两个下划线）那种组，
    于是"未分组"要么变成"一个都不搜"，要么变成"全部都搜"。
    """
    reading = _py_source("app/services/reading/reading_service.py")
    # 名字 → 线上值。`_SCOPE_LABELS` 的键是**常量名**，不是那两条串，所以先按定义表换算。
    consts = dict(re.findall(r'^(_SCOPE_[A-Z_]+) = "([^"]+)"', reading, flags=re.M))
    assert {'_SCOPE_ALL', '_SCOPE_UNGROUPED'} <= set(consts), (
        f"reading_service 里的哨兵常量抽不全，读到 {sorted(consts)}")
    sentinels = (consts['_SCOPE_ALL'], consts['_SCOPE_UNGROUPED'])

    dart = _read("features/find_books/data/search_scope.dart")
    assert f"const String kScopeAll = '{sentinels[0]}';" in dart, (
        f"服务端的“全部”哨兵是 {sentinels[0]!r}，Dart 的 kScopeAll 不是这一串")
    assert f"const String kScopeUngrouped = '{sentinels[1]}';" in dart, (
        f"服务端的“未分组”哨兵是 {sentinels[1]!r}，Dart 的 kScopeUngrouped 不是这一串")

    labels = _scope_label_table()
    assert sorted(labels) == ['_SCOPE_ALL', '_SCOPE_UNGROUPED'], (
        f"find_books_controller 的 _SCOPE_LABELS 键不是那两条哨兵名：{sorted(labels)}")
    resolved = {consts[name]: text for name, text in labels.items()}
    assert sorted(resolved) == sorted(sentinels)
    for value, text in resolved.items():
        assert f"=> '{text}'" in dart, (
            f"哨兵 {value!r} 的中文标签 {text!r} 在 Dart 的 `label` 分支里找不到")

    # 范围那一行的**排版**：flet 拼的是 `f"{label}（{count} 源）"`
    # （`find_books_controller.py:293`）。全角括号、括号里那个空格、以及"源"字前
    # 不带"条"，都是抄的人会顺手改掉的地方，而上面三条查的是哨兵值与标签名，
    # 一句排版都不查。方向照旧：先从 Python 抽，再把 f-string 的插值名换成 Dart 的，
    # 要求 Dart 的 `rowLabel` 里含同一串。
    controller_src = _py_source("app/controllers/find_books_controller.py")
    row_format = _literal(controller_src, r'f"(\{label\}（\{count\}[^"]*)"',
                          "范围那一行的拼法")
    as_dart = (row_format.replace('{label}', '$label')
               .replace('{count}', '$sourceCount'))
    literals = _dart_literals("features/find_books/data/search_scope.dart")
    assert as_dart in literals, (
        f"flet 那一行拼的是 {row_format!r}（{as_dart!r}），"
        f"Dart 的 `rowLabel` 不是这一句；Dart 里带“源”的字面量是 "
        f"{[line for line in literals.splitlines() if '源' in line]}")


def _scope_label_table() -> dict[str, str]:
    """从 `find_books_controller` 的源码里把 `_SCOPE_LABELS` 那张字典抽出来。

    键是 `_SCOPE_ALL` / `_SCOPE_UNGROUPED` 两个**名字**（不是字符串），所以不能
    `literal_eval`；这里按 ast.Dict 逐格取"键名 + 值字面量"。
    """
    tree = ast.parse(_py_source("app/controllers/find_books_controller.py"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "_SCOPE_LABELS"
                   for t in node.targets):
            continue
        table: dict[str, str] = {}
        for key, value in zip(node.value.keys, node.value.values):
            assert isinstance(key, ast.Name) and isinstance(value, ast.Constant), \
                "_SCOPE_LABELS 的形状变了（键应为哨兵名、值应为字符串）"
            table[key.id] = value.value
        return table
    pytest.fail("find_books_controller 里没有 _SCOPE_LABELS——范围标签表挪走了")


def _module_dict(py_rel: str, name: str) -> dict[str, str]:
    """模块级 `NAME = {...}` 的字符串字典（用 literal_eval，键值都得是字面量）。"""
    tree = ast.parse(_py_source(py_rel))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    pytest.fail(f"{py_rel} 里没有模块级 {name}——守卫要读的表挪走了")


def _returned_strings(py_rel: str, func: str) -> list[str]:
    """函数**返回位置**上的字符串常量。

    `IfExp` 只看两个分支、不看条件，集合字面量整个不看：`_classify_result` 里
    `return "content_unavailable" if reading not in {"skip", None} else (…)`
    那句，条件里的 `skip` 是**输入状态**不是结论，把它当结论抽出来，
    下一支就会要求标签表给 `skip` 配一句中文——那是假红。
    """
    tree = ast.parse(_py_source(py_rel))
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == func), None)
    if node is None:
        pytest.fail(f"{py_rel} 里没有函数 {func}——守卫要读的位置挪了")

    out: list[str] = []

    def walk(value: ast.expr) -> None:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            out.append(value.value)
        elif isinstance(value, ast.IfExp):
            walk(value.body)
            walk(value.orelse)
        elif isinstance(value, ast.BoolOp):
            for item in value.values:
                walk(item)

    for statement in ast.walk(node):
        if isinstance(statement, ast.Return) and statement.value is not None:
            walk(statement.value)
    return [s for s in out if re.fullmatch(r"[a-z][a-z_]*", s)]


def test_check_state_vocabulary_is_one_set_three_places() -> None:
    """检测结论那一套词汇：**分类器返回的集合 = flet 的标签表 = Dart 的标签表**。

    三份今天都存在，而最坏的那种漂移是**第四种**：分类器新加一档而两边标签表没有它，
    于是那一档在界面上直接显示英文 `state_class`（flet 与 Dart 都是 `表外回原值`，
    故意如此）。所以这里查的是"表里有的档，分类器还在返回吗"（多余=死条目）
    与"分类器返回的档，表里都有吗"（缺=界面漏一档），两个方向都查。
    """
    emitted = set(_returned_strings("app/services/check_service.py", "_classify_result"))
    assert len(emitted) >= 6, (f"从 `_classify_result` 只抽到 {sorted(emitted)}——"
                               "抽得太少就是仪器没接上")

    labels = _module_dict("app/views/source/check_view.py", "CHECK_STATE_LABELS")
    assert set(labels) == emitted, (
        f"flet 的标签表与分类器返回的集合不同：\n"
        f"  表里有而分类器不返回（死条目）：{sorted(set(labels) - emitted)}\n"
        f"  分类器返回而表里没有（界面显示英文）：{sorted(emitted - set(labels))}")

    dart = _read("features/check/data/check_models.dart")
    region = dart[dart.index("kCheckStateLabels"):]
    region = region[:region.index("};")]
    got = dict(re.findall(r"'([a-z_]+)':\s*'([^']+)'", region))
    assert got == labels, (
        f"Dart 的 kCheckStateLabels 与 flet 的 CHECK_STATE_LABELS 不一致：\n"
        f"  Dart 多：{sorted(set(got) - set(labels))}  Dart 缺：{sorted(set(labels) - set(got))}\n"
        f"  同一档两种说法：{sorted((k, got[k], labels[k]) for k in set(got) & set(labels) if got[k] != labels[k])}")


def test_dart_event_names_are_classes_the_engine_publishes() -> None:
    """事件名 = `app/core/events.py` 那些 dataclass 的类名，一个字母都不许多。

    名字打错的后果不是红屏，是**那一屏永远不出结果**：监听器在等
    `SearchFinshedEvent`，流里来的是 `SearchFinishedEvent`，两边都不报错。
    """
    dart = _read("core/events/event_names.dart")
    names = re.findall(r"static const String \w+ = '([^']+)'", dart)
    assert names, "Dart 侧一条事件名都没抽到——仪器没接上"

    published = set(re.findall(r"^class (\w+Event)\b",
                               _py_source("app/core/events.py"), flags=re.M))
    assert len(published) >= 10, f"从 events.py 只抽到 {len(published)} 个事件类，抽得太少"
    typo = sorted(set(names) - published)
    assert not typo, (f"Dart 在听引擎从来不发的事件：{typo}\n"
                      f"（引擎那边现有的：{sorted(published)}）")


def test_appearance_status_lines_are_one_sentence_two_places() -> None:
    """保存/恢复默认那两句状态行 + 三个模式标签 + 墨水屏标签，都以 flet 源码为准。

    范围是**点名**的：只比 `AppearanceController.save` 与 `.reset` 这两个函数里的句子。
    背景图那三条（"背景图已换上…""已去掉背景图…""已取消（没选图片）"）故意不在内——
    协议面上没有"换背景图"那一发（`theme.preferences` 只有 `{skin_id, mode, eink}` 三格），
    Dart 那一屏在文件头写明了"没接"，比它等于要求两边都有同一件不存在的功能。

    "已恢复默认外观（保存后生效；背景图不受影响）"这一句里同时讲了**两件事**
    （要按保存才落库、背景图不参与），少了括号那一段用户就会以为背景也被清了。
    """
    runs: list[str] = []
    for func in ("save", "reset"):
        pieces = [p for p in
                  _cjk_literals_in_function(
                      "app/controllers/settings/appearance_controller.py", func)
                  # "XXService 未就绪"讲的是服务可用性，Dart 那边由信封错误码承担。
                  if "未就绪" not in p]
        runs += [word for word in _cjk_runs(pieces) if word not in runs]
    assert len(runs) >= 4, f"从 save/reset 两个函数只抽到 {runs}——仪器没接上"

    dart = _dart_literals("features/settings/domain/appearance_notifier.dart")
    missing = sorted(word for word in runs if word not in dart)
    assert not missing, f"外观那两句在 Dart 里对不上：{missing}\n（抽到的语词：{runs}）"

    view = _py_source("app/views/settings/appearance_view.py")
    switch_label = _literal(view, r'ft\.Switch\(\s*label="([^"]+)"', "墨水屏开关的标签")
    assert f"'{switch_label}'" in _read(
        "features/settings/presentation/appearance_screen.dart"), \
        f"flet 的开关写着 {switch_label!r}，Dart 那一格不是这一句"


#: 换源那一族里**不上屏**的那两句：flet 只把它们写进日志，界面看不到。
#:
#: 为什么单列而不是直接从比对里漏掉：Dart 侧同一分支走的是「换源失败：<原因>」那一句
#: （前缀来自 `_run_switch`/`_do_switch` 都有的 `换源失败：`），加上横幅一条声音。
#: 所以这两句确实不需要 Dart 复刻——但"不需要"要有名字，不然下一个人只会把它们当成
#: 抽漏了，然后顺手把整个函数从扫描面里删掉。
_SWITCH_LOGGER_ONLY_SENTENCES = {"换源种子获取失败", "换源写回失败"}


def test_source_switch_wording_is_read_from_flet_into_dart() -> None:
    """换源那一屏每句上屏的话，用词表由 flet 的 AST 给，Dart 那两份文件必须都接住。

    扫两处而不是只扫对话框：flet 把句子拆在**两个文件**里——控件形状与按钮标签在
    `app/views/reading/source_switch_dialog.py`，而所有状态句在
    `app/controllers/bookshelf_controller.py` 的 `_seed_switch`/`_start_switch`/
    `_run_switch`/`_stop_switch`/`_do_switch` 五发里（`正在扫描 i/N 个书源` 那一句就带
    四个数各来自不同表，与 `_summary_line` 同一族）。

    不在扫描面的那一支也写明：`_confirm_switch` 的「该候选的来源书源已不在列表中，
    无法换源」是 flet **独有**的分支——它手里握着源对象，取不取得到当场就知道；
    跨进程这一侧取不到时服务端是**抛**（`switch.apply` 的三种成因之一），Dart 说的是
    「换源失败：<原因>」。把这一句要求进 Dart，等于要求客户端预知服务端的判断。

    比的是 Dart 的**字面量**而不是整份文件：注释里会复述上屏的话，拿整文件 substring 比，
    界面那句改错了注释还替它顶着（`_dart_literals` 那份说明写的就是这一发）。
    """
    pieces = _cjk_literals_in_module("app/views/reading/source_switch_dialog.py")
    for func in ("_seed_switch", "_start_switch", "_run_switch", "_stop_switch",
                 "_do_switch"):
        for piece in _cjk_literals_in_function(
                "app/controllers/bookshelf_controller.py", func):
            if piece not in pieces:
                pieces.append(piece)
    runs = _cjk_runs(pieces)

    # 排除表必须是**活配置**：这句话在 flet 侧不存在时，它就不是"排除"，是死条目。
    vanished = sorted(_SWITCH_LOGGER_ONLY_SENTENCES - set(runs))
    assert not vanished, (f"排除表里的 {vanished} 在 flet 侧已经抽不到了——"
                          "那句要么改名了要么删了，这张表要跟着改，不许留着当装饰")

    kept = [word for word in runs if word not in _SWITCH_LOGGER_ONLY_SENTENCES]
    # 第九十四轮当场数过：整批 23 个语词，去掉那两句 logger 的是 21 个。
    # 余量留 3：五个函数名里丢掉任意一个都会跌破 18（`_run_switch` 自己带 6 个）。
    assert len(kept) >= 18, (f"只抽到 {len(kept)} 个语词（当场测得的是 21）——"
                             "扫描面缩了就是仪器没接上，这条守卫不许靠“少抽”变绿")

    dart = (_dart_literals("features/bookshelf/data/source_switch_models.dart")
            + "\n"
            + _dart_literals("features/bookshelf/presentation/source_switch_dialog.dart"))
    missing = sorted(word for word in kept if word not in dart)
    assert not missing, (f"flet 的换源屏用这些词，Dart 那两份文件里没有：{missing}\n"
                         "改文案要两边各改一次，漏一边就是同一轮换源在两个界面说两种话")


def test_local_book_source_url_is_one_constant_two_places() -> None:
    """本地书伪源那一个 url：Python 有谓词，Dart 只许有**同一个字面量的精确相等**。

    为什么这一格值得单独钉：书架上"这本是本地文件"在 flet 侧靠控制器注入的
    `record["local"]`，而那一格**从来不过协议面**（`bookshelf.list` 里没有它）。
    Dart 收换源入口时必须自己判，能判的就只有 `source_url` 这一个字段——
    于是它落的判据必须与服务端 `is_local_source_url` 是同一个：精确相等、同一个串。

    反面形状有两种，都在这条守卫的下半身：界面写 `startsWith('localsource')`
    （前缀判据，服务端那边多一个变体就漏）；或者在 `bookshelf_screen.dart` 里
    再抄一遍那个 url（两处字面量，改了服务端那份它不跟着改）。
    """
    shelf = _read("features/bookshelf/data/bookshelf_item.dart")
    dart_value = _literal(shelf, r"const String kLocalBookSourceUrl\s*=\s*'([^']+)'",
                          "kLocalBookSourceUrl")
    assert dart_value == LOCAL_BOOK_SOURCE_URL, (
        f"Dart 写的是 {dart_value!r} 而服务端是 {LOCAL_BOOK_SOURCE_URL!r}；"
        "串不一样时那一本的入口就不会收起")
    # 递得回去：Dart 拿去问服务端的那一串，服务端的谓词必须认。
    assert is_local_source_url(dart_value) is True
    # 正对照：谓词不是"什么 url 都算本地"。
    assert is_local_source_url("https://example.com/a") is False
    assert is_local_source_url("") is False

    # 判据只许有一处：那一行必须还是精确相等，且别处不再抄第二个字面量。
    assert "bool get isLocal => sourceUrl == kLocalBookSourceUrl;" in shelf, \
        "`isLocal` 不再是「同一个字段的精确相等」——判据换了，服务端那边没换"
    for rel in ("features/bookshelf/presentation/bookshelf_screen.dart",
                "features/bookshelf/presentation/source_switch_dialog.dart",
                "features/bookshelf/domain/source_switch_notifier.dart"):
        # 读**字面量**不读整份文件：这三处都在注释里复述过那一个 url（讲的就是
        # "为什么服务端那边多一个变体会漏"），拿 substring 比就是把说明当成读取。
        assert LOCAL_BOOK_SOURCE_URL not in _dart_literals(rel), (
            f"{rel} 里出现了第二个 {LOCAL_BOOK_SOURCE_URL!r} 字面量：界面自己发明判据的地方")


def _py_function_source(py_rel: str, func: str) -> str:
    """某个 Python 函数/方法的**源码那一段**（连 docstring 一起给）。

    用来钉"这一格是怎么算出来的"——字面量比对只能证明两处写了同一个词，
    证明不了其中一处没有另算一遍。
    """
    source = _py_source(py_rel)
    tree = ast.parse(source)
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == func), None)
    if node is None:
        pytest.fail(f"{py_rel} 里没有函数 {func}——守卫要读的位置挪了")
    return ast.get_source_segment(source, node) or ""


def test_media_key_field_is_one_name_three_places() -> None:
    """正文插图的地址：服务端发、Dart 收、屏上用，三处必须都叫 `mediaKey`。

    这一格是 G3-D4 新加的协议面字段，也是两种语言里第一次出现"同一个字段名手写三遍"。
    名字漂了**不会报错**：Dart 那句是 `json['mediaKey'] ?? ''`，取不到就是空串，
    空串等于"这块没有二进制路"，于是图位上一片空白，而两侧各自的测试都在读自己
    抄的那个名——两边都绿，用户看到的缺图。
    """
    row = _py_function_source("app/services/reading/reading_service.py", "_block_row")
    assert '"mediaKey": media_key' in row, \
        "服务端发的那一格不再叫 mediaKey（或不再由入参填）"
    assert '"media_key"' not in row, "同一份回话里出现第二个名：客户端只会读到空串"

    doc = _read("features/reader/data/chapter_document.dart")
    assert re.search(r"mediaKey:\s*'\$\{json\['mediaKey'\]", doc), \
        "Dart 收这一格的那一句挪了/换了名"
    assert "final String mediaKey;" in doc, "字段名不是 mediaKey"

    screen = _read("features/reader/presentation/reader_screen.dart")
    assert "block?.mediaKey" in screen, \
        "阅读屏不再从 mediaKey 取地址——它多半改回自己拼 URL 了（§4.4 不许）"

    # 反面形状：整个 Dart 工程里不许出现 snake_case 的那个名当协议键读。
    doubles = sorted({(path.relative_to(DART_LIB).as_posix(), found.group(1))
                      for path in _dart_files()
                      for found in re.finditer(r"""json\[['"]([^'"]+)['"]\]""",
                                               path.read_text(encoding="utf-8"))
                      if found.group(1) in ("media_key", "mediakey", "MediaKey")})
    assert doubles == [], f"Dart 侧用这几个拼法读协议键：{doubles}"


def test_chapter_media_grammar_lives_in_the_cache_only() -> None:
    """两种 /media key 里，正文那一种的语法只许住在 ``reading_cache`` 一处。

    网关要是把那个正则抄第二份，缓存改名（比如给下标补零）时它不会跟着改：
    合法的 key 被判成 404，而 reading_cache 自己的测试全绿——断的是接线，
    不是实现（记忆里 `refactor-regression-orphan-methods` 那个形状）。
    """
    offenders = []
    for py in sorted((ROOT / "app").rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        if "[0-9a-f]{64}" in _py_source(
                py.relative_to(ROOT).as_posix()) and py.name != "reading_cache.py":
            offenders.append(py.relative_to(ROOT).as_posix())
    assert offenders == [], f"第二处抄了正文插图 key 的语法：{offenders}"

    media = _py_function_source("app/services/gateway/service.py", "_media")
    assert "is_media_key(key)" in media, "网关不再问缓存那把尺，而是自己判形状"
    assert "cache.media_bytes(key)" in media, "网关不再把取字节交回缓存"
    for banned in ("sha256", "hexdigest", "_digest"):
        # 封面那一发的 `_MEDIA_KEY_RE.match` 是**它自己的**形状（名字出自
        # SourceService 落在盘上的文件名），不在 ban 之列；这里禁的是"自己算摘要"。
        assert banned not in media, f"网关自己算了 key：{banned}"


def test_chapter_media_key_shares_the_caches_own_digests() -> None:
    """发出去的 key 必须由**写盘那两支 _digest** 拼成，不在这里另算一遍摘要。

    这是 §4.4「不新造一套键空间」的可执行形式：`media_key` 与 `_book_dir`/
    `_content_path` 用的是同一个调用形状，所以"地址"与"盘上的名字"是同一件事。
    自己另算（哪怕同样 sha256）时，改 join 符、改编码、改参数序都不报错——
    只会让 /media 永远 404。
    """
    maker = _py_function_source("app/services/reading_cache.py", "media_key")
    assert "self._digest(source_url, book_url)" in maker
    assert "self._digest(chapter_url)" in maker
    for banned in ("hashlib", "md5", "sha256", "hexdigest"):
        assert banned not in maker, f"media_key 里自己算摘要：{banned}"

    # 正对照：那两个形状确实就是落盘路径用的，不是守卫在这里空转。
    assert "self._digest(source_url, book_url)" in _py_function_source(
        "app/services/reading_cache.py", "_book_dir")
    assert "self._digest(chapter_url)" in _py_function_source(
        "app/services/reading_cache.py", "_content_path")


def test_media_key_is_handed_out_from_the_content_key_only() -> None:
    """发地址的那一处只许由 ``_content_key`` 那个三元组喂，且全库只喂一次。

    字节是照着 `(源 url 归一化后, book.url, chapter.url)` 落盘的。用请求参数里的
    ``source_url`` 去算，出来的就是另一个摘要——地址对不上盘，而且只在"书源 url
    带斜杠/带协议差异"那种书上偶发，是最难复现的那一类。
    """
    document = _py_function_source("app/services/reading/reading_service.py",
                                   "chapter_document")
    assert "block_rows(shown.blocks, self._cache," in document
    assert "self._content_key(source, book, chapter)" in document, \
        "发地址用的不是那个归一化三元组"

    callers = sorted({py.relative_to(ROOT).as_posix()
                      for py in (ROOT / "app").rglob("*.py")
                      if "__pycache__" not in py.parts
                      and re.search(r"\bblock_rows\(", _py_source(
                          py.relative_to(ROOT).as_posix()))})
    assert callers == ["app/services/reading/reading_service.py"], \
        f"block_rows 出现了第二处调用点（第二个发地址的口）：{callers}"


def test_feed_html_to_text_rule_is_only_on_the_service() -> None:
    """feed 正文去标签那把尺**只有服务侧一把**：控制器与视图都不许再定义或自己剥一次。

    G4-B 之前它住在 ``rss_controller``（"显示前在这里去掉标签"），flet 那一面用着没问题；
    跨进程之后它就是**两侧共同的口径**——Dart 若自己剥一次，两边给出的字数、空行、
    脚本处理就会分家，而没有任何一侧会报错（记忆里 ``legado-port-adapt-not-copy`` 与
    「不许界面自己发明判据」说的都是这一族）。所以钉三处：定义在不在服务上、
    控制器是不是 import 过来的、视图有没有沾 HTML。
    """
    stripper = _py_function_source("app/services/content/rss_service.py", "plain_text")
    assert "BeautifulSoup" in stripper and "script" in stripper, \
        "服务侧那把尺不再去脚本/样式：它已经不是原来那把尺了"

    controller = _py_source("app/controllers/rss_controller.py")
    assert "def plain_text" not in controller, \
        "控制器里还有第二份 plain_text 定义：两侧各剥一次的入口又开回来了"
    assert re.search(r"from app\.services\.content\.rss_service import [^\n]*plain_text",
                     controller), "控制器没从服务侧 import 那把尺"

    view = _py_source("app/views/reading/rss_view.py")
    for banned in ("plain_text", "BeautifulSoup"):
        assert banned not in view, \
            f"订阅页视图出现了 {banned}：视图不认 HTML，这条红线挪位置了"

    # 发正文那一步必须真过这把尺（不是"函数还在，没人调用"那一族）
    detail = _py_function_source("app/services/content/rss_service.py", "article_detail")
    assert "plain_text(" in detail, "rss.article 的正文没过那把尺"

    # 两处定义的正对照：controllers/views 里一个 plain_text 定义都不许有
    second = sorted(py.relative_to(ROOT).as_posix()
                    for py in list((ROOT / "app" / "controllers").rglob("*.py"))
                    + list((ROOT / "app" / "views").rglob("*.py"))
                    if "__pycache__" not in py.parts
                    and re.search(r"\bdef plain_text\(", _py_source(
                        py.relative_to(ROOT).as_posix())))
    assert not second, f"界面面出现了 plain_text 的定义处：{second}"


def test_rss_projections_carry_the_screen_and_not_the_body() -> None:
    """订阅面两发的投影名单：**列表不带正文，角标两格必须在**。

    名单在服务侧有一份常量（``ARTICLE_ROW_FIELDS``）、在 action 表上有一份，两处必须
    逐字相等——这就是"字段在两侧各写一遍迟早会飘"那一笔账，方向是列表那一发。
    另一头钉角标：``total``/``unread`` 少了任何一格，客户端就只能自己逐源数文章
    （``article_counts`` 那条 GROUP BY 的存在理由）。
    """
    from app.services.content.rss_service import ARTICLE_ROW_FIELDS

    articles = BY_ACTION["rss.articles"]
    assert tuple(articles.fields or ()) == ARTICLE_ROW_FIELDS, (
        f"rss.articles 的投影 {articles.fields} 与服务那份名单 {ARTICLE_ROW_FIELDS} 不同")
    for name in ("content", "text"):
        assert name not in (articles.fields or ()), \
            f"文章列表那一发带着正文（{name}）：一屏几十行每行都剥一次 HTML"

    detail = _py_function_source("app/services/content/rss_service.py", "article_detail")
    # 钉的是**回包里的键**，不是"这段源码里有没有 content 这几个字母"：SQL 本来就要
    # SELECT 那一个列（库里存的还是 HTML 原文），拿 substring 判会把合法写法判成事故。
    assert '"content":' not in detail and "'content':" not in detail, \
        "rss.article 把 HTML 原文也递出去了：那界面就会自己再剥一次"
    assert '"text":' in detail, "rss.article 不再给显示口径那一份正文"
    # 正对照：正文确实是从库里那一列**现剥**出来的，不是凭空造的字段。
    assert re.search(r'"text":\s*plain_text\(', detail), \
        "rss.article 的 text 没过那把尺（或那一列压根没读）"

    sources = BY_ACTION["rss.sources"]
    assert {"total", "unread"} <= set(sources.fields or ()), \
        f"订阅列表的角标两格不在投影里：{sources.fields}"
    rows = _py_function_source("app/services/content/rss_service.py", "source_rows")
    for call in ("self.list_sources()", "self.article_counts()"):
        assert call in rows, f"source_rows 不再经 {call}：那份 join 有了第二个写法"


#: 订阅屏 flet 有、Dart **不该**复刻的六格。分两族，理由各不相同：
#:
#: - 前五格说的是**进程内装配态**：`self._rss is None`（服务没挂上）与
#:   `self._page() is None`（视图还没挂载，所以没有弹层可挂）。跨进程之后客户端手里
#:   没有这两个状态——服务装配失败是服务端的事，走到协议面就是**错误信封**，
#:   Dart 说的是「订阅源列表加载失败：<原因>」那一族（`rss_notifier.dart`）。
#:   把「未就绪」要求进 Dart，等于要求客户端预知服务端的接线。
#: - 最后一格只进 `logger.debug`，从来不上屏（与 `_SWITCH_LOGGER_ONLY_SENTENCES` 同族）。
_RSS_FLET_ONLY_RUNS = {
    "未就绪": "RssService 没挂上时的状态句（三处）：跨进程后这一格在服务端",
    "订阅页无法读写": "同上一句的后半",
    "页面未就绪": "flet 的 page 还没挂载——Flutter 侧对话框走的是 widget context，没这一格",
    "暂时弹不出添加框": "同上一格：添加框弹不出的原因在 flet 的挂层机制上",
    "暂时弹不出确认框": "同上一格：删除确认框",
    "刷新后重绘失败": "只进 logger.debug（`_after_refresh_done`），不上屏",
}

#: 反方向：Dart 有、flet 那一屏**没有**的话。跨进程才多出来的两个状态，各写明凭什么
#: （记忆 `feedback-protocol-missing-predicate-pin-server`：界面自造读数要么上钉常量、
#: 要么加服务端字段，并且必须具名）。第九十七轮当场扫出来的就是这两格。
_RSS_DART_ONLY_RUNS = {
    "个源取失败了": "kRssRefreshDoneWithFailures：收尾那一帧带 failures，而 flet 吃的是"
                   "`refresh_all()` 的返回值，单源失败被服务吞进 `status.last_error`，"
                   "那一屏读不到条数。只报「新增 0 篇」会把「三个源都没连上」演成"
                   "「三个源都没新文章」（`zero-counts-need-a-positive-control`）",
    "事件流断开": "rss_screen 的 onStreamError：flet 是同进程回调，没有可断的流；"
                "跨进程之后这条故障必须出声，写法与 find_books 屏同一句",
}


def test_rss_wording_is_read_from_flet_into_dart() -> None:
    """订阅屏每句上屏的话，用词表由 flet 的 AST 给，Dart 那三份文件必须都接住。

    扫两处：flet 把这一屏的话拆在两个文件里——控件形状与标签在
    `app/views/reading/rss_view.py`，所有状态句在 `app/controllers/rss_controller.py`
    （它把句子写在 `_status(...)` 的调用位上，不在函数自己的 docstring 里，
    所以整模块扫是安全的：docstring 那一族由 `_cjk_literals_from` 排除）。

    **两个方向都查**。正向是"改文案要两边各改一次，漏一边当场红"；反向查的是
    "界面自己造了一句 flet 从没说过的话"——正向守卫对这一族完全瞎（它只拿着 flet 的词表），
    而跨进程这一侧恰恰会新增状态（失败源数、事件流断）。两侧各自的排除表都必须是
    **活配置**：条目在对应那一侧抽不到了，就是那句已经改名或删掉，这张表要跟着改，
    不许留着当装饰（`comparison-closure-must-reread-live`）。
    """
    runs = _cjk_runs(
        _cjk_literals_in_module("app/views/reading/rss_view.py")
        + _cjk_literals_in_module("app/controllers/rss_controller.py"))

    vanished = sorted(set(_RSS_FLET_ONLY_RUNS) - set(runs))
    assert not vanished, (f"排除表里的 {vanished} 在 flet 侧已经抽不到了——"
                          "那句要么改名了要么删了，这张表要跟着改")

    kept = [word for word in runs if word not in _RSS_FLET_ONLY_RUNS]
    # 第九十七轮当场数过：整批 73 个语词，视图那一份 45、控制器那一份 29，
    # 去掉排除表那六格是 67。底价 60 的牙齿：整份文件的扫描面掉任何一个都跌破
    # （掉控制器只剩 45，掉视图只剩 29）。
    assert len(kept) >= 60, (f"只抽到 {len(kept)} 个语词（当场测得的是 67）——"
                             "扫描面缩了就是仪器没接上，这条守卫不许靠“少抽”变绿")

    dart_files = ("features/rss/data/rss_models.dart",
                  "features/rss/domain/rss_notifier.dart",
                  "features/rss/presentation/rss_screen.dart")
    dart = "\n".join(_dart_literals(rel) for rel in dart_files)
    missing = sorted(word for word in kept if word not in dart)
    assert not missing, (f"flet 的订阅屏用这些词，Dart 那三份文件里没有：{missing}\n"
                         "改文案要两边各改一次，漏一边就是同一轮刷新在两个界面说两种话")

    # 反方向：Dart 抽到的语词里，flet 没有的那些必须具名。
    dart_runs = _cjk_runs(dart.split("\n"))
    stale = sorted(set(_RSS_DART_ONLY_RUNS) - set(dart_runs))
    assert not stale, f"反向排除表里的 {stale} 在 Dart 侧已经没有了——那句改名或删了"
    extra = sorted(word for word in dart_runs if word not in runs
                   and word not in _RSS_DART_ONLY_RUNS)
    assert not extra, (f"Dart 的订阅屏在说 flet 从没说过的话：{extra}\n"
                       "要么补进服务端让它有出处，要么写明凭什么（进排除表要带理由）")


# 黄金样本那一族的守卫（覆盖表、时效摘要、请求合法性、账目自洽、job 的事件交付）
# 全部住在 tests/test_gateway_golden_corpus.py：这里只留跨语言字面量那几支。
# 搬过来的理由就是这份文件自己的主张——同一处事实不许有第二份写法。
