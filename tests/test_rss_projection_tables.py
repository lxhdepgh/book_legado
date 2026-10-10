"""订阅面四张**投影表**（``tests/gateway/projection/rss.*.json``）的 Python 侧守卫。

分工（与 ``tests/test_reader_position_projection.py`` 同族，比的是另一头）：

- Dart 那两只（``rss_projection_test.dart`` / ``rss_screen_test.dart``）比
  "我这边渲染出来的 == 表里那一格"；
- **本文件**比"表里那一格 == 现在的 flet 现渲"。少了这一支，flet 改了字样而没人重跑
  探针时，Dart 会绿着对上一张过期的表——那正是 ``comparison-closure-must-reread-live``
  说的那件事：比较口必须现读现场，"表存在"不等于"表还对"。

表是怎么造的：``.probe/g4_rss_projection.py`` 现场跑 flet 自己的
``RssView.render_sources / render_articles / show_article / on_band_change`` 产出，
**答案一个都没手写**（合成的是输入，不是答案）。这里不重抄那套逻辑，而是**再调用一次
同一批 builder** 与磁盘上那份比——同一个写法的第二次调用，不是第二份写法。

四条判据各配反证：

1. 四张表都在、行数不低于当场测得的底价、``renderer`` 点名的函数**现在还在**；
2. 现算一遍与文件逐格比（除时区戳那格），并且**同一只手**改一格必须不等
   （否则那只比较口是装饰，见 ``guard-tests-assert-absence``）；
3. 时区戳那一格在不在、说的是不是"重跑探针"；
4. builder 与表**两头对齐**：探针里多一只没人守的 builder、或登记了却查不到文件，都红。
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "gateway" / "golden"
PROJECTION = ROOT / "tests" / "gateway" / "projection"
PROBE = ROOT / ".probe" / "g4_rss_projection.py"
VIEW = ROOT / "app" / "views" / "reading" / "rss_view.py"

#: 文件名 → 探针里那只 builder 的名字。
TABLES = {
    "rss.sources.rows": "sources_table",
    "rss.articles.rows": "articles_table",
    "rss.article.detail": "detail_table",
    "rss.shape": "shape_table",
}

#: 当场（第九十七轮）数出来的行数底价。**不是**期望行数，而是"少这么多就是仪器没接上"：
#: 每一行是一条分支，掉一行就有一支没被画过。多出几行不算红。
FLOOR_ROWS = {
    "rss.sources.rows": 11,      # 黄金 3 行 + 未读有/无 + 无名称 + 停用 + 从未 + epoch + 小数目 + 空表
    "rss.articles.rows": 13,     # 黄金 2 行 + Atom 无作者 + 日期两档 + 收藏/已读 + 空表 …
    "rss.article.detail": 9,     # 正文/空正文/无文章 + 清屏两句 + 收藏 + 元信息缺项 …
    "rss.shape": 6,              # 宽窄两副 + 够不着的级 + 源删光自动退一级
}


@pytest.fixture(scope="module")
def probe():
    """按路径装载 ``.probe/g4_rss_projection.py``（.probe 不是包，走 importlib）。

    探针自己把仓库根插进 ``sys.path``，所以这里不需要额外铺垫。
    """
    assert PROBE.is_file(), f"探针不见了：{PROBE}"
    spec = importlib.util.spec_from_file_location("g4_rss_projection", PROBE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["g4_rss_projection"] = module
    spec.loader.exec_module(module)
    return module


def _table(name: str) -> dict:
    path = PROJECTION / f"{name}.json"
    assert path.exists(), f"参照表没落地：{path}——先跑 .probe/g4_rss_projection.py"
    return json.loads(path.read_text(encoding="utf-8"))


def _comparable(table: dict) -> str:
    """比对的**一个口**：整张表按 JSON 往返、按 key 排序，只摘掉时区戳那一格。

    摘 `generated` 不是因为不比它，而是它记的就是"这台机器几点"，换时区的机器上现算
    必然与旧文件不同——那一格由 ``test_…timezone_marker`` 单独管，且它管的是"有没有指路"，
    不是"值对不对"。
    """
    trimmed = {key: value for key, value in table.items() if key != "generated"}
    return json.dumps(trimmed, ensure_ascii=False, sort_keys=True)


def _renderer_functions(table: dict) -> list[str]:
    """从 ``renderer`` 那一句里点名出函数名（``::`` 之后、中文括号之前，``/`` 分隔）。"""
    tail = str(table["renderer"]).split("::", 1)[-1]
    tail = tail.split("（", 1)[0]
    names = [part.strip().split(".")[-1] for part in tail.split("/") if part.strip()]
    return [name for name in names if name]


# --------------------------------------------------------------------------- #
# 一、四张表都在，且点名的是当前的 flet
# --------------------------------------------------------------------------- #
def test_the_four_tables_are_on_disk_and_name_functions_that_still_exist(probe) -> None:
    view_source = VIEW.read_text(encoding="utf-8-sig")
    total_names = 0
    for name, builder in TABLES.items():
        table = _table(name)
        assert isinstance(table["rows"], list) and table["rows"], f"{name} 是空的"
        assert len(table["rows"]) >= FLOOR_ROWS[name], (
            f"{name} 只有 {len(table['rows'])} 行，当场测得的底价是 {FLOOR_ROWS[name]}："
            "少一行就少一条被画过的分支（先重跑探针，别在这里抬底价）")
        names = _renderer_functions(table)
        assert names, f"{name} 的 renderer 那一句点不出函数名：这一支没在比东西"
        total_names += len(names)
        for func in names:
            assert f"def {func}" in view_source, (
                f"{name} 说答案出自 {func}，而 rss_view.py 里已经没有这个函数了")
        assert hasattr(probe, builder), f"探针里没有 {builder}：表和守卫脱节了"

    # 正对照：解析器真的在解析东西。当场数过四张表合计点出 9 个函数名
    # （sources 1 / articles 1 / detail 2 / shape 5）；少于这个数就是句式变了，
    # 而 `_renderer_functions` 空转时，上面那圈"逐个函数名核对"会变成零次循环，
    # 读起来照样像"每一格都对上了"。
    assert total_names >= 9, f"四张表合计只点出 {total_names} 个函数名：解析口窄了"

    for name in TABLES:
        for sample in _golden_names(_table(name)):
            assert (GOLDEN / f"{sample}.json").is_file(), (
                f"{name} 的 payload_from 指着不存在的样本 {sample}")


def _golden_names(table: dict) -> list[str]:
    """`payload_from` 里提到的黄金样本名（`rss.articles{,--no-author,…}.json` 展开）。"""
    text = str(table.get("payload_from", ""))
    found: list[str] = []
    for match in re.finditer(r"(rss\.[a-z_]+)(\{[^}]*\})?", text):
        base, braces = match.group(1), match.group(2)
        if braces:
            found.extend(f"{base}{part}" for part in braces.strip("{}").split(","))
        else:
            found.append(base)
    return sorted(set(found))


# --------------------------------------------------------------------------- #
# 二、表与**当前的 flet** 逐格同数
# --------------------------------------------------------------------------- #
def test_the_tables_still_match_a_live_render_from_current_flet(probe) -> None:
    for name, builder in TABLES.items():
        fresh = getattr(probe, builder)()
        on_disk = _table(name)
        assert _comparable(fresh) == _comparable(on_disk), (
            f"{name} 与现场重渲的不是同一批答案：flet 的字样/形状改了却没重跑探针。\n"
            "重跑：PYTHONIOENCODING=utf-8 PYTHONPATH=. python .probe/g4_rss_projection.py")


def test_the_comparator_is_not_a_tautology(probe) -> None:
    """上面那条比的是"同一只 builder 的两批答案"，所以要证明**改一格就会不等**。

    反证用 ``rss.shape``：它是四张表里唯一没有 ``row``/``article`` 输入列的一支，
    最容易长成"两边都读同一个 dict"而恒真。
    """
    fresh = probe.shape_table()
    baseline = _comparable(fresh)

    visible = copy.deepcopy(fresh)
    visible["rows"][0]["expected"]["switch_visible"] = not \
        visible["rows"][0]["expected"]["switch_visible"]
    assert _comparable(visible) != baseline, (
        "改了 switch_visible 都没让比较变红——那只比较口是装饰")

    disabled = copy.deepcopy(fresh)
    target = disabled["rows"][1]["expected"]["switch"][2]
    target["disabled"] = not target["disabled"]
    assert _comparable(disabled) != baseline, (
        f"改了「{target['text']}」那一颗的 disabled 也不红：switch 那一列没被比到")

    dropped = copy.deepcopy(fresh)
    del dropped["rows"][0]
    assert _comparable(dropped) != baseline, "少一整行也不红：行数根本没在比"


def test_the_corpus_evidence_still_has_its_witnesses(probe) -> None:
    """``corpus_evidence`` 那几格是**语料自己走到了哪几支**的证据，退化了就红。

    判据用探针自己的谓词（现算），不在这里重写一遍 `float(...) > 1e9`：两张尺比一张表
    比不出漂移（``measurement-corpus-must-not-be-self-proving`` 的同形）。
    """
    evidence = probe.sources_table()["corpus_evidence"]
    assert evidence["epoch_rows"] > 0, "语料里一条 epoch 秒的行都没有：那两发只是合成行"
    assert evidence["unread_rows"] > 0, "语料里没有带未读的行：「N 未读」那一段没了证人"

    golden_rows = json.loads((GOLDEN / "rss.sources.json").read_text(encoding="utf-8"))[
        "response"]["data"]
    synthetic = sum(1 for row in probe.sources_table()["rows"]
                    if not str(row["case"]).startswith("row_"))
    assert synthetic + len(golden_rows) == len(probe.sources_table()["rows"]), (
        "表里的行数不等于「黄金行 + 合成分支行」：语料换过而表没重跑")


# --------------------------------------------------------------------------- #
# 三、时区戳
# --------------------------------------------------------------------------- #
def test_the_tables_carry_the_timezone_they_were_answered_in() -> None:
    """``_when`` 把 epoch 秒按**本地时区**折成 ``MM-DD HH:MM``，所以答案自带时区戳。

    这里钉的是"戳在不在、有没有指路"，不比大小：换时区的机器上 Dart 那一侧
    （``rss_projection_test.dart`` 的 ``_requireSameTimeZone``）会直接拒绝比对并点名重跑，
    而不是拿旧答案比新渲染。
    """
    for name in TABLES:
        generated = _table(name).get("generated")
        assert isinstance(generated, dict), f"{name} 没有 generated 块：重跑探针"
        assert "local_utc_offset_minutes" in generated, f"{name} 的 generated 缺时区戳"
        # 指路要指到**跑得动的那只脚本**：写"本探针"的人不知道探针叫什么。
        script = str(generated.get("probe") or "")
        assert script and (ROOT / script).is_file(), (
            f"{name} 的 generated.probe={script!r} 不是一只在位的脚本")
        assert "_when" in str(generated.get("reason", "")), (
            f"{name} 的 generated.reason 没说是哪把尺带来的时区依赖")


# --------------------------------------------------------------------------- #
# 四、builder 与表两头对齐
# --------------------------------------------------------------------------- #
def test_every_builder_has_a_table_and_vice_versa(probe) -> None:
    builders = sorted(name for name in dir(probe) if name.endswith("_table"))
    assert builders == sorted(TABLES.values()), (
        f"探针的 builder 面 {builders} 与守卫登记的 {sorted(TABLES.values())} 不同")
    on_disk = sorted(path.stem for path in PROJECTION.glob("rss.*.json"))
    assert on_disk == sorted(TABLES), (
        f"磁盘上的订阅表 {on_disk} 与登记的 {sorted(TABLES)} 不同")
