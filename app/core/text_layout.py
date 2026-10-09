"""自然段切分与章内坐标：显示、索引与阅读进度共用的一把尺。

正文"一段"是谁定义的，决定了四件事能不能对上：屏幕上挂出来的第 k 段、书内全文索引
里的第 k 行、搜索命中后要滚到的第 k 段，以及**落库的字符偏移**指向哪一段。所以切段
这份纯计算必须放在视图和服务都够得着的地方——红线 §7.5 规定 ``views/reading`` 不
import ``app.services.*``，而 ``app.core.*`` 两边都可以 import，就只有这里放得下。

字符偏移那一头是迁移 G3 的位置契约（设计 §9）：客户端（flet / Dart）各自排版，像素
属于当时那一套装尺，只有"章内正文字符流里的第几个字"在两侧是同一个数。#88 现场取证
过老那条 ``chapter_pos`` 不能用——它是**像素比例 × 实测字数**插出来的，连同尺都比真
段号差 7 段（``tests/test_reader_position_paragraph.py`` 取证一）。这一模块给的是它
的正确形式：偏移由段号**算**出来，不是由像素**猜**出来。

标题识别与段首缩进是显示的事，不在本模块：索引与坐标都不需要，加了反而两边算出的
段长不一致（缩进每段多两个字，偏移就整体漂）。
"""
from __future__ import annotations

import re
from bisect import bisect_right
from typing import Iterable


def paragraph_lines(value) -> list[str]:
    """正文原文 → 自然段文本列表（不缩进、不认标题）。

    口径与 ``ReaderView.novel_paragraphs`` 完全一致：换行符统一、``\\xa0`` 当普通
    空格、行内连续空白压成一个空格、去首尾空白、空行整条丢掉。
    """
    normalized = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\xa0", " ")
    lines: list[str] = []
    for raw_line in normalized.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return lines


# --------------------------------------------------------------------------- #
# 章内字符坐标（设计 §9 门 G3 的位置契约）
# --------------------------------------------------------------------------- #
#: 段与段之间在字符流里占一格——与 :func:`chapter_text` 的那个换行是同一格。
PARAGRAPH_JOINER_LEN = 1


def chapter_paragraphs(blocks) -> list[str]:
    """正文块 → 本章自然段列表（口径与 ``ReaderView.content_plan`` 逐字一致）。

    吃的必须是**可显示形态**的块（``<usehtml>`` 已渲染过），两侧才拿得到同一串字：
    引擎原文里的 ``<usehtml>`` 标记会被 :func:`paragraph_lines` 当普通文本切进段里，
    渲染掉之后段长就变了，偏移跟着变。这条口径由 ``display_blocks`` 一处保证。

    媒体块（image/audio/video）不产段：它们挂在正文之间但不参与"第几段"的计数，
    与视图的段顶表同一个口径（排版重排也只认正文段）。
    """
    paragraphs: list[str] = []
    for block in blocks or []:
        if str(getattr(block, "kind", "") or "") != "text":
            continue
        if not (getattr(block, "value", "") or ""):
            continue
        paragraphs.extend(paragraph_lines(getattr(block, "value", "")))
    return paragraphs


def paragraph_offsets(paragraphs: Iterable[str]) -> list[int]:
    """每段段首在章内字符流里的偏移（``"\\n".join(paragraphs)`` 的下标）。

    吃一整串正文是要防的那件错事：``list("第一段第二段")`` 会安静地变成逐字列表，
    算出来的"段首"全是 1，两侧各错各的还能互相绿。
    """
    offsets: list[int] = []
    cursor = 0
    for text in _as_paragraph_list(paragraphs):
        offsets.append(cursor)
        cursor += len(text) + PARAGRAPH_JOINER_LEN
    return offsets


def _as_paragraph_list(paragraphs: Iterable[str]) -> list[str]:
    """段列表的形状检查：必须是"一串 str"，不许是一整串正文，也不许混进非文本。"""
    if isinstance(paragraphs, str):
        raise TypeError("坐标吃的是自然段列表，不是整章正文——先调 chapter_paragraphs")
    items = list(paragraphs)
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"自然段必须是 str，收到 {type(item).__name__}")
    return items


def chapter_text_length(paragraphs: Iterable[str]) -> int:
    """本章字符流总长（空章为 0；不造"至少一个字"那种假下限）。"""
    texts = _as_paragraph_list(paragraphs)
    if not texts:
        return 0
    return sum(len(text) for text in texts) + PARAGRAPH_JOINER_LEN * (len(texts) - 1)


def offset_of_paragraph(paragraphs: Iterable[str], index: int) -> int:
    """段序号 → 段首字符偏移；越界 clamp 到末尾，负数回 0。

    这是**存档**方向：落库的字符偏移必须由段号算出来，不是由像素猜出来。
    """
    texts = _as_paragraph_list(paragraphs)
    if not texts:
        return 0
    if index <= 0:
        return 0
    if index >= len(texts):
        return chapter_text_length(texts)
    return sum(len(text) + PARAGRAPH_JOINER_LEN for text in texts[:index])


def paragraph_at_offset(paragraphs: Iterable[str], offset: int) -> tuple[int, int]:
    """字符偏移 → ``(段序号, 段内偏移)``；空章回 ``(0, 0)``，越界 clamp。

    这是**回位**方向，与 :func:`offset_of_paragraph` 互逆：
    ``paragraph_at_offset(paragraphs, offset_of_paragraph(paragraphs, i))[0] == i``。
    落在段间那一格换行上算**前一段的段尾**（``local == len(段)``），不算下一段的段首——
    否则同一次滚动会在"刚好压着换行"那一帧把用户弹到下一段开头。
    """
    texts = _as_paragraph_list(paragraphs)
    if not texts:
        return 0, 0
    offsets = paragraph_offsets(texts)
    position = max(0, int(offset))
    index = bisect_right(offsets, position) - 1
    if index < 0:
        index = 0
    local = position - offsets[index]
    return index, min(local, len(texts[index]))
