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

``port`` 只出现在 :func:`source_dict` 里：页面内的 href 全是相对路径，引擎按请求 URL
拼接，所以换端口不用改语料。``--port 0`` 让内核挑端口并把真值打在 stdout
（``PORT <n>``），同时 ``GET /g3/source.json`` 回同一份书源字典——非 Python 的宿主
（Dart 那条实弹臂）照着这两个读数建源，**不必抄写规则**。

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


def create_app(port: int) -> web.Application:
    async def handler(request: web.Request) -> web.Response:
        path = request.path
        if path == "/g3/source.json":
            # 字典里的 url 必须带着**真绑定**端口：--port 0 时传进来的就是 0，
            # 宿主（Dart 那条实弹臂）照着 PORT 行连过来，字典却指向 :0 就是连不上语料。
            return web.json_response(source_dict(request.app["source_port"]))
        if path.startswith("/g3/ch/"):
            number = int(path.rsplit("/", 1)[-1].split(".")[0])
            return web.Response(text=chapter_html(number), content_type="text/html")
        if path.startswith("/g3/toc"):
            return web.Response(text=TOC_HTML, content_type="text/html")
        if path.startswith("/g3/book/"):
            return web.Response(text=BOOK_HTML, content_type="text/html")
        if path.startswith("/g3/pic"):
            return web.Response(body=PICTURE, content_type="image/jpeg")
        return web.Response(text=SEARCH_HTML, content_type="text/html")

    app = web.Application()
    app["source_port"] = port
    app.router.add_get("/g3", handler)
    app.router.add_get("/g3/", handler)
    app.router.add_get("/g3/{tail:.*}", handler)
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
