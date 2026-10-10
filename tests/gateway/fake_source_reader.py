"""G3 阅读面语料：一条四页齐全的回环假源，**跟踪在仓库里**的那一份。

为什么要有一份在仓库里的：这条语料原来只活在 ``.probe/g3_golden_capture.py``（gitignore
的探针目录）里，于是"落点实弹臂"——真 ``--serve`` 子进程读同一本书、两侧按字符偏移比
翻页落点——没有靶子可用： Dart 那侧要么自己搓一份正文（搓出来的就是它自己想要的读数，
记忆里 ``measurement-corpus-must-not-be-self-proving`` 那一笔账），要么依赖一个不存在
的探针脚本。语料本身是契约的一部分，它得和被它钉住的样本一样进版本库。

四页齐全是硬要求（搜索页→书籍页→目录页→正文页）：``reader.open`` 走的是
getBookInfo + getToc + getContent 这条完整链；而正文页里同时埋了 ``<usehtml>`` 与
``<img>``——前者验"两侧渲染口径是同一份"（``display_blocks``，那一步会改字数），
后者验"媒体块不产自然段"（``chapter_paragraphs``）。

**``/g3web`` 是另一族**（§8 R1 反向 WebView 通道的靶子）：章 url 的 href 上挂着 Legado 的
``,{"webView":true}`` 选项，同一个路径有**两种形状**——不带 query 是登录墙
（``block_pages.looks_like_gate`` 认得下它，引擎据此才会把这次渲染推给远端宿主），
``?rendered=1`` 才是"带会话的可见宿主渲染之后"的那一页。两形状必须同时在：只有一页正文
的话，屏上那 4 段到底是宿主答的还是引擎自己 HTTP 拿的，就无从可证
（``measurement-corpus-must-not-be-self-proving``）。取页账在 ``GET /hits.json``：
谁取了哪一版由语料自己记，不由取页的那一方自报。

``port`` 只出现在 :func:`source_dict` 里：页面内的 href 全是相对路径，引擎按请求 URL
拼接，所以换端口不用改语料。``--port 0`` 让内核挑端口并把真值打在 stdout
（``PORT <n>``），同时 ``GET /g3/source.json`` 与 ``GET /g3web/source.json`` 各回同一份
书源字典——非 Python 的宿主（Dart 那条实弹臂）照着这两个读数建源，**不必抄写规则**。

用法：``python tests/gateway/fake_source_reader.py --port 8797``（只绑 127.0.0.1）
"""
from __future__ import annotations

import argparse
import asyncio
import json

from aiohttp import web

#: 黄金样本建源时用的那个端口：样本里的 url 带着它（``http://127.0.0.1:8797/g3``）。
CANONICAL_PORT = 8797
CHAPTER_COUNT = 6
GROUP = "G3离线"
KEYWORD = "阅读基线书"

SEARCH_HTML = ('<ul class="list"><li><a href="/g3/book/1.html">阅读基线书</a>'
               '<i>作者戊</i><span>连载</span></li></ul>')
BOOK_HTML = """<html><body>
<h1>阅读基线书</h1><p class="author">作者戊</p>
<p class="intro">这是 G3 阅读面语料的书简介，带 <usehtml>内嵌标记</usehtml>。</p>
<a class="toc" href="/g3/toc.html">目录</a><p class="last">第 6 章</p>
</body></html>
"""
TOC_HTML = ("<html><body><ul>" + "".join(
    f'<li><a href="/g3/ch/{i}.html">第{i}章 测试章</a></li>'
    for i in range(1, CHAPTER_COUNT + 1)) + "</ul></body></html>")
PICTURE = b"\xff\xd8\xff\xe0G3"


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/g3"


def chapter_html(index: int) -> str:
    """一章正文：自然段长度**故意不均匀**，且带 <usehtml> 与一张插图。

    长度均匀是 ``measurement-corpus-must-not-be-self-proving`` 那笔账：段长一样的语料
    里"像素插值"与"段号导出"两种算法会算出几乎同一个数，样本就钉不住契约。
    """
    lengths = (7, 61, 13, 94, 5, 48, 22)
    paras = [f"第{index}章第{n}段" + "字句读纸张看灯下山风月夜声光影花雪路桥水田家园" * count
             for n, count in enumerate(lengths, start=1)]
    body = ("<usehtml><p><strong>" + paras[0] + "</strong></p>"
            "<p>" + paras[1] + "</p></usehtml>\n"
            + "\n".join(f"<p>{text}</p>" for text in paras[2:4])
            + '\n<p><img src="/g3/pic.jpg"></p>\n'
            + "\n".join(f"<p>{text}</p>" for text in paras[4:])
            + '\n<a class="next" href="/g3/ch/%d.html">下一章</a>'
            % (index + 1 if index < CHAPTER_COUNT else index))
    return f"<html><body><div class='list'>{body}</div></body></html>"


def source_dict(port: int) -> dict:
    """这一族页面对应的书源规则。最后一章的 ``nextContentUrl`` 自指，引擎按 visited 断开。"""
    url = base_url(port)
    return {
        "bookSourceName": "G3阅读源",
        "bookSourceUrl": url,
        "bookSourceGroup": GROUP,
        "bookSourceType": 0,
        "enabled": True,
        "enabledExplore": False,
        "searchUrl": f"{url}/search?key={{{{key}}}}",
        "ruleSearch": {"bookList": "ul.list li", "name": "a@text",
                       "bookUrl": "a@href", "author": "i@text", "kind": "span@text"},
        "ruleBookInfo": {"name": "h1@text", "author": "p.author@text",
                         "intro": ".intro@text", "tocUrl": "a.toc@href",
                         "lastChapter": "p.last@text"},
        "ruleToc": {"chapterList": "li", "chapterName": "a@text",
                    "chapterUrl": "a@href"},
        "ruleContent": {"content": ".list@html", "nextContentUrl": "a.next@href"},
    }


# --------------------------------------------------------------------------- #
# 反查那一支：章 url 上挂着 Legado 的 ``url,{"webView":true}`` 选项
# --------------------------------------------------------------------------- #
#: 这一族是给 §8 R1 那条反向通道当靶子的：引擎取正文时必须走 ``webview_fetcher``
#: （设计里"可见 WebView 归客户端"那一条），而``/ws`` 上那一发 ``request`` 帧的
#: ``payload.url`` 就是下面这个带选项的地址。**选项写在 href 里而不是写在规则里**，
#: 因为真源就是这么写的（语料普查：56 条带 webView 选项），规则里加这一格反而不像真形状。
WEB_KEYWORD = "阅读反查书"
WEB_CHAPTER_COUNT = 2
WEB_GROUP = "G3反向"

WEB_SEARCH_HTML = ('<ul class="list"><li><a href="/g3web/book/1.html">阅读反查书</a>'
                   '<i>作者戊</i><span>连载</span></li></ul>')
WEB_BOOK_HTML = """<html><body>
<h1>阅读反查书</h1><p class="author">作者戊</p>
<p class="intro">这一本的反查页要由宿主渲染，简介是普通 HTTP 拿得到的。</p>
<a class="toc" href="/g3web/toc.html">目录</a><p class="last">第 2 章</p>
</body></html>
"""
WEB_TOC_HTML = ("<html><body><ul>" + "".join(
    f'<li><a href=\'/g3web/ch/{i}.html,{{"webView":true}}\'>第{i}章 反查章</a></li>'
    for i in range(1, WEB_CHAPTER_COUNT + 1)) + "</ul></body></html>")


def web_base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/g3web"


#: 不加 `?rendered=1` 时那一页长什么样：**登录墙**。这是 R1 那一族源的真形状
#: （内容要由带会话的可见 WebView 才拿得到），也是引擎侧选择链的开关——
#: `BrowserService._silent_render` 渲染完先问 `block_pages.looks_like_gate`，
#: 认下这堵墙才会把这一次渲染推给远端宿主（``/ws`` 的 ``request`` 帧）。
#: 墙里也放着 ``.list``：正文规则在墙上**能抽到东西**（一行提示语），这样
#: "读到 4 段" 才只能是宿主答的、不可能是"引擎自己 HTTP 拿的但抽干了"。
WEB_WALL_HTML = """<html><body><div class='list'>
<p>阅读正文需要登录</p>
<form><input type="password" name="pwd" placeholder="口令"></form>
</div></body></html>
"""


def web_chapter_html(index: int, *, rendered: bool = False) -> str:
    """反查章：``rendered=False`` 是墙（普通渲染拿到的），``True`` 是宿主交回的那一页。

    两种形状必须**同时存在**，不然这一族语料就是自证的：只有一页正文的话，
    "宿主被叫到了"与"引擎自己 HTTP 拿的"读出来是同一个数（记忆
    ``measurement-corpus-must-not-be-self-proving``）。墙用登录表单当标记，
    宿主页的段数与段长又都与 ``chapter_html`` 不同——三处各不相同，落点才认得出谁。

    故意不同：如果两族的正文一模一样，"反查那一臂真的过了一趟宿主"就无从可证——
    屏上读到与 G3 那一章同样的 8 段 / 5,802 字，分不清是宿主答的还是引擎自己 HTTP 拿的。
    """
    if not rendered:
        return WEB_WALL_HTML
    lengths = (4, 37, 11, 55)
    paras = [f"反查第{index}章第{n}段" + "雪落窗台灯火远河桥渡口船笛声寒山有雨" * count
             for n, count in enumerate(lengths, start=1)]
    body = ("<usehtml><p>" + paras[0] + "</p><p>" + paras[1] + "</p></usehtml>\n"
            + "\n".join(f"<p>{text}</p>" for text in paras[2:]))
    return f"<html><body><div class='list'>{body}</div></body></html>"


#: 取页账：键是"路径 + 是否渲染后那一版"，值是次数。实弹臂读它来分清
#: "引擎自己渲染了墙"与"宿主取回了正文"分别是几次（记忆 aggregated-counters-hide-which-call：
#: 一个总数回答不了"到底谁取的"）。
HITS: dict[str, int] = {}


def _hit(path: str, *, rendered: bool = False) -> None:
    key = f"{path}{'?rendered=1' if rendered else ''}"
    HITS[key] = HITS.get(key, 0) + 1


def web_source_dict(port: int) -> dict:
    """反查那一支的书源：只有正文规则，没有 ``nextContentUrl``（不追加，一页一章）。"""
    url = web_base_url(port)
    return {
        "bookSourceName": "G3反查源",
        "bookSourceUrl": url,
        "bookSourceGroup": WEB_GROUP,
        "bookSourceType": 0,
        "enabled": True,
        "enabledExplore": False,
        "searchUrl": f"{url}/search?key={{{{key}}}}",
        "ruleSearch": {"bookList": "ul.list li", "name": "a@text",
                       "bookUrl": "a@href", "author": "i@text", "kind": "span@text"},
        "ruleBookInfo": {"name": "h1@text", "author": "p.author@text",
                         "intro": ".intro@text", "tocUrl": "a.toc@href",
                         "lastChapter": "p.last@text"},
        "ruleToc": {"chapterList": "li", "chapterName": "a@text",
                    "chapterUrl": "a@href"},
        "ruleContent": {"content": ".list@html"},
    }



# --------------------------------------------------------------------------- #
# 连续那一支：三章**形状互不相同**，专给"接上下一章之后落点归谁"当靶子
# --------------------------------------------------------------------------- #
#: §9 G3-D2 的语料。为什么不能拿 ``/g3`` 那一族接两章就完事：``chapter_html`` 对每一章
#: 用的是**同一串** ``lengths``，所以第 1 章与第 2 章的段数（7）与每段长度**全同**——
#: 那时"忘了把整篇段号减成本章起点"与"减对了"会算出同一个 ``character_offset``，
#: 只有章号能区分（记忆 ``measurement-corpus-must-not-be-self-proving``）。
#: 所以这一族三章各是一种形状：12 短段 / 3 长段带一张图（**媒体占行不产段**，
#: 追加起点要是把行当段算就连第一站都对不上）/ 7 段且首行是标题形状。
#: 章与章的段数（12 / 3 / 7）与段长都不同，段起点因此是 0 / 12 / 15 三个不同的数。
#:
#: 这一族**不带 ``nextContentUrl``**（与 ``/g3web`` 同策）：要验的是**客户端**按目录
#: 序号接下一章，而引擎那条 Legado 章尾合并会把两章并成一章的正文，那条路一走
#: "接来的那一章"就不存在了。
CONT_KEYWORD = "连续阅读书"
CONT_CHAPTER_COUNT = 3
CONT_GROUP = "G3连续"
CONT_STEM = "字句读纸张看灯下山风"

CONT_SEARCH_HTML = ('<ul class="list"><li><a href="/g3cont/book/1.html">连续阅读书</a>'
                    '<i>作者戊</i><span>连载</span></li></ul>')
CONT_BOOK_HTML = """<html><body>
<h1>连续阅读书</h1><p class="author">作者戊</p>
<p class="intro">这一本要连着读：三章形状各异，接上之后段起点会换。</p>
<a class="toc" href="/g3cont/toc.html">目录</a><p class="last">第 3 章</p>
</body></html>
"""
CONT_TOC_HTML = ("<html><body><ul>" + "".join(
    f'<li><a href="/g3cont/ch/{i}.html">第{i}章 连续章</a></li>'
    for i in range(1, CONT_CHAPTER_COUNT + 1)) + "</ul></body></html>")

#: 每一章的段构造：``(每段重复次数, ...)`` 三章长度互不相同；``usehtml`` 只在第 1 章
#: 前两行（验"显示形态改了字数"这件事在追加文档里也不算进尺），``picture_after``
#: 指插图插在**第几段之后**（第 2 章插在 1 之后：那一行上屏、不产段）。
CONT_SHAPES: dict[int, dict] = {
    1: {"counts": (1, 2, 1, 3, 2, 1, 4, 2, 3, 1, 2, 1), "usehtml": 2,
        "picture_after": None, "heading": False},
    2: {"counts": (24, 31, 18), "usehtml": 0, "picture_after": 1, "heading": False},
    3: {"counts": (5, 2, 9, 1, 6, 3), "usehtml": 0, "picture_after": None,
        "heading": True},
}


def cont_chapter_html(index: int) -> str:
    """连续章：第 1 章 12 短段（带 ``<usehtml>``）、第 2 章 3 长段带插图、第 3 章首行是标题。"""
    shape = CONT_SHAPES[index]
    paras = [f"连续第{index}章第{n}段" + CONT_STEM * count
             for n, count in enumerate(shape["counts"], start=1)]
    if shape["heading"]:
        paras.insert(0, f"第{index}章 收梢")     # 认得出标题，也照样占第 0 段
    pieces: list[str] = []
    usehtml = shape["usehtml"]
    if usehtml:
        inner = ("<p><strong>" + paras[0] + "</strong></p>"
                 "<p>" + paras[1] + "</p>")
        pieces.append(f"<usehtml>{inner}</usehtml>")
        rest = paras[usehtml:]
    else:
        rest = paras
    picture_after = shape["picture_after"]
    for position, text in enumerate(rest):
        pieces.append(f"<p>{text}</p>")
        if picture_after is not None and position == picture_after - 1:
            pieces.append('<p><img src="/g3cont/pic.jpg"></p>')
    body = "\n".join(pieces)
    return f"<html><body><div class='list'>{body}</div></body></html>"


def cont_source_dict(port: int) -> dict:
    """连续那一支的书源：正文规则只有 ``content``（不追章尾，见上面的注释）。"""
    url = f"http://127.0.0.1:{port}/g3cont"
    return {
        "bookSourceName": "G3连续源",
        "bookSourceUrl": url,
        "bookSourceGroup": CONT_GROUP,
        "bookSourceType": 0,
        "enabled": True,
        "enabledExplore": False,
        "searchUrl": f"{url}/search?key={{{{key}}}}",
        "ruleSearch": {"bookList": "ul.list li", "name": "a@text",
                       "bookUrl": "a@href", "author": "i@text", "kind": "span@text"},
        "ruleBookInfo": {"name": "h1@text", "author": "p.author@text",
                         "intro": ".intro@text", "tocUrl": "a.toc@href",
                         "lastChapter": "p.last@text"},
        "ruleToc": {"chapterList": "li", "chapterName": "a@text",
                    "chapterUrl": "a@href"},
        "ruleContent": {"content": ".list@html"},
    }


# --------------------------------------------------------------------------- #
# 换源那一族：六条源，**三条该被扫、两条不该、一条就是当前那一条**（§9 G3-D3 的靶子）
# --------------------------------------------------------------------------- #
#: 为什么换源要一整套源而不是复用 ``/g3`` 那一条：``switch.scan`` 的读数不是
#: "有没有找到书"，而是**这一轮扫了几条源、按什么顺序排、谁被过滤掉了**。
#: 一条源做不了这三件事——过滤那一步在只有一条源的语料上，"排除停用的"与
#: "把停用的也扫了"读出来都是"1 个候选"（记忆 ``measurement-corpus-must-not-be-self-proving``）。
#:
#: 六条各管一格：
#:   * ``x`` 这本书**当前挂着**的那条源：它照样精确命中同名同作者，而队列里不能有它。
#:     为什么给它一个精确命中而不是随便一条：万一"排除当前源"断了，它的行会排在
#:     候选**第一格**（排序第一判据），样本与守卫一眼就读得出"换成自己了"；给它一条
#:     命不中的源，断了也读不出来（记忆 ``zero-counts-need-a-positive-control``）。
#:   * ``a`` 两条命中——``换源目标书``（书名精确 + 作者对得上）与 ``换源目标书 卷二``
#:     （书名只**包含**关键词）：排序那一步要的就是这两种命中之差。
#:   * ``b`` 一条命中，书名精确而**作者不同**：排第三判据（作者只当加分项）的证据。
#:   * ``c`` 一条都不命中（只有 ``旁书一本``）：这一条**要**被扫，``total`` 里得有它，
#:     而候选里不能有它——"扫过但没找到"与"没扫"必须是两个数。
#:   * ``d`` 与 ``a`` 同样会命中，但 ``enabled=False``；``e`` 同样会命中，但没有
#:     ``searchUrl``。这两条是**过滤那一步的对照臂**：它们的页面照样在服务器上，
#:     只是引擎一次都没来取过——``GET /hits.json`` 里读不出 ``/g3sw/d/search`` 这一格
#:     （没有键 = 没被取过），而 ``/g3sw/a/search`` 有数
#:     （记忆 ``zero-counts-need-a-positive-control``：0 要有非 0 的同伴才叫读数）。
SW_TARGET = "换源目标书"
SW_GROUP = "G3换源"
SW_AUTHOR = "作者戊"
#: 每个 stem 一章几段、有几章：章数不同的两条源才是"换成乙之后目录真的换了"的靶子
#: ——目录条数若处处相同，换源写回那条链就只是把 url 换了个名字。
SW_STEMS = ("a", "b", "c", "d", "e", "x")
SW_ROWS: dict[str, list[tuple[str, str]]] = {
    "a": [(SW_TARGET, SW_AUTHOR), (SW_TARGET + " 卷二", SW_AUTHOR)],
    "b": [(SW_TARGET, "作者己")],
    "c": [("旁书一本", "作者庚")],
    "d": [(SW_TARGET, SW_AUTHOR)],
    "e": [(SW_TARGET, SW_AUTHOR)],
    "x": [(SW_TARGET, SW_AUTHOR)],
}
SW_CHAPTERS: dict[str, int] = {"a": 3, "b": 2, "c": 1, "d": 3, "e": 3, "x": 4}
SW_CURRENT = "x"
SW_DISABLED = "d"
SW_NO_SEARCH_URL = "e"


def sw_base_url(port: int, stem: str) -> str:
    return f"http://127.0.0.1:{port}/g3sw/{stem}"


def sw_search_html(stem: str) -> str:
    items = "".join(
        f'<li><a href="/g3sw/{stem}/book/{i}.html">{name}</a>'
        f'<i>{author}</i><span>连载</span></li>'
        for i, (name, author) in enumerate(SW_ROWS[stem], start=1))
    return f'<ul class="list">{items}</ul>'


def sw_book_html(stem: str, index: int) -> str:
    name, author = SW_ROWS[stem][index - 1]
    return (f"<html><body><h1>{name}</h1><p class=\"author\">{author}</p>"
            f"<p class=\"intro\">这是 {stem} 那一条源上《{name}》的简介。</p>"
            f"<a class=\"toc\" href=\"/g3sw/{stem}/toc.html\">目录</a>"
            f"<p class=\"last\">第 {SW_CHAPTERS[stem]} 章</p></body></html>")


def sw_toc_html(stem: str) -> str:
    """目录：章名带**源的名字**——换成另一条源之后目录行必须换，否则"真换了源"无从可证。"""
    return ("<html><body><ul>" + "".join(
        f'<li><a href="/g3sw/{stem}/ch/{i}.html">第{i}章 {stem}源章</a></li>'
        for i in range(1, SW_CHAPTERS[stem] + 1)) + "</ul></body></html>")


def sw_chapter_html(stem: str, index: int) -> str:
    """正文：三条自然段，段长按 stem 变（三条源的正文互不相同，落点尺才认得出是谁）。"""
    counts = (2, 5, 3) if stem in ("a", "d", "e") else (4, 1, 6, 2)
    paras = [f"{stem}源第{index}章第{n}段" + "山风月夜声光影花雪路桥水田家园" * count
             for n, count in enumerate(counts, start=1)]
    body = "\n".join(f"<p>{text}</p>" for text in paras)
    return f"<html><body><div class='list'>{body}</div></body></html>"


def sw_source_dict(port: int, stem: str) -> dict:
    """一条换源语料源。``d`` 停用、``e`` 没有 ``searchUrl``——那两格就是过滤的靶子。"""
    url = sw_base_url(port, stem)
    source = {
        "bookSourceName": f"G3换源{stem}",
        "bookSourceUrl": url,
        "bookSourceGroup": SW_GROUP,
        "bookSourceType": 0,
        "enabled": stem != SW_DISABLED,
        "enabledExplore": False,
        "ruleSearch": {"bookList": "ul.list li", "name": "a@text",
                       "bookUrl": "a@href", "author": "i@text", "kind": "span@text"},
        "ruleBookInfo": {"name": "h1@text", "author": "p.author@text",
                         "intro": ".intro@text", "tocUrl": "a.toc@href",
                         "lastChapter": "p.last@text"},
        "ruleToc": {"chapterList": "li", "chapterName": "a@text",
                    "chapterUrl": "a@href"},
        "ruleContent": {"content": ".list@html"},
    }
    # 停用与无规则**分两格**：合成一条源就只能证明"有一条源没参加"，
    # 而两个过滤分支（enabled / searchUrl）各自都得有自己的 witness。
    if stem != SW_NO_SEARCH_URL:
        source["searchUrl"] = f"{url}/search?key={{{{key}}}}"
    return source


def sw_source_list(port: int) -> list[dict]:
    """六条源一次交付：非 Python 的宿主（Dart 那条实弹臂）照这一发建源，不抄规则。"""
    return [sw_source_dict(port, stem) for stem in SW_STEMS]


async def sw_response(request: web.Request) -> web.Response | None:
    """``/g3sw`` 那一族的路由。不匹配时返回 ``None``，由调用方继续往下分发。

    为什么抽成函数而不只挂在 :func:`create_app` 里：黄金样本那一趟
    （``.probe/g3_golden_capture.py``）为了同端口供 ``/g2`` 用的是它自己的 inline 应用，
    换源这一族得挂到**同一个端口**上，才能和写进 ``search_books`` 的那些 url 对上。
    两处各写一遍路由，迟早有一处漏掉一条臂（这一族的对照全靠路径分得清）。
    """
    path = request.path
    if path == "/g3sw/sources.json":
        return web.json_response(sw_source_list(request.app["source_port"]))
    if not path.startswith("/g3sw/"):
        return None
    parts = path.split("/")
    stem = parts[2] if len(parts) > 2 else ""
    # 按**那一段的名字**比（去掉 ``.html`` 后缀），既不按整段前缀比也不兜底：
    # ``/g3sw/a/book/1.html`` 切出来第 3 段是 ``book``（不含斜杠），拿
    # ``startswith("book/")`` 去认永远认不到，书籍页会一路掉到末尾那一支被答成
    # 搜索页——页面上没有 ``a.toc``，引擎取不到 tocUrl，于是目录塌成一章，
    # 而这条链的错只显在"章数对不上"那里，看着像语料配错了。
    # 反过来，写成"其余都算搜索页"会让 ``/g3sw/a/toc`` 少写后缀时答成搜索页，
    # 目录就永远只剩一行——两种写法都得**指名**才分得清谁没接上。
    tail = parts[3] if len(parts) > 3 else ""
    name = tail.split(".", 1)[0]
    if stem not in SW_STEMS:
        # 不落到调用方那一支 fallthrough：那会把换源族的未知地址答成
        # /g3 的搜索页，于是"这条源没配规则"与"路径写错了"读出来一样。
        return web.Response(status=404)

    def _index(bound: int) -> int:
        digits = "".join(c for c in (parts[4] if len(parts) > 4 else "")
                         if c.isdigit())
        # 夹到**各自**的边界：书籍页有几条命中行、正文页有几章，是两个数。
        # 拿命中行数去夹章号会让 ``/g3sw/x/ch/3.html`` 答成第 1 章（x 只有一条命中），
        # 于是"换成另一条源之后正文真的换了"那条判据读的是同一份字节。
        return max(1, min(int(digits or 1), max(bound, 1)))

    if name == "book":
        return web.Response(text=sw_book_html(stem, _index(len(SW_ROWS[stem]))),
                            content_type="text/html")
    if name == "toc":
        return web.Response(text=sw_toc_html(stem), content_type="text/html")
    if name == "ch":
        _hit(path)
        return web.Response(text=sw_chapter_html(stem, _index(SW_CHAPTERS[stem])),
                            content_type="text/html")
    if name == "search" or name == "":
        # 搜索页（``/{stem}/search?key=…``；``/{stem}`` 本身也给搜索形状）。
        # **必须记账**：过滤那两条对照臂判的是"引擎来没来过"，而来没来过只有语料
        # 这一边说得清。
        # 认**这一段**而不是"其余都算搜索"：``/g3sw/x/toc`` 少写一个 ``.html``
        # 就会被兜成搜索页，于是目录永远只有一行（那一个 ``li`` 指向 book 页），
        # 而错的形状看着像"这条源的目录就这么短"。
        _hit(path)
        return web.Response(text=sw_search_html(stem), content_type="text/html")
    return web.Response(status=404)


def create_app(port: int) -> web.Application:
    async def handler(request: web.Request) -> web.Response:
        path = request.path
        if path == "/hits.json":
            # 谁在什么时候取过哪一页，由**语料**记账，不由取页的那一方自报。
            # 反向通道那一臂要分清"引擎自己渲染过墙"与"宿主取过正文"，
            # 让宿主来回答"有没有被叫到"等于让仪器自己声明自己接上了
            # （记忆 zero-counts-need-a-positive-control）。
            return web.json_response(dict(HITS))
        if path == "/g3/source.json":
            # 字典里的 url 必须带着**真绑定**端口：--port 0 时传进来的就是 0，
            # 宿主（Dart 那条实弹臂）照着 PORT 行连过来，字典却指向 :0 就是连不上语料。
            return web.json_response(source_dict(request.app["source_port"]))
        if path == "/g3web/source.json":
            return web.json_response(web_source_dict(request.app["source_port"]))
        if path == "/g3cont/source.json":
            return web.json_response(cont_source_dict(request.app["source_port"]))
        sw = await sw_response(request)
        if sw is not None:
            return sw
        if path.startswith("/g3cont/ch/"):
            digits = "".join(char for char in path.rsplit("/", 1)[-1] if char.isdigit())
            number = int(digits or 1)
            _hit(path)
            return web.Response(text=cont_chapter_html(number), content_type="text/html")
        if path.startswith("/g3cont/pic"):
            return web.Response(body=PICTURE, content_type="image/jpeg")
        if path.startswith("/g3cont/toc"):
            return web.Response(text=CONT_TOC_HTML, content_type="text/html")
        if path.startswith("/g3cont/book/"):
            return web.Response(text=CONT_BOOK_HTML, content_type="text/html")
        if path.startswith("/g3cont/"):
            return web.Response(text=CONT_SEARCH_HTML, content_type="text/html")
        if path.startswith("/g3/ch/"):
            number = int(path.rsplit("/", 1)[-1].split(".")[0])
            _hit(path)
            return web.Response(text=chapter_html(number), content_type="text/html")
        if path.startswith("/g3web/ch/"):
            # 章 url 上挂着 ``{"webView":true}``，选项被引擎拆掉之后剩下的就是这一段；
            # 没拆干净（宿主直接拿整条地址来取页）也要能答，所以按数字取而不按整串匹配。
            digits = "".join(char for char in path.rsplit("/", 1)[-1] if char.isdigit())
            # 默认那一发是**墙**；``?rendered=1`` 才是"可见宿主渲染完会话之后"的那一页。
            # 脚本化宿主（实弹臂）就靠这个查询参数作答，不必在测试代码里抄正文。
            rendered = request.query.get("rendered") == "1"
            _hit(path, rendered=rendered)
            return web.Response(text=web_chapter_html(int(digits or 1), rendered=rendered),
                                content_type="text/html")
        if path.startswith("/g3/toc"):
            return web.Response(text=TOC_HTML, content_type="text/html")
        if path.startswith("/g3web/toc"):
            return web.Response(text=WEB_TOC_HTML, content_type="text/html")
        if path.startswith("/g3/book/"):
            return web.Response(text=BOOK_HTML, content_type="text/html")
        if path.startswith("/g3web/book/"):
            return web.Response(text=WEB_BOOK_HTML, content_type="text/html")
        if path.startswith("/g3/pic"):
            return web.Response(body=PICTURE, content_type="image/jpeg")
        if path.startswith("/g3web/"):
            return web.Response(text=WEB_SEARCH_HTML, content_type="text/html")
        return web.Response(text=SEARCH_HTML, content_type="text/html")

    app = web.Application()
    app["source_port"] = port
    app.router.add_get("/hits.json", handler)
    app.router.add_get("/g3", handler)
    app.router.add_get("/g3/", handler)
    app.router.add_get("/g3/{tail:.*}", handler)
    app.router.add_get("/g3web", handler)
    app.router.add_get("/g3web/", handler)
    app.router.add_get("/g3web/{tail:.*}", handler)
    app.router.add_get("/g3cont", handler)
    app.router.add_get("/g3cont/", handler)
    app.router.add_get("/g3cont/{tail:.*}", handler)
    app.router.add_get("/g3sw", handler)
    app.router.add_get("/g3sw/", handler)
    app.router.add_get("/g3sw/{tail:.*}", handler)
    return app


async def serve(port: int, *, ready: asyncio.Queue | None = None) -> None:
    runner = web.AppRunner(create_app(port), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    # --port 0 时用户要的是内核挑的那个真值，而他传进来的数是 0。
    # runner.addresses 是 aiohttp 自己公布的绑定地址，别去摸 site 的私有字段。
    addresses = runner.addresses or [("", port)]
    bound = int(addresses[0][1])
    runner.app["source_port"] = bound
    print(f"PORT {bound}", flush=True)
    if ready is not None:
        ready.put_nowait(bound)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=CANONICAL_PORT,
                        help="0 = 让内核挑一个空闲端口，真值打在 stdout 的 PORT 行")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
