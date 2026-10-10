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
