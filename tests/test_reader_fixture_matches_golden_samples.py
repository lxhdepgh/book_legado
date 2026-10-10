"""语料对齐守卫：仓库里的假源（``tests/gateway/fake_source_reader.py``）必须还在产黄金样本那一章。

这一族守卫钉的是**靶子**，不是被测对象：黄金样本（``tests/gateway/golden/reader.*``）与
flet 落点参照表（``tests/gateway/projection/reader.content.json``）都是当初**真子进程**读
这条假源量出来的，而语料脚本本身住在 gitignore 的 ``.probe/`` 里。落点实弹臂要把同一本书
再读一遍（Dart 侧排版 ↔ flet 投影，按字符偏移比对），它需要一个跟踪在仓库里的靶子——
靶子一旦和样本漂了，实弹臂读数就会指向另一本书，而"两边一致"照样绿。

五笔账，与 ``tests/test_gateway_golden_corpus.py`` 同一族：

1. **建源那一发**：``source_dict(8797)`` 必须与 ``source.save`` 样本里的载荷逐键相等；
2. **搜索页**：按 ``ruleSearch`` 解析出的那本书，字段必须与 ``reader.open`` 载荷里的
   book 快照一致（实弹臂就是从搜索事件里拿这一份的）；
3. **目录页**：按 ``ruleToc`` 解析出的行必须与 ``reader.toc`` 样本逐行同序同名同 url；
4. **正文页**：**离线**跑引擎的那条纯管线（``_format_content_page`` →
   ``process_book_content`` → ``parse_content_blocks`` → ``display_document`` →
   ``chapter_paragraphs`` / ``paragraph_offsets``），段数/字数/偏移必须同时等于黄金样本
   与 flet 投影表——这一笔是实弹臂那把尺的锚点；
5. **形态**：把语料当子进程真起一次（``--port 0`` → stdout 的 ``PORT`` 行），验它只在
   127.0.0.1 上服务、``/g3/source.json`` 回的是带着**真绑定端口**的那份字典；顺带把
   反查那一族（``/g3web``）的两形状与 ``/hits.json`` 取页账上线核一遍；
6. **反向通道那一族的两形状**（G3-D1 的锚点）：宿主答的那一页与墙那一份过同一条纯
   管线，读数必须分别等于 ``projection/reader.content.webview.json``，且两份 ``
   looks_like_gate`` 一读真一读假。这一笔钉的是"读到 4 段只可能是宿主答的"这句话。

正对照散在第 1、4、6 笔里：换一个端口建源、往语料里多打一个字、把墙喂进宿主那把尺，
读数必须跟着动——不然这些比对可能只是在比"两边都读同一个常量"。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

import pytest
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "gateway"))

import fake_source_reader as fx  # noqa: E402
from app.core.text_layout import (  # noqa: E402
    chapter_paragraphs, chapter_text_length, paragraph_offsets)
from app.services.engine.reading_engine import (  # noqa: E402
    _format_content_page, display_document, parse_content_blocks, process_book_content)
from app.services.engine.rule_engine import BookItem, ChapterItem  # noqa: E402

GOLDEN = ROOT / "tests/gateway/golden"
PROJECTION = ROOT / "tests/gateway/projection"


def _sample(name: str) -> dict:
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


def _value(node, rule: str) -> str:
    """按引擎规则的写法（``选择器@方法``）取一条子节点的值。

    ``@`` 后面那是取值方法（text/href/src…），不是 CSS 的一部分——整条塞给 soupsieve
    会报 ``SelectorSyntaxError``；而 ``a@href`` 更不能当文本取，那样拿到的是链接文字。
    """
    selector, _, method = rule.partition("@")
    found = node.select_one(selector)
    if found is None:
        return ""
    if method in ("href", "src"):
        return str(found.get(method, "") or "")
    return found.get_text(strip=True)


def _ruler(index: int, *, body: str | None = None) -> dict:
    """离线跑一遍引擎对一章做的那几步，交出段表与偏移（不发一个请求）。

    ``body`` 是给正对照留的口子：喂一份改过的正文，看这把尺是不是真的在量语料。
    """
    chapter_url = f"{fx.base_url(fx.CANONICAL_PORT)}/ch/{index}.html"
    page = BeautifulSoup(body if body is not None else fx.chapter_html(index),
                         "html.parser")
    # 引擎的 ``@html`` 取的是 str(node)（外层），不是 inner HTML：
    # 见 rule_engine.py:1885 `(str(node) if kind == "html" else ...)`。
    value = str(page.select_one(".list"))
    book = BookItem(name="阅读基线书", author="作者戊",
                    url=f"{fx.base_url(fx.CANONICAL_PORT)}/book/1.html")
    chapter = ChapterItem(name=f"第{index}章 测试章", url=chapter_url, index=index - 1)
    formatted = _format_content_page(value, chapter_url)
    joined, title = process_book_content(formatted, book, chapter)
    document = parse_content_blocks(joined, chapter_url)
    document.title = title
    shown = display_document(document)
    paragraphs = chapter_paragraphs(shown.blocks)
    return {
        "paragraphs": paragraphs,
        "offsets": paragraph_offsets(paragraphs),
        "total": chapter_text_length(paragraphs),
        "blocks": shown.blocks,
        # 渲染前的那一版：``<usehtml>`` 还挂在里面。两侧比对的是渲染后的字，
        # 而"这一族语料真带了这个标记"必须由渲染前那一版来作证。
        "raw_blocks": document.blocks,
        "title": title,
    }


def _web_ruler(index: int, *, rendered: bool, body: str | None = None) -> dict:
    """反查那一族的同一把尺：``?rendered=1`` 是宿主交回的那一页，默认是墙。

    与 :func:`_ruler` 同一条管线，只是换书、换章、换形状——**故意不合并成一个函数**：
    这两把尺要能各自被改坏（第 6 笔的正对照靠"喂进另一形状"来动读数），合并了
    就只剩一处可改，变异核验分不清是哪一头失效。
    """
    chapter_url = f"{fx.web_base_url(fx.CANONICAL_PORT)}/ch/{index}.html"
    html = body if body is not None else fx.web_chapter_html(index, rendered=rendered)
    value = str(BeautifulSoup(html, "html.parser").select_one(".list"))
    book = BookItem(name=fx.WEB_KEYWORD, author="作者戊",
                    url=f"{fx.web_base_url(fx.CANONICAL_PORT)}/book/1.html")
    chapter = ChapterItem(name=f"第{index}章 反查章", url=chapter_url, index=index - 1)
    formatted = _format_content_page(value, chapter_url)
    joined, title = process_book_content(formatted, book, chapter)
    document = parse_content_blocks(joined, chapter_url)
    document.title = title
    shown = display_document(document)
    paragraphs = chapter_paragraphs(shown.blocks)
    return {
        "paragraphs": paragraphs,
        "offsets": paragraph_offsets(paragraphs),
        "total": chapter_text_length(paragraphs),
        "title": title,
        "raw": "".join(block.value for block in document.blocks if block.kind == "text"),
    }


def test_source_dict_is_the_payload_that_built_the_samples() -> None:
    """第 1 笔：语料字典 == 当年 ``source.save`` 真发出去的那一发载荷。"""
    payload = _sample("source.save")["request"]["payload"]["source_json"]
    assert fx.source_dict(fx.CANONICAL_PORT) == payload

    # 正对照：端口是字典的一部分，换个端口就不是同一份——否则上面那条相等
    # 只是在比"两边都不看端口"。
    assert fx.source_dict(fx.CANONICAL_PORT + 1) != payload
    # 且这一族里不许有非回环地址（探针零外网是硬规矩，记忆 legado-live-scan-av-block）。
    blob = json.dumps(fx.source_dict(fx.CANONICAL_PORT), ensure_ascii=False)
    assert "127.0.0.1" in blob
    hosts = {urljoin(fx.base_url(fx.CANONICAL_PORT), link).split("/")[2]
             for link in [fx.source_dict(fx.CANONICAL_PORT)["bookSourceUrl"],
                          fx.source_dict(fx.CANONICAL_PORT)["searchUrl"]]}
    assert hosts == {"127.0.0.1:8797"}, f"书源里出现了非回环地址：{hosts}"


def test_search_page_yields_the_book_that_reader_open_received() -> None:
    """第 2 笔：搜索页按 ``ruleSearch`` 解析出来的那本书就是 ``reader.open`` 的 book 快照。"""
    rules = fx.source_dict(fx.CANONICAL_PORT)["ruleSearch"]
    page_url = fx.base_url(fx.CANONICAL_PORT) + "/search?key=%s" % fx.KEYWORD
    soup = BeautifulSoup(fx.SEARCH_HTML, "html.parser")
    nodes = soup.select(rules["bookList"])
    assert len(nodes) == 1, "语料搜索页只该有一条结果，多了就不是当年那一发"
    node = nodes[0]
    book = {
        "name": _value(node, rules["name"]),
        "author": _value(node, rules["author"]),
        "kind": _value(node, rules["kind"]),
        "url": urljoin(page_url, _value(node, rules["bookUrl"])),
    }
    sample = _sample("reader.open")["request"]["payload"]["book"]
    # 只比规则产得出的那四个字段：其余（origin/read_config/type…）是引擎在链上补的。
    for key, value in book.items():
        assert sample[key] == value, f"{key}：语料产的是 {value!r}，样本里是 {sample[key]!r}"
    # 事件的 item[0] 与 open 的载荷必须是同一份（实弹臂从事件里取书，喂给 open 的是它）
    event_book = _sample("search.start--reader_book")["event_sample"]["payload"]["items"][0]
    assert event_book["url"] == sample["url"] and event_book["name"] == sample["name"]


def test_toc_page_matches_reader_toc_rows() -> None:
    """第 3 笔：目录页解析出的 6 行与 ``reader.toc`` 样本同序同名同 url。"""
    rules = fx.source_dict(fx.CANONICAL_PORT)["ruleToc"]
    page_url = fx.base_url(fx.CANONICAL_PORT) + "/toc.html"
    soup = BeautifulSoup(fx.TOC_HTML, "html.parser")
    rows = [{"name": _value(li, rules["chapterName"]),
             "url": urljoin(page_url, _value(li, rules["chapterUrl"]))}
            for li in soup.select(rules["chapterList"])]
    sample = _sample("reader.toc")["response"]["data"]
    assert len(rows) == fx.CHAPTER_COUNT == len(sample)
    assert rows == [{"name": row["name"], "url": row["url"]} for row in sample]
    # 正对照：行序真有意义——把目录页首尾两条对调，比对就该响。
    swapped = [rows[1], rows[0], *rows[2:]]
    assert swapped != [{"name": row["name"], "url": row["url"]} for row in sample]


def test_offline_pipeline_reproduces_the_ruler_and_the_projection() -> None:
    """第 4 笔（锚点）：离线纯管线的段数/字数/偏移 == 黄金样本 == flet 投影表。

    实弹臂要证的是"Dart 排出来的偏移与 flet 那份是同一个数"，而这条守卫先证
    "仓库里的语料还在产当年那一章"——锚点歪了，实弹臂的比对就只是两个客户端互点对错。
    """
    sample = _sample("reader.content")["response"]["data"]
    projection = json.loads(
        (PROJECTION / "reader.content.json").read_text(encoding="utf-8"))
    ruler = _ruler(2)

    assert len(ruler["paragraphs"]) == sample["paragraphCount"] == projection["paragraph_count"]
    assert ruler["total"] == sample["totalChars"] == projection["total_chars"]
    assert ruler["offsets"] == projection["offsets"]
    # 投影表的每一行都真能被这把尺对回去：ruler 那一列是段原文，display 带缩进。
    indent = projection["preferences"]["indent"]
    for row in projection["rows"]:
        index = row["paragraph_index"]
        if index is None:
            assert row["kind"] != "text" and row["display"] == ""
            continue
        assert ruler["paragraphs"][index] == row["ruler"]
        assert row["display"] == ("　　" + row["ruler"] if indent else row["ruler"])
    # 交付形态那一头：块的种类与顺序、以及那张图的**地址**能不能一路带下来。
    assert [block.kind for block in ruler["blocks"]] == [row["kind"] for row in sample["blocks"]]
    media = [block for block in ruler["blocks"] if block.kind != "text"]
    # 离线这一趟没发请求，所以图还在 ``value`` 那一格；样本那份是引擎换过字节之后的形状
    # （``value`` 空、地址搬进 ``url``，设计 §4.4）。两格指的是同一张图，而"字节 + 地址"
    # 那个形状只有真子进程才产得出——那一半归落点实弹臂验。
    assert len(media) == 1 and media[0].value == sample["blocks"][1]["url"]
    assert sample["blocks"][1]["binary"] is True and not sample["blocks"][1]["value"]
    # 正对照：往语料里多打 5 个字，这把尺必须跟着动——不动就说明它读的是样本里的常量。
    shifted = _ruler(2, body=fx.chapter_html(2).replace("第2章第3段", "第2章第3段啊啊啊啊啊"))
    assert shifted["total"] == ruler["total"] + 5
    assert shifted["offsets"][:3] == ruler["offsets"][:3]
    assert shifted["offsets"][3:] != ruler["offsets"][3:]
    # 语料不许是均匀的（记忆 measurement-corpus-must-not-be-self-proving）：
    # 段长全一样时"像素插值"与"段号导出"会算出几乎同一个数，样本钉不住契约。
    lengths = [len(paragraph) for paragraph in ruler["paragraphs"]]
    assert len(set(lengths)) >= 5, f"段长太整齐，这条臂会自证：{lengths}"


def test_corpus_still_carries_the_two_rendering_arms() -> None:
    """语料必须还埋着那两条臂：``<usehtml>`` 与 ``<img>``。

    上一条守卫比的是**渲染后**的字，而把 ``<usehtml>`` 整段删掉也可能一样对——
    那等于这条靶子悄悄退化成"任何一本干净书"，而落点契约要验的恰恰是
    "带标记的那一章两侧数出同一个字数"（``display_blocks`` 会改字数）。
    所以这里用**渲染前**那一版作证：标记在原文里、不在显示里。
    """
    ruler = _ruler(2)
    raw = "".join(block.value for block in ruler["raw_blocks"] if block.kind == "text")
    shown = "".join(block.value for block in ruler["blocks"] if block.kind == "text")
    assert "<usehtml>" in raw and "</usehtml>" in raw
    assert "<usehtml>" not in shown and "<strong>" not in shown
    assert any(block.kind != "text" for block in ruler["blocks"]), "插图那条臂没了"
    assert "<usehtml>" in fx.BOOK_HTML, "简介那一头也带标记，display_intro 的臂靠它"


def test_clamp_sample_reads_the_same_ruler() -> None:
    """第 4 笔的续：服务端夹回的那两个读数也是这把尺算出来的。

    ``reader.position_save--clamped`` 的 ``chapter_pos`` 落在全章字符流末尾、
    ``chapter_paragraph`` 落在最后一段——实弹臂拿同一份语料复跑夹回时，读到的必须是
    这两个数，否则两侧比的就不是同一把尺。
    """
    ruler = _ruler(2)
    clamped = _sample("reader.position_save--clamped")["response"]["data"]
    assert clamped["chapter_pos"] == ruler["total"]
    assert clamped["chapter_paragraph"] == len(ruler["paragraphs"]) - 1


def test_reverse_channel_shapes_are_the_anchor() -> None:
    """第 6 笔：反向通道那一族的**两种形状**过管线 == 参照表，且两者不许互换。

    D1 那一臂的判据是"屏上读到的 4 段只可能是宿主答的"。这句话的两半都在这里钉：
    ① 宿主那一版（``?rendered=1``）过管线的段数/字数/偏移必须等于表里那一份——实弹臂
    读回的数拿它比，正文不抄进 Dart 测试代码；② 墙那一版过管线**只剩 1 段 8 字**，
    所以"读到 4 段"不可能是引擎自己 HTTP 拿的。两形状的 ``looks_like_gate`` 必须
    一读为真一读为假：认不下墙，宿主永远不会被叫到；把宿主认成墙，答回来的正文回不去。
    """
    table = json.loads(
        (PROJECTION / "reader.content.webview.json").read_text(encoding="utf-8"))
    index = table["chapter_index"] + 1
    host = _web_ruler(index, rendered=True)
    wall = _web_ruler(index, rendered=False)

    assert len(host["paragraphs"]) == table["host"]["paragraph_count"]
    assert host["total"] == table["host"]["total_chars"]
    assert host["offsets"] == table["host"]["offsets"]
    assert [row["head"] for row in table["host"]["rows"]] == [
        paragraph[:16] for paragraph in host["paragraphs"]]
    assert (len(wall["paragraphs"]), wall["total"], wall["offsets"]) == (
        table["wall"]["paragraph_count"], table["wall"]["total_chars"],
        table["wall"]["offsets"])

    # 两形状不许互换：塌成一个数，"只可能是宿主答的"就又不成立（记忆
    # measurement-corpus-must-not-be-self-proving）。
    assert host["total"] != wall["total"] and len(host["paragraphs"]) != len(wall["paragraphs"])
    from app.services.block_pages import looks_like_gate
    assert looks_like_gate(fx.web_chapter_html(index, rendered=False)) is True
    assert looks_like_gate(fx.web_chapter_html(index, rendered=True)) is False

    # 触发反向通道的是目录 href 上那格 url 选项，而它必须真在语料里、且是解析出来的
    # （手抄进表就钉不住 href 改动）。
    rules = fx.web_source_dict(fx.CANONICAL_PORT)["ruleToc"]
    hrefs = [urljoin(f"{fx.web_base_url(fx.CANONICAL_PORT)}/toc.html",
                     _value(li, rules["chapterUrl"]))
             for li in BeautifulSoup(fx.WEB_TOC_HTML, "html.parser")
                     .select(rules["chapterList"])]
    assert len(hrefs) == fx.WEB_CHAPTER_COUNT == table["chapter_count"]
    assert '{"webView":true}' in hrefs[index - 1] and hrefs[index - 1].endswith(",{\"webView\":true}")
    assert hrefs[index - 1].startswith(table["chapter_url"] + ",")
    # 选项之外那一段必须正好是参照表里那一发地址（引擎剥掉选项后才拿去渲染）。
    assert hrefs[index - 1].split(",")[0] == table["chapter_url"]

    # 渲染口径那一头：宿主那一版带 <usehtml>（display 会改字数），墙那一版不带。
    assert "<usehtml>" in host["raw"] and "<usehtml>" not in wall["raw"]
    assert table["host"]["carries_usehtml"] and not table["wall"]["carries_usehtml"]

    # 正对照：把**墙**喂进宿主那把尺，读数必须塌到墙那一档——不塌就说明表里的数
    # 是从常量抄来的，而不是这把尺量出来的。
    collapsed = _web_ruler(index, rendered=True,
                           body=fx.web_chapter_html(index, rendered=False))
    assert (len(collapsed["paragraphs"]), collapsed["total"]) == (
        len(wall["paragraphs"]), wall["total"])
    # 反向也来一发：往宿主页里多打 3 个字，字数必须跟着动。
    grown = _web_ruler(index, rendered=True,
                       body=fx.web_chapter_html(index, rendered=True).replace(
                           "反查第1章第3段", "反查第1章第3段啊啊啊"))
    assert grown["total"] == host["total"] + 3


def test_fixture_serves_on_loopback_with_a_real_port() -> None:
    """第 5 笔：把语料当子进程真起一次（实弹臂用的就是这条 CLI）。

    ``--port 0`` 时打印的必须是内核挑的**真值**，而 ``/g3/source.json`` 回的那份字典
    必须带着它——Dart 那侧照着这两个数建源，字典与端口对不上就是连不上语料。
    """
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "tests" / "gateway" / "fake_source_reader.py"),
         "--port", "0"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    try:
        deadline = time.monotonic() + 20.0
        port = None
        while time.monotonic() < deadline and port is None:
            line = process.stdout.readline()
            if not line:
                break
            if line.startswith("PORT "):
                port = int(line.split()[1])
        assert port, f"没读到 PORT 行（子进程退出码 {process.poll()}）"

        source = json.loads(_read(f"http://127.0.0.1:{port}/g3/source.json"))
        assert source["bookSourceUrl"] == f"http://127.0.0.1:{port}/g3"
        assert source["ruleContent"]["content"] == ".list@html"

        body = _read(f"http://127.0.0.1:{port}/g3/ch/2.html")
        assert body == fx.chapter_html(2)
        assert _read(f"http://127.0.0.1:{port}/g3/toc.html") == fx.TOC_HTML
        picture = urllib.request.urlopen(  # noqa: S308 -- 地址是本机刚起的服务，不是用户输入
            f"http://127.0.0.1:{port}/g3/pic.jpg", timeout=10).read()
        assert picture == fx.PICTURE

        # 反查那一族也在同一台语料上服务：字典带着真绑定端口，两形状各自可取且互不相同，
        # 而取页账由语料自己记（第 6 笔那张表的两形状若能上线取到，实弹臂才有的比）。
        web = json.loads(_read(f"http://127.0.0.1:{port}/g3web/source.json"))
        assert web["bookSourceUrl"] == f"http://127.0.0.1:{port}/g3web"
        wall = _read(f"http://127.0.0.1:{port}/g3web/ch/1.html")
        host = _read(f"http://127.0.0.1:{port}/g3web/ch/1.html?rendered=1")
        assert wall == fx.web_chapter_html(1, rendered=False)
        assert host == fx.web_chapter_html(1, rendered=True) and wall != host
        # 带着 url 选项整串来取也要答得上：宿主可能拿的是引擎剥过选项的地址，
        # 也可能拿的是目录里那一整条（反向请求帧里的 url 以引擎给的为准，别赌）。
        assert _read(f"http://127.0.0.1:{port}/g3web/ch/1.html,{{\"webView\":true}}") == wall
        hits = json.loads(_read(f"http://127.0.0.1:{port}/hits.json"))
        # 三种取法各记各的键：墙按剥过选项的地址取、宿主按 ``?rendered=1`` 取、
        # 而"整条带选项的 href 直接被当路径取"是第三种。合成一个总数就答不出
        # "到底谁取的哪一版"（记忆 aggregated-counters-hide-which-call）。
        # 这本账是**这一趟子进程**的账（进程新起、计数从空开始），所以比精确次数；
        # 实弹臂那边读的是同一个接口，但那一趟里引擎也在取页，那里比的是差量。
        assert hits["/g3web/ch/1.html"] == 1
        assert hits["/g3web/ch/1.html?rendered=1"] == 1
        assert hits['/g3web/ch/1.html,{"webView":true}'] == 1
        assert hits["/g3/ch/2.html"] == 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 收尾兜底
            process.kill()


def _read(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S308
            return response.read().decode("utf-8")
    except urllib.error.URLError as exc:  # pragma: no cover - 失败信息要能看出口
        raise AssertionError(f"{url} 取不回来：{exc}") from exc


pytestmark = pytest.mark.skipif(
    not (ROOT / "tests/gateway/golden/reader.content.json").exists(),
    reason="先跑 .probe/g3_golden_capture.py 产黄金样本")
