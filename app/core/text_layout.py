"""自然段切分：显示与索引共用的一把尺子。

正文"一段"是谁定义的，决定了三件事能不能对上：屏幕上挂出来的第 k 段、书内全文索引
里的第 k 行、以及搜索命中后要滚到的第 k 段。所以切段这份纯计算必须放在视图和服务
都够得着的地方——红线 §7.5 规定 ``views/reading`` 不 import ``app.services.*``，
而 ``app.core.*`` 两边都可以 import，就只有这里放得下。

标题识别与段首缩进是显示的事，不在本模块：索引不需要，加了反而两边算出的段长不一致。
"""
from __future__ import annotations

import re


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
