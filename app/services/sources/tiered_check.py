"""自动分级检测的进深检判据 + 待深检队列（第六十四轮）。

形状：一轮批量检测分两遍。**浅遍**只验到列表链路（`check_service.CHECK_DEPTH_LIST`），
**深遍**只对进了队列的源跑完整阅读链（`full`）。单价实测于
`.probe/tiered_two_pass_cost.py` (C) 的参照臂（36 条同形状假源、外壳并发 8）：浅遍
12.1 ms/源、2.00 次 HTTP（正文段 0.00 次），完整档 23.7 ms/源、4.92 次 HTTP（正文段
2.92 次）—— 多出来的那 2.92 次全是正文段（开书/取目录/取正文），这就是阅读链的价钱，
单价比 0.508。

⚠ **同一轮里紧接着跑第二遍买不到便宜**（同一支探针 (C) 的扫档，深遍占比 5.6%→91.7%
逐档实测，最低那一档就已经亏 16.1%，最亏 −47.8%）：两臂的**正文段请求数每一档都相等**，
而两遍臂的总请求逐档更高（最高档 230 vs 168 次）。原因是判据自身的形状——"会真跑阅读链"
的源（手里有书）浅遍结论必然是 `list_usable`，于是**全部**进队列；不进队列的那批
（搜索空手 / blocked / unusable）本来就不发正文请求。分档省的是**时间**（首轮只付浅遍
单价 ≈ 完整档的一半，贵的另一半摊到后续轮次），不是总工作量。队列/TTL/重试次数因此不是
"这一轮的预算"，而是**跨轮**的账，见 :class:`DeepQueue`。

进队列的三条判据（开发者 2026-09-30 确认；分布实测于 `.probe/tiered_two_pass_cost.py` (A)，
917 条快照 → 静态就进队列 204 条 = 22.2%，其余 713 条先跑浅遍）：

1. :data:`REASON_CROSS_HOST` —— `searchUrl` 与 `bookSourceUrl` **不是同一家站**。
   这类源"主站通不通"和"搜索通不通"问的是两台机器，浅遍里主站那一段的结论对它
   没有推力（实测 917 条里 35 条可注册域名不同）。判据按**可注册域名**比，不按裸
   `netloc`：`m.jjwxc.net → www.jjwxc.net` 这种只是子域差（实测 24 条），是同一家
   的基础设施，为它赔一整条阅读链不值。
2. :data:`REASON_UNDECIDABLE_HOST` —— 搜索地址**静态判不出主机**（`@js:` 71 条 +
   `{{}}` 模板拼主机 96 条）或主站地址本身不是 http(s)（实测 13 条）。浅遍那一段
   "主站通"根本没测到东西，所以无条件进队列，按完整档跑（合计 169 条）。
3. 浅遍结论 —— 列表链路已经证明可用（`search_only` / `explore_only` / `list_usable`）
   却没验过正文、**且手里有书可试**的（:data:`REASON_LIST_USABLE`，见
   :func:`has_prose_candidates`）；浅遍自己没给出结论的（`unknown`：timeout /
   rate_limited / network_error / 规则错）也进，但**带次数上限**，别把一条打不通的
   源每轮重试（:data:`REASON_UNKNOWN_SHALLOW`）。

**不**进队列的：`content_unavailable`（正文段已经跑过并失败，重跑买不到新信息）、
`blocked` / `unusable`（要的是换线路、代理，或确实死了）、`fully_usable`（已经到底）。

两个必须避开的坑，都是从真库快照里量出来的：

* **相对路径的 `searchUrl`（`/d/{key}` 这种）算"一致"**。真库 917 条里这样的有 386 条
  ——它们和主站天生同一台机器。按"字符串不等"判跨站会把这 386 条全拖进深检，第二遍
  等于全库重跑，省下的钱一分不剩地赔回去。
* **没写 scheme 不等于写了主机**。`search.php?key={{key}}`、`s.php,{...}` 这类**不带
  前导斜杠的相对路径**在真库里也有一批，早期按"head 里有个点就当主机"判，凭空造出
  76 条假跨站（占当时跨站桶的 68%）。现在只认"最后一段是公共后缀表里的已知顶级域"
  或 IP 字面量（:func:`_host_like`），剩下的按相对路径处理；head 里出现 `{{`/`(`/`=`
  这一类模板字符的按无从判定处理（判据②）。快照上这条修正让跨站桶从 111 条回到 35 条，
  而 9 条"带点的 same_origin"逐条看过全是 `search.html`/`s.php`/`index.php` 这种真相对路径。

红线：纯字符串/字典运算，零 IO、零 flet import，深遍的编排不在这里（在
`SourceCheckService`）。
"""
from __future__ import annotations

import ipaddress
import time
from urllib.parse import urlparse

from app.services.engine.public_suffix import effective_tld_plus_one, is_known_tld

#: 判据①：搜索主机与主站主机不是同一家站（按可注册域名比）。
REASON_CROSS_HOST = "cross_host"
#: 判据②：搜索地址或主站地址静态判不出主机（`@js:` 动态、非 http(s)）。
REASON_UNDECIDABLE_HOST = "undecidable_host"
#: 判据③a：浅遍证明列表链路可用，正文段还没验过。
REASON_LIST_USABLE = "list_usable"
#: 判据③b：浅遍自己没结论（网络/超时/规则错这一类"不能否证"的状态）。
REASON_UNKNOWN_SHALLOW = "unknown_shallow"

#: 所有会让一条源进深检队列的原因（界面上按这个顺序归组）。
DEEP_REASONS: tuple[str, ...] = (
    REASON_LIST_USABLE, REASON_UNKNOWN_SHALLOW,
    REASON_CROSS_HOST, REASON_UNDECIDABLE_HOST)

#: 浅遍结论里"列表链路已经证明可用"的那几个九态状态。
SHALLOW_USABLE_STATES = frozenset({"search_only", "explore_only", "list_usable"})
#: 浅遍结论里"没结论"的那一个——不是坏，只是这一遍没测出来。
SHALLOW_UNKNOWN_STATES = frozenset({"unknown"})
#: 只有 `reading_check_skipped` 等于这个标记的结果才是浅遍跑出来的（值即
#: `check_service.CHECK_DEPTH_LIST`，这里不 import 那个冻结模块，以免判据反过来
#: 依赖流水线）。`probe_blocked`/`no_candidates`/None 都不是浅遍，一律不排——
#: 尤其 `no_candidates`：完整档也可能给出"列表可用但没书可点"的结论，按状态判会
#: 让它每轮把自己排回深遍。
SHALLOW_DEPTH_MARK = "list"

#: 一条源最多被深遍重试几次（只对 `unknown` 类生效），试满就歇 cooldown。
DEEP_MAX_ATTEMPTS = 3
#: 队列条目的存活时间：太老的结论已经不代表现在的站点，跟着下一轮浅遍重新判一次。
DEEP_ENTRY_TTL_SECONDS = 7 * 24 * 3600.0
#: 试满配额之后隔多久重新给一次机会（比 TTL 短：一条常年超时的源不该每轮都赔深检，
#: 但也不该被永久拉黑——它的超时设置、线路甚至站点本身都会变）。
DEEP_RETRY_COOLDOWN_SECONDS = 24 * 3600.0


def _first_token(raw) -> str:
    text = str(raw or "").strip()
    return text.split()[0] if text else ""


def _normalize_host(host: str) -> str:
    return str(host or "").strip().strip(".").lower()


def home_host(source) -> str:
    """`bookSourceUrl` 的主机名；不是 http(s) 或抠不出主机时回空串。"""
    token = _first_token(getattr(source, "bookSourceUrl", ""))
    if not token.lower().startswith(("http://", "https://")):
        return ""
    return _normalize_host(urlparse(token).netloc)


#: 出现在"主机那一段"里就说明这段是模板/JS 算出来的，不是字面主机名。
_EXEC_MARKERS = frozenset('{}()=$<>;,"`')


def _host_like(head: str) -> bool:
    """这一段文本像不像一台主机。

    判据不是"数点数"也不是"最后一段是字母"——Legado 的相对搜索地址大量长这样：
    `search.php?key={{key}}`、`s.php,{...}`、`/module.php/search?...`，按点数判会把
    它们当成别家站（真库 917 条里这样误判出 **76** 条，占跨站桶的 68%）。这里只认
    两种形状：**已知顶级域结尾**（公共后缀表里查得到最后一段）或 IP 字面量。
    """
    if not head or head.startswith("."):
        return False
    try:
        ipaddress.ip_address(head.split(":")[0])
        return True
    except ValueError:
        pass
    labels = head.split(".")
    return len(labels) >= 2 and is_known_tld(labels[-1])


def search_host(source) -> tuple[str, str]:
    """`searchUrl` 的形状与主机：``(kind, host)``。

    kind 取四个值，其中 ``same_origin`` **不是**"判不出"而是"和主站同一台机器"：

    * ``empty`` —— 没有搜索地址（这类源浅遍会给结论，不按主机判）；
    * ``dynamic`` —— 主机那一段是 `@js:` 或 `{{}}` 模板算出来的，静态看不出会打到哪；
    * ``same_origin`` —— 相对路径（`/d/{key}`、`search.php?key={key}` 这种），
      天生与主站同源；
    * ``host`` —— 绝对地址或裸主机地址，`host` 是它的主机名（可能为空 = 判不出）。
    """
    token = _first_token(getattr(source, "searchUrl", ""))
    if not token:
        return "empty", ""
    if token.startswith("@"):
        return "dynamic", ""
    lowered = token.lower()
    if lowered.startswith(("http://", "https://")):
        return "host", _normalize_host(urlparse(token).netloc)
    if token.startswith("//"):
        return "host", _normalize_host(urlparse("http:" + token).netloc)
    if token.startswith(("/", "?")):
        return "same_origin", ""
    # 没写 scheme：先剥掉 Legado 的选项段（`s.php,{"charset":"gbk"}` 里逗号之后是
    # 请求选项，不是主机），再看主机那一段是模板、像主机、还是相对路径。
    head = token.split("/")[0].split("?")[0].split(",")[0]
    if _EXEC_MARKERS.intersection(head):
        return "dynamic", ""
    if _host_like(head):
        return "host", _normalize_host(head)
    return "same_origin", ""


def same_site(host_a: str, host_b: str) -> bool:
    """两台主机是不是同一家站（可注册域名相同）。

    用引擎自带的公共后缀表（`app/services/engine/public_suffix.py`）而不是
    "取最后两段"：`www.kadokado.com.tw` 的最后两段是 `com.tw`，裸切会把
    同一家站判成跨站，也会把 `xxx.com.cn` 这类一并误伤。算不出可注册域名时
    按"不同家"处理——宁可多跑一条深检，也别把跨站源当成同源漏掉。
    """
    if not host_a or not host_b:
        return False
    if host_a == host_b:
        return True
    try:
        a = effective_tld_plus_one(host_a)
        b = effective_tld_plus_one(host_b)
    except Exception:
        return False
    return bool(a) and a == b


def static_deep_reason(source) -> str:
    """判据①②：不看检测结果，只看这条源的地址形状。返回原因或空串。"""
    home = home_host(source)
    kind, search = search_host(source)
    if not home or kind == "dynamic" or (kind == "host" and not search):
        return REASON_UNDECIDABLE_HOST
    if kind in ("empty", "same_origin"):
        return ""
    return "" if same_site(home, search) else REASON_CROSS_HOST


def _positive_counter(value) -> bool:
    """这个计数键是不是"至少有一个"（None/坏值都当 0，不猜）。"""
    try:
        return int(value or 0) > 0
    except (TypeError, ValueError):
        return False


def has_prose_candidates(result: dict) -> bool:
    """浅遍有没有真交出**可以拿去试正文的书**。

    深遍跑的是阅读链，而阅读链的候选来自"搜索命中的书 + 发现页标签里的书"。两边
    都是空的时候，完整档那段只会立刻回一句 `no_candidates`——结论一字不变（九态里
    还是 search_only），却要整条链路重跑一遍搜索与发现。所以"列表可用"要进队列，
    还得**手里有书**才行。实测（`.probe/tiered_two_pass_cost.py` (D)，36 条假源里
    18 条是"搜索链路通、这个词没命中"的形状）：把这道门打成恒真，深遍占比从 50.0%
    冲到 100.0%，墙钟多花 17.0%、多付 36 次请求，而**正文段一次都没多花**（那 18 条
    立刻 `no_candidates` 返回）；更要紧的是不补它时两遍相对一遍全完整档**反而贵
    65.1%**——分档就白做了。
    """
    if not result:
        return False
    return (_positive_counter(result.get("book_count"))
            or _positive_counter(result.get("tag_valid")))


def shallow_deep_reason(result: dict) -> str:
    """判据③：浅遍跑完之后，这条结论要不要进深检队列。返回原因或空串。"""
    if not result or str(result.get("reading_check_skipped") or "") != SHALLOW_DEPTH_MARK:
        # 不是浅遍跑出来的结论（`None`=验过正文，`probe_blocked`/`no_candidates`=
        # 因为别的原因没验）一律不排，否则深遍的结果会把自己反复排进深遍。
        return ""
    state = str(result.get("check_state") or "")
    if state in SHALLOW_USABLE_STATES:
        # `no_results` 在九态里也算"搜索链路证明通了"，但手里一本书都没有：
        # 深遍那段没东西可试，跑了只是重付一遍搜索的钱（见 has_prose_candidates）。
        return REASON_LIST_USABLE if has_prose_candidates(result) else ""
    if state in SHALLOW_UNKNOWN_STATES:
        return REASON_UNKNOWN_SHALLOW
    return ""


class DeepQueue:
    """待深检队列（**服务侧**，不落库、不进用户分组）。

    只存 `raw_url → {reason, attempts, ts}`：源本身随时可能在库里被改过或删掉，
    队列只记"谁该深检、为什么、试了几次"，取的时候再按 `raw_url` 回到当前源列表里
    捞，捞不到就顺手淘汰（不留幽灵条目）。
    """

    def __init__(self, *, max_attempts: int = DEEP_MAX_ATTEMPTS,
                 ttl: float = DEEP_ENTRY_TTL_SECONDS,
                 cooldown: float = DEEP_RETRY_COOLDOWN_SECONDS,
                 clock=time.monotonic) -> None:
        self._entries: dict[str, dict] = {}
        self.max_attempts = int(max_attempts)
        self.ttl = float(ttl)
        self.cooldown = float(cooldown)
        self._clock = clock

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, raw_url, reason: str) -> None:
        key = str(raw_url or "")
        if not key or reason not in DEEP_REASONS:
            return
        entry = self._entries.get(key)
        if entry is None:
            self._entries[key] = {"reason": reason, "attempts": 0,
                                  "ts": self._clock()}
            return
        if entry["reason"] != reason:
            # 判据换了就是新信息（比如从"浅遍没结论"变成"跨站"），重试预算跟着重置；
            # 同一个原因反复排进来不许洗白次数，否则试满配额的源每轮都能复活。
            entry["reason"] = reason
            entry["attempts"] = 0
        entry["ts"] = self._clock()

    def exhausted(self, raw_url) -> bool:
        """这一条是不是已经把重试预算花完了。"""
        entry = self._entries.get(str(raw_url or ""))
        return bool(entry) and int(entry.get("attempts", 0)) >= self.max_attempts

    def note_attempt(self, raw_url) -> None:
        """深遍跑过它一回（跑完还不行就计数，满次数歇 cooldown）。"""
        entry = self._entries.get(str(raw_url or ""))
        if entry is not None:
            entry["attempts"] = int(entry.get("attempts", 0)) + 1

    def drop(self, raw_url) -> None:
        self._entries.pop(str(raw_url or ""), None)

    def reason_of(self, raw_url) -> str:
        entry = self._entries.get(str(raw_url or ""))
        return str(entry["reason"]) if entry else ""

    def prune(self) -> list[str]:
        """丢掉过期与"歇够 cooldown 的试满条目"，返回被丢掉的 `raw_url`。

        试满但还没歇够的那些**留着**：它们是"这一轮别再排深检"的凭据，删了下一轮的
        浅遍又会把它重新排进去（配额等于没有）。歇够 cooldown 才删，删掉之后下一轮
        浅遍重新判它，等于重新给一次机会。
        """
        now = self._clock()
        gone = []
        for url, entry in list(self._entries.items()):
            age = now - float(entry.get("ts", now))
            spent = int(entry.get("attempts", 0)) >= self.max_attempts
            if age > self.ttl or (spent and age > self.cooldown):
                self._entries.pop(url, None)
                gone.append(url)
        return gone

    def pending(self) -> dict[str, str]:
        """当前**该排深检**的条目：``{raw_url: reason}``（试满配额的不在这里）。"""
        self.prune()
        return {url: entry["reason"] for url, entry in self._entries.items()
                if int(entry.get("attempts", 0)) < self.max_attempts}

    def select(self, sources) -> list:
        """从本轮要检的源里挑出队列中那些（顺序跟源列表，不跟队列）。

        队列里但**不在本轮范围内**的（比如只检某分组）不会被打出来，也不会被淘汰
        ——它们还在队列里，等下一次覆盖到它们的轮次。
        """
        wanted = self.pending()
        return [source for source in sources
                if str(getattr(source, "bookSourceUrl", "") or "") in wanted]

    def snapshot(self) -> list[dict]:
        """给回放快照用的明细（按原因分组稳定排序，界面/统计直接读）。

        含已试满的那些，并如实标 `exhausted`——"队列里有 5 条"和"这轮会真排 2 条"
        是两件事，界面上不能混成一个数。
        """
        self.prune()
        rows = [{"raw_url": url, "reason": entry["reason"],
                 "attempts": int(entry.get("attempts", 0)),
                 "exhausted": int(entry.get("attempts", 0)) >= self.max_attempts}
                for url, entry in self._entries.items()]
        order = {reason: index for index, reason in enumerate(DEEP_REASONS)}
        rows.sort(key=lambda row: (order.get(row["reason"], len(order)),
                                   row["raw_url"]))
        return rows
