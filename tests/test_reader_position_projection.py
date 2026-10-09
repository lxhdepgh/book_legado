"""flet 阅读面的**落点参照表**（``tests/gateway/projection/reader.content*.json``）的守卫。

这张表是 §9 G3 判据"同一本书翻页落点与 flet 版可比（按字符偏移断言）"里 **flet 那一半的
读数**：表由 ``.probe/g3c_flet_projection.py`` 现场跑 flet 自己的
``ReaderView.content_plan`` 与 ``app.core.text_layout`` 那族坐标函数产出（答案不是手抄的），
Dart 那侧（G3-C）只许对上这张表。

本文件管三件事，每件都配了反证：

1. **表还对着当前的 flet**：现算一遍与文件比——flet 的排版改了却没重跑探针，这里红。
2. **表不是另一把尺**：表里的偏移必须等于 ``paragraph_offsets(chapter_paragraphs(...))``，
   并且必须**不等于**三种"看起来也行"的错尺（按块数、不带段间格、按显示形态含缩进）。
   这一条钉的是"第三把尺被重新引进"（记忆 ``measurement-corpus-must-not-be-self-proving``：
   只比候选尺与正确答案相等，比不出候选尺什么时候会退化）。
3. **行与段是两件事**：媒体占一行不产段；标题行占一行**也**产段。两种形状都在语料里，
   少一种就有一种把行号当段号的实现能绿着过去。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.text_layout import (PARAGRAPH_JOINER_LEN, chapter_paragraphs,
                                  chapter_text_length, offset_of_paragraph,
                                  paragraph_at_offset, paragraph_offsets)
from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.views.reading.reader_view import ReaderView

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "gateway" / "golden"
PROJECTION = ROOT / "tests" / "gateway" / "projection"


def _document(data: dict) -> ChapterContent:
    """协议面那一份 dict → 真 ``ChapterContent``（真 dataclass，不是替身）。"""
    return ChapterContent(
        blocks=[ContentBlock(kind=str(row.get("kind") or ""),
                             value=(b"\x00" if row.get("binary") and not row.get("value")
                                    else (row.get("value") or "")),
                             headers=dict(row.get("headers") or {}),
                             title=str(row.get("title") or ""),
                             url=str(row.get("url") or ""))
                        for row in (data.get("blocks") or [])],
        title=str(data.get("title") or ""),
        image_style=str(data.get("imageStyle") or ""))


def _table(name: str) -> dict:
    path = PROJECTION / f"{name}.json"
    assert path.exists(), f"参照表没落地：{path}——先跑 .probe/g3c_flet_projection.py"
    return json.loads(path.read_text(encoding="utf-8"))


def _input_of(table: dict) -> dict:
    """表里那份**输入**：合成那两发原样钉在表里，黄金那一发指回黄金样本。

    输入可以钉死（答案不许手写）；载荷本来就落在语料里的那一发不抄第三遍正文——
    表里已经存了 ``ruler`` 与 ``display`` 两份，再加一份整表从 42 KB 变 60 KB（实测）。
    """
    if "input" in table:
        return dict(table["input"])
    ref = str(table.get("input_ref") or "")
    assert ref, "参照表既没有 input 也没有 input_ref：重跑探针"
    path, _, pointer = ref.partition("#")
    data = json.loads((ROOT / path).read_text(encoding="utf-8"))
    for key in [part for part in pointer.split("/") if part]:
        data = data[key]
    assert isinstance(data, dict), f"input_ref 指到的不是 dict：{ref}"
    return data


def _golden_document(name: str = "reader.content") -> ChapterContent:
    sample = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
    return _document(sample["response"]["data"])


# --------------------------------------------------------------------------- #
# 一、表与当前的 flet 同口径
# --------------------------------------------------------------------------- #
def test_projection_table_still_matches_the_current_ruler() -> None:
    """现算一遍偏移，与表里那份逐格比：改了 flet 的排版没重跑探针就红。

    这一条不许写成"表里第 i 格 = 表里第 i-1 格 + 长度 + 1"那种**自证**（表内部本来就该
    自洽，那条只能抓住表被手改过）。这里比的是**表 vs 现场函数**。
    """
    document = _golden_document()
    paragraphs = chapter_paragraphs(document.blocks)
    table = _table("reader.content")
    assert table["offsets"] == paragraph_offsets(paragraphs), (
        "参照表的偏移与当前 text_layout 算出来的不是同一串——重跑探针或说明尺动了")
    assert table["paragraph_count"] == len(paragraphs)
    assert table["total_chars"] == chapter_text_length(paragraphs)
    # 正对照：语料不能是空章，否则上面三条在空表上恒成立。
    assert table["paragraph_count"] >= 5, (
        f"参照表只有 {table['paragraph_count']} 段：这一发测不出尺的形状")


def test_projection_table_agrees_with_the_service_facade() -> None:
    """表里的段数/字数必须等于**服务端那一发自己报的**那两个数。

    这是"一把尺"最硬的一条证据：一侧是 flet 现算（探针），一侧是真 ``--serve`` 子进程
    序列化出来的载荷。两边数出来的不一样，跨进程比对落点就没有意义。
    """
    sample = json.loads((GOLDEN / "reader.content.json").read_text(encoding="utf-8"))
    data = sample["response"]["data"]
    table = _table("reader.content")
    assert table["paragraph_count"] == int(data["paragraphCount"]), (
        f"flet 数出 {table['paragraph_count']} 段，服务端那一发报 "
        f"{data['paragraphCount']} 段")
    assert table["total_chars"] == int(data["totalChars"]), (
        f"flet 数出 {table['total_chars']} 字，服务端那一发报 {data['totalChars']} 字")


# --------------------------------------------------------------------------- #
# 二、表里那把尺不是别的尺
# --------------------------------------------------------------------------- #
def test_the_table_is_not_one_of_the_three_wrong_rulers() -> None:
    """三种"看起来也行"的错尺必须与表**不等**（表里数的是段，不是块）。

    错尺一：按块数（黄金那一章 8 段只挂在 2 个块上——块首偏移≈章首偏移）。
    错尺二：不带段间格（每段少算 1 个字，越深漂越多且单调）。
    错尺三：按**显示形态**数（段首缩进每段多两个字，落库偏移整体漂）。
    只断言"表 = 正确尺"而不断言"表 ≠ 这三种"，就等于相信将来没人会重新引进它们。
    """
    document = _golden_document()
    table = _table("reader.content")
    offsets = table["offsets"]

    blocks = [b for b in document.blocks if str(b.kind) == "text" and b.value]
    by_block: list[int] = []
    cursor = 0
    for block in blocks:
        by_block.append(cursor)
        cursor += len(str(block.value))
    assert len(by_block) != len(offsets), (
        "黄金语料的正文块数与段数相等了：按块数那把错尺再也照不出来，换语料")
    assert offsets != by_block[:len(offsets)], "表退化成了按块数的那把尺"

    paragraphs = chapter_paragraphs(document.blocks)
    no_joiner = [sum(len(text) for text in paragraphs[:i]) for i in range(len(paragraphs))]
    assert offsets != no_joiner, (
        "表里的偏移没算段间那一格（每段少 "
        f"{PARAGRAPH_JOINER_LEN} 字）——一章越深漂得越多")

    displayed = [text for kind, payload, _ in ReaderView().content_plan(document)
                 if kind == "text" for text, _heading in payload]
    by_display: list[int] = []
    cursor = 0
    for text in displayed:
        by_display.append(cursor)
        cursor += len(text) + PARAGRAPH_JOINER_LEN
    assert offsets != by_display, (
        "表按**显示形态**数了（段首缩进进了尺）——落库偏移会整体漂")
    assert displayed != paragraphs, "显示形态与尺上的段文本完全一样：这一支照不出缩进那笔钱"


# --------------------------------------------------------------------------- #
# 三、行与段
# --------------------------------------------------------------------------- #
def test_media_rows_occupy_a_row_but_produce_no_paragraph() -> None:
    """插图挂一行、不产段：把行号当段号的实现必须在这一格对不上。

    正对照（``zero-counts-need-a-positive-control``）：同一张表里**确实有**一条媒体行，
    且正文行段的序号连续覆盖 0..paragraph_count-1——不然"媒体行段号为 None"
    可能只是因为整张表根本没有行。
    """
    table = _table("reader.content")
    rows = table["rows"]
    media = [row for row in rows if row["kind"] != "text"]
    assert media, "参照表里一条媒体行都没有：这一支在空转，重跑探针换语料"
    assert all(row["paragraph_index"] is None for row in media), (
        f"有媒体行领到了段号：{[r for r in media if r['paragraph_index'] is not None][:2]}")
    text_rows = [row for row in rows if row["kind"] == "text"]
    assert [row["paragraph_index"] for row in text_rows] == list(range(len(text_rows))), (
        "正文行的段序号不是从 0 连续排下来的")
    assert len(text_rows) == table["paragraph_count"], (
        f"屏上正文行 {len(text_rows)} 与尺上段数 {table['paragraph_count']} 不等")
    assert len(rows) > len(text_rows), "行集合与段集合一样大：媒体那一行没进表"


def test_projection_recomputes_from_the_recorded_input() -> None:
    """每张表：拿表里**记录在案的那份输入**（``input``）现算，逐格重对一遍全表。

    探针把输入原样写进了表，所以这张表在任何一台机器上都可复核——答案仍然只能由
    flet 自己的函数产出，手抄的答案在这一条下活不下来（第十三轮的规矩：输入可以钉死，
    答案不许手写）。这一条比"只比重算的偏移"硬：它连屏上的行集合一起重对。
    """
    view = ReaderView()
    for name in ("reader.content", "reader.content--media_only",
                 "reader.content--with_heading"):
        table = _table(name)
        document = _document(_input_of(table))
        paragraphs = chapter_paragraphs(document.blocks)
        assert table["paragraph_count"] == len(paragraphs), name
        assert table["offsets"] == paragraph_offsets(paragraphs), name
        assert table["total_chars"] == chapter_text_length(paragraphs), name
        plan = [(kind, payload, meta) for kind, payload, meta in view.content_plan(document)]
        re_rows: list[dict] = []
        for kind, payload, meta in plan:
            if kind == "text":
                for text, is_heading in payload:
                    re_rows.append({"kind": kind, "heading": bool(is_heading),
                                    "display": text, "meta": meta})
            else:
                re_rows.append({"kind": kind, "heading": False, "display": "", "meta": meta})
        assert len(table["rows"]) == len(re_rows), (
            f"{name}：屏上行数对不上（表 {len(table['rows'])} / 现算 {len(re_rows)}）")
        for row, rebuilt in zip(table["rows"], re_rows):
            for key, value in rebuilt.items():
                assert row[key] == value, f"{name} 第 {row['row']} 行的 {key} 变了"
            ruler = paragraphs[row["paragraph_index"]] if row["paragraph_index"] is not None \
                else None
            assert row["ruler"] == ruler, f"{name} 第 {row['row']} 行的尺上文本变了"


def test_heading_row_is_a_paragraph_too() -> None:
    """小标题行**认得出是标题**，但它在尺上就是第 0 段。

    显示认标题走 ``content_plan``（加粗、不缩进），尺上认段走 ``chapter_paragraphs``
    （不认标题）——两条路必须数出同一个第 0 段，否则"跳到第 k 段"和"屏上第 k 行"
    会在章首那一行分家。
    """
    table = _table("reader.content--with_heading")
    headings = [row for row in table["rows"] if row["heading"]]
    assert len(headings) == 1, f"合成那一章应只有一行标题，实得 {len(headings)} 行"
    heading = headings[0]
    assert heading["paragraph_index"] == 0, "标题行没占到第 0 段：显示与尺各数各的"
    assert heading["row"] == 0, "标题行不是屏上第一行"
    # 标题行显示与尺上同文（不缩进），正文行差那两个全角空格——两件事一起钉住，
    # 才排除"整表按显示形态数"的退化。
    assert heading["display"] == heading["ruler"], heading
    ruler_texts = [row["ruler"] for row in table["rows"] if row["kind"] == "text"]
    assert ruler_texts == chapter_paragraphs(_document(_input_of(table)).blocks), (
        "表里那些行的尺上文本与现算的段列表不是同一串")
    assert [p["length"] for p in table["paragraphs"]] == [len(t) for t in ruler_texts]
    indented = [row for row in table["rows"]
                if row["kind"] == "text" and row["display"] != row["ruler"]]
    assert len(indented) == 3, (
        f"表里只有 {len(indented)} 行显示带缩进：这一发照不出缩进那笔钱")
    assert all(row["display"].startswith("　　") for row in indented), indented


def test_a_media_only_chapter_has_no_paragraph_at_all() -> None:
    """整章只有媒体：段数 0、屏上仍有 1 行、落点全部夹在 (0, 0)。

    这一发是"空计数"那一族明知非 0 的对照（``zero-counts-need-a-positive-control``）：
    上一支的段数 0 如果来自仪器没接上，形状与真语料一模一样，所以这里把
    "行仍为 1 / 偏移表仍为空 / 探针仍夹回 (0,0)"三件事一起钉住。
    """
    table = _table("reader.content--media_only")
    assert table["paragraph_count"] == 0
    assert table["offsets"] == []
    assert table["total_chars"] == 0
    assert len(table["rows"]) == 1, "整章只有媒体时屏上应有 1 行"
    assert table["rows"][0]["paragraph_index"] is None
    for probe in table["offset_probes"]:
        assert (probe["paragraph_index"], probe["local"]) == (0, 0), probe
    # 正对照：同一支判据打在满语料那一发上不许全夹在 (0,0)。
    full = _table("reader.content")
    assert any(p["paragraph_index"] > 0 for p in full["offset_probes"]), (
        "落点探针在真语料上也没越过第 0 段：探针没接上")


# --------------------------------------------------------------------------- #
# 四、存档/回位两个方向互逆（表里那些数自洽，且与函数一致）
# --------------------------------------------------------------------------- #
def test_paragraph_to_offset_and_back_is_the_identity() -> None:
    """每个段号走"存 → 读"回来必须是同一段（越界除外，那一格另测）。

    表里那两个数不许只是自洽：这里的"存"与"读"都是**现场函数**（``offset_of_paragraph``
    / ``paragraph_at_offset``），表只负责被比。
    """
    for name in ("reader.content", "reader.content--media_only",
                 "reader.content--with_heading"):
        table = _table(name)
        paragraphs = chapter_paragraphs(_document(_input_of(table)).blocks)
        assert len(table["position_round_trip"]) == table["paragraph_count"], name
        for entry in table["position_round_trip"]:
            index = entry["paragraph_index"]
            stored = offset_of_paragraph(paragraphs, index)
            assert entry["offset"] == stored, f"{name}：段 {index} 的存档偏移变了"
            assert entry["offset"] == table["offsets"][index], name
            back = paragraph_at_offset(paragraphs, stored)
            assert tuple(entry["back_to"]) == back, f"{name}：段 {index} 回位不互逆"
            assert back[0] == index, f"{name}：段 {index} 存成 {stored} 读回来成了 {back[0]}"


def test_the_joiner_cell_belongs_to_the_previous_paragraph() -> None:
    """段间那一格算**前一段的段尾**，不算下一段的段首。

    否则"刚好压着换行"那一帧会把用户弹到下一段开头（``paragraph_at_offset`` 的
    docstring 明写了这一条，这里是它的表）——落在表里就得有这一格。
    """
    table = _table("reader.content")
    paragraphs = chapter_paragraphs(_golden_document().blocks)
    first_len = len(paragraphs[0])
    index, local = paragraph_at_offset(paragraphs, first_len)
    assert (index, local) == (0, first_len), (
        f"第 {first_len} 格（段 0 与段 1 之间那一格）读回 {(index, local)}，"
        "应该是段 0 的段尾")
    on_screen = [p for p in table["offset_probes"] if p["offset"] == first_len]
    assert on_screen, "参照表里没有那条'正好压在段间'的探针——重跑探针"
    assert (on_screen[0]["paragraph_index"], on_screen[0]["local"]) == (0, first_len), (
        f"表里那一格记的是 {on_screen[0]}，与现场函数不一致")


def test_offsets_out_of_range_clamp_instead_of_wrapping() -> None:
    """越界与负数夹回两端，不许绕回章首（绕回去看起来像"进度丢了"）。"""
    table = _table("reader.content")
    paragraphs = chapter_paragraphs(_golden_document().blocks)
    last = len(paragraphs) - 1
    for probe in table["offset_probes"]:
        index, local = paragraph_at_offset(paragraphs, probe["offset"])
        assert (index, local) == (probe["paragraph_index"], probe["local"]), probe
        assert 0 <= index <= last, probe
        assert 0 <= local <= len(paragraphs[index]), probe
    beyond = [p for p in table["offset_probes"] if p["offset"] > table["total_chars"]]
    assert beyond and all(p["paragraph_index"] == last for p in beyond), (
        "越界那几发没夹到末段：这一支没在测越界")
