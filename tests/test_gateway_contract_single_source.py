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
#: 所以它现在与其余 43 发一样**必须有调用点**，那张例外表删掉了。
#: 表现在是空的，而**判据照跑**——它的作用是下一门（G3-D 的换源、G4 的那几族）
#: 落地时的同一张嘴，不是历史装饰。
#: 口径提醒：这里数的"Dart 侧有人落"是**协议面在 Dart 有实现**（`core/rpc/` 那三个文件
#: 里的字面量），不是"界面调了它"；后者由 widget/golden 测试那一面负责（G3-C3）。
#:
#: 第九十三轮（G3-D3）把换源那一族五发登记上表，Dart 的仓库面与换源屏在同一门的
#: 第二发提交里接——这一张欠据管的就是那半截。划掉的判据是 `core/rpc/` 里出现这五个
#: 字面量，不是"屏幕看着像"。
_UNWIRED_PENDING_GATE: dict[str, str] = {
    "switch.seeds": "G3-D3 的 Dart 换源屏：打开对话框先读本地种子（不联网那一发）",
    "switch.scan": "G3-D3 的 Dart 换源屏：mode=job + 进度事件，换源那一轮的结果从 /ws 走",
    "switch.snapshot": "G3-D3 的 Dart 换源屏：断线重连补回'扫到第几源'的那一发",
    "switch.cancel": "G3-D3 的 Dart 换源屏：停止按钮那一发（合作式叫停，保留已扫出的候选）",
    "switch.apply": "G3-D3 的 Dart 换源屏：选中候选写回书架那一发",
}


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
    return path.read_text(encoding="utf-8")


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
    # **裸表达式**位置的字符串常量一律不收：那是 docstring（函数自己的、以及里面
    # 嵌套函数/类的），不是要上屏的话。只按"含不含换行"过滤是不够的——
    # `"""把 pending 持久化到 SettingsService。"""` 就是单行的，第一次跑就撞上去了。
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
        if re.search(r"[\u4e00-\u9fff]", value) and value not in out:
            out.append(value)
    return out


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


# 黄金样本那一族的守卫（覆盖表、时效摘要、请求合法性、账目自洽、job 的事件交付）
# 全部住在 tests/test_gateway_golden_corpus.py：这里只留跨语言字面量那几支。
# 搬过来的理由就是这份文件自己的主张——同一处事实不许有第二份写法。
