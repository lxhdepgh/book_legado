"""跨语言契约守卫：Dart 客户端与 Python 网关**不许各自漂**。

为什么要有这一支（设计 §4.1 / §6.3 / §10）：信封、错误码、窄窗阈值这三样今天各有两份
手写实现。flet 时代它们只有一份（Python），拆成两个进程之后第一次出现"同一件事写在
两种语言里"，而这类漂移的表现不是报错，是**客户端把"没连上"渲染成"书架是空的"**。

规矩：这一支只读 Dart 源文件里的**字面量**，不 import 任何东西到 Python 侧来"转换"——
一旦这里开始解析 Dart 的类型系统，守卫自己就成了第二个要维护的实现。

flutter_client 目录不存在时（比如 CI 只跑 Python 面）**跳过而不是失败**：
G1 之前这条守卫不该把全库回归拦住。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.gateway.methods import BY_ACTION
from app.gateway.protocol import ERROR_CODES, PROTOCOL_VERSION
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
    """扫出**像 action** 的字符串字面量，排掉两类同形状的邻居。

    - `import 'router.dart'`：文件名也是 `小写.小写`，靠后缀排掉；
    - `Key('bookshelf.refresh')`：控件键与 action 用的是同一个 `域.名词` 形状，
      但它不是协议面上的东西，靠 `Key(` 前缀排掉。

    排掉的两条都必须窄：宽到"整个文件跳过"就把真调用点也一起藏了。
    """
    shape = re.compile(r"(?<!Key\()'([a-z][a-zA-Z]*\.[a-zA-Z][a-zA-Z]*)'")
    return [m for m in shape.findall(text)
            if not any(m.endswith(ext) for ext in _DART_EXTENSIONS)]


def test_dart_only_calls_actions_that_exist() -> None:
    """Dart 里出现的每个 `域.动词` 字面量都必须在 action 表上。

    表外动作在服务端回 `bad_request`，用户看到的是"点了没反应"——这正是
    `refactor-regression-orphan-methods` 那一族的反向版本（那边是方法还在没人调，
    这边是客户端在调一个从来没登记过的动作）。
    """
    used: dict[str, list[str]] = {}
    for path in _dart_files():
        for name in _action_literals(path.read_text(encoding="utf-8")):
            used.setdefault(name, []).append(
                str(path.relative_to(DART_LIB)).replace("\\", "/"))
    assert used, "Dart 里一个 action 都没扫到——仪器没接上"
    unknown = {name: where for name, where in used.items() if name not in BY_ACTION}
    assert not unknown, f"客户端在调用没登记过的动作：{unknown}"


def test_widget_key_lookalikes_are_not_mistaken_for_actions() -> None:
    """上面那条排掉 `Key('bookshelf.refresh')` 的手必须**窄**到只排键。

    正对照：拿一行真调用点喂同一个解析器，它必须被扫出来。否则"排键"其实是
    "整个不扫"，前一支就永远绿着（记忆里 `guard-tests-assert-absence` 那一族）。
    """
    sample = ("  final a = client.call('bookshelf.list');\n"
              "  key: const Key('bookshelf.refresh'),\n"
              "import 'router.dart';\n")
    assert _action_literals(sample) == ["bookshelf.list"], _action_literals(sample)


# 黄金样本那一族的守卫（覆盖表、时效摘要、请求合法性、账目自洽、job 的事件交付）
# 全部住在 tests/test_gateway_golden_corpus.py：这里只留跨语言字面量那几支。
# 搬过来的理由就是这份文件自己的主张——同一处事实不许有第二份写法。
