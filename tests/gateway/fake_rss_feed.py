"""G4 订阅面语料：三份 feed 原文 + 它们各自要证的那一格（**跟踪在仓库里**）。

与 ``fake_source_reader.py`` 同一族的理由：这份语料原来只会活在 gitignore 的探针目录里，
于是"落点臂"没有靶子——Dart 那侧要么自己搓一份 feed（搓出来的就是它想要的读数，
记忆 ``measurement-corpus-must-not-be-self-proving``），要么依赖一个不存在的探针脚本。
语料本身是契约的一部分，它得和被它钉住的样本一样进版本库。

三份各管一格，缺一格就有一段契约没有证人：

- ``FEED_RSS``（RSS 2.0，3 条）：标准 feed 的主路。第 1 条的 ``description`` 里埋了
  **HTML**（``<p>``/``<b>`` 与一段 ``<script>``）——正文那一发必须把标签去掉，而库里存的
  仍是带标签的原文（刷新要能覆盖回原文）。**现场读数**：``feedparser`` 自己的 sanitizer
  在解析时就把 ``<script>`` 整段吞了，所以库内那一格是
  ``<p>第一段 <b>加粗</b></p>\n\n<p>第二段</p>``——标签还在、脚本已经没了。于是"去脚本"
  那一条分支在 RSS 2.0 这一路上够不到，只有规则源那一路（``_parse_rule_feed``：原文连
  脚本一起进表）吃得到它，由 ``tests/test_gateway_inproc.py`` 里直喂 :func:`plain_text`
  的那一发钉住。三条都有 author 与 pubDate，所以"日期与作者那一格到底有没有落库"读得出来。
- ``FEED_ATOM``（Atom，2 条）：另一种 feed 形状，条目**没有 author、没有 summary**。
  只有第一份的话，"每行都带着非空的 author"这种假象查不出来——空 author 的行
  与解析器根本没填这一格是同一种形状，必须现场分一次。
- ``DEAD_PATH`` / ``DEAD_URL``：一条**取不到**的 feed（注入面那边直接抛，探针那边是
  没人听的端口→连接被拒，理由见 ``DEAD_URL`` 那一段）。它管的是"失败要出声"那一格：
  ``rss.refresh`` 那一发要回错误信封而不是安静的 0，而 ``rss.refresh_all`` 的收尾帧要
  带着 ``failures=1``——否则 failures 这一格可以是一个永远 0 的常量，进度条照样绿
  （记忆 ``zero-counts-need-a-positive-control``）。

**这一份不联网、也不起服务**：它只出字符串与坐标。取 feed 有两条路，两条都吃这份语料：
无头测试注入 :func:`fetcher`（不经过网络栈，离线跑），黄金样本探针把 :data:`ROUTES`
挂在回环 aiohttp 上、由真 RuleEngine 去取（那一条才证"feed 抓取走引擎网络栈"）。
"""
from __future__ import annotations

HOST = "http://127.0.0.1:8797"
BASE = f"{HOST}/rss"

#: 三条 feed 的路径（源 id 就是完整 url，所以这三格就是协议面上那三个 source_id）。
RSS_PATH = f"{BASE}/feed.xml"
ATOM_PATH = f"{BASE}/atom.xml"
DEAD_PATH = f"{BASE}/dead.xml"

#: 探针那边"取不到"的那条 feed 用**没人听的回环端口**（连接被拒），不是 500 页。
#: 理由不是省事：引擎的 ``_request_transport`` 对非 2xx 走的是「``attempt>=attempts``
#: 就把那一页正文交回去」，feedparser 读到 0 条，``rss.refresh`` 于是回 **ok=True、新增 0**
#: ——那一发教给客户端的是"这篇 feed 没有文章"，而这一族要的证人是"取不到"（两种空
#: 必须分得开，记忆 ``zero-counts-need-a-positive-control``）。连接层失败会由
#: ``RequestConnectionError`` 上抛，网关折成错误信封。端口与
#: ``.probe/r4_service_baseline.DEAD_PORT`` 是同一个（那边管书源，这边管 feed）。
DEAD_PORT = 8799
DEAD_URL = f"http://127.0.0.1:{DEAD_PORT}/rss/dead.xml"

#: 埋 HTML 的那一条在第几条（0 起）：正文那一发取它。
HTML_ITEM_URL = f"{BASE}/post/1"

#: ``description`` 里那一段脚本的指纹：出现在任何"可读正文"里就是剥标签没生效。
SCRIPT_MARK = "var evil=1"

FEED_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>G4 订阅语料</title>
  <link>{base}</link>
  <description>标准 RSS 2.0：三条都有作者与日期，第一条的正文是 HTML</description>
  <item>
    <title>带标签的第一条</title>
    <link>{html_item}</link>
    <author>作者甲</author>
    <pubDate>Mon, 05 Oct 2026 09:00:00 GMT</pubDate>
    <description><![CDATA[<p>第一段 <b>加粗</b></p>
<script>{script}</script>
<p>第二段</p>]]></description>
  </item>
  <item>
    <title>纯文本的第二条</title>
    <link>{base}/post/2</link>
    <author>作者乙</author>
    <pubDate>Tue, 06 Oct 2026 09:00:00 GMT</pubDate>
    <description>这一条根本没有标签</description>
  </item>
  <item>
    <title>没有链接的第三条</title>
    <author>作者丙</author>
    <pubDate>Wed, 07 Oct 2026 09:00:00 GMT</pubDate>
    <description>没有 link 的条目进不了表（按 (source_id,url) 去重，url 是主键的一半）</description>
  </item>
</channel></rss>
""".format(base=BASE, html_item=HTML_ITEM_URL, script=SCRIPT_MARK)

FEED_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>G4 订阅语料（Atom）</title>
  <link href="{base}"/>
  <entry>
    <title>Atom 第一条</title>
    <link href="{base}/atom/1"/>
    <updated>2026-10-04T10:00:00Z</updated>
  </entry>
  <entry>
    <title>Atom 第二条</title>
    <link href="{base}/atom/2"/>
    <updated>2026-10-05T10:00:00Z</updated>
  </entry>
</feed>
""".format(base=BASE)

#: 探针挂路由用的表：完整 url → 原文。``None`` 的那一条**取不到**：注入面据此抛，
#: 探针那边压根不挂它的路由（用的是没人听的 ``DEAD_URL``，见上面那段）。
ROUTES: dict[str, str | None] = {
    RSS_PATH: FEED_RSS,
    ATOM_PATH: FEED_ATOM,
    DEAD_PATH: None,
    DEAD_URL: None,
}

#: 解析出的**条目**数（feed 里有几条，含那条没有 link 的）。
FEED_ENTRY_COUNTS = {RSS_PATH: 3, ATOM_PATH: 2}

#: 真正落进 rss_articles 的行数（无 link 的那条进不了表——它是主键的一半）。
ARTICLE_COUNTS = {RSS_PATH: 2, ATOM_PATH: 2}

#: 去标签那把尺的答案（``plain_text`` 之后应当长这样）。
PLAIN_FIRST_LINE = "第一段"
PLAIN_HTML_TEXT = "第一段\n加粗\n第二段"


async def fetcher(url: str) -> str:
    """注入面的取页函数：**不联网**，直接回语料字符串（取不到的那条照样抛）。

    抛的形状与真网络栈一致（``RuntimeError``），这样"抓取失败上抛、由网关折成错误信封"
    那一支在无头测试里也是真走到的，不是替身自己造的。
    """
    body = ROUTES.get(str(url or ""))
    if body is None:
        raise RuntimeError(f"feed 取不到：{url}")
    return body
