"""RssView：订阅源页（阅读组第三个页签）。

服务 ``RssService`` 早就齐了（源的增删 / 刷新 / 文章已读收藏），但**没有任何界面
调用过它**，``RssRefreshedEvent`` 也零订阅——用户只能看着库里的空表。本页补上
"订阅源 → 文章 → 正文"这条链，按窗口档位给两种形状：

- **宽窗**：三栏并排（flex 3:4:5），选源与选文章只换右边两栏，一眼能看到全链。
- **窄窗**（外壳 :data:`app.views.navigation.NARROW_WIDTH_THRESHOLD` 以下）：**一次只
  摊开一栏**，顶部一条「订阅源 › 文章 › 正文」级切换跟着走——选中源自动跳到文章、
  选中文章自动跳到正文；到不了的级（还没选源时的"文章"）置灰。
  第七十七轮在 344 那档量过并排三栏的代价：每栏只剩 ~90–120px，第二栏被切掉半截、
  第三栏整个在窗外，收进 3:4:5 也只是"看得见但读不了"。窄窗里三栏并排本来就是错的
  形状，不是宽度没给够。

档位由外壳推（:meth:`on_band_change`）：视图自己读 ``page.width`` 抓不到"跨档"这一拍。

带规则字段的订阅源（``rule_articles`` 非空）在这里只能列与刷新：逐字段规则求值
按 M4 记录仍是"最小可用兜底"，所以界面不提供编辑规则的入口，不承诺改了会生效。

红线（§7.5）：本文件不 import 任何 ``app.services.*``；数据以 dict 注入。
"""
from __future__ import annotations

import time

import flet as ft


def _when(value) -> str:
    """时间戳 → 界面可读的日期（0 / 空 = 从没刷过）。"""
    try:
        stamp = float(value or 0)
    except (TypeError, ValueError):
        return str(value or "")
    if stamp <= 0:
        return ""
    if stamp > 1_000_000_000:      # epoch 秒
        return time.strftime("%m-%d %H:%M", time.localtime(stamp))
    return str(value)


class RssView(ft.Column):
    """订阅页：宽窗三栏并排，窄窗单栏 + 级切换（源 → 文章 → 正文）。"""

    #: 窄窗那三级的顺序与标题（也是级切换条上的字）。
    LEVELS = ("sources", "articles", "detail")
    _LEVEL_TITLES = {"sources": "订阅源", "articles": "文章", "detail": "正文"}

    def __init__(self) -> None:
        super().__init__(expand=True, spacing=0)

        #: 外壳推来的档位（默认宽窗：装配时外壳会立刻补推一次真实档位）
        self._narrow = False
        #: 窄窗当前摊开的那一级
        self._level = "sources"
        #: 级可达性：文章要有选中的源，正文要有打开的文章
        self._has_source = False
        self._has_article = False

        # -- 回调槽（Controller 填充） --
        self.on_request_add = None      # () -> None（弹添加框）
        self.on_add_source = None       # (name: str, url: str) -> None
        self.on_refresh_all = None      # () -> None
        self.on_select_source = None    # (source_id: str) -> None
        self.on_refresh_source = None   # (source_id: str) -> None
        self.on_remove_source = None    # (source: dict) -> None
        self.on_filter_unread = None    # (only_unread: bool) -> None
        self.on_select_article = None   # (article: dict) -> None
        self.on_toggle_star = None      # (article: dict) -> None

        # ------------------------------------------------------------------ #
        # 顶栏：标题 + 添加 / 全部刷新 + 进度
        # ------------------------------------------------------------------ #
        self._refresh_all_btn = ft.IconButton(
            ft.Icons.REFRESH, tooltip="刷新全部订阅源",
            on_click=lambda e: self._fire(self.on_refresh_all))
        self._add_btn = ft.IconButton(
            ft.Icons.ADD, tooltip="添加订阅源",
            on_click=lambda e: self._fire(self.on_request_add))
        self._progress = ft.ProgressBar(value=0, visible=False, bar_height=6)

        # ------------------------------------------------------------------ #
        # 左栏：订阅源
        # ------------------------------------------------------------------ #
        self._source_count = ft.Text("", size=12, color=ft.Colors.GREY_600)
        self._sources = ft.ListView(expand=True, spacing=2)
        self._source_pane = ft.Column(spacing=4, expand=True, controls=[
            ft.Row([ft.Text("订阅源", size=14, weight=ft.FontWeight.W_500),
                    self._source_count],
                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            self._sources,
        ])

        # ------------------------------------------------------------------ #
        # 中栏：文章
        # ------------------------------------------------------------------ #
        self._article_title = ft.Text("文章", size=14, weight=ft.FontWeight.W_500)
        self._unfiltered = ft.Checkbox(
            label="只看未读", value=False,
            on_change=lambda e: self._fire(self.on_filter_unread, e.control.value))
        self._articles = ft.ListView(expand=True, spacing=2)
        self._article_pane = ft.Column(spacing=4, expand=True, controls=[
            ft.Row([self._article_title, self._unfiltered], spacing=8),
            self._articles,
        ])

        # ------------------------------------------------------------------ #
        # 右栏：正文
        # ------------------------------------------------------------------ #
        self._detail_title = ft.Text("选一个订阅源", size=16,
                                     weight=ft.FontWeight.W_500)
        self._detail_meta = ft.Text("", size=12, color=ft.Colors.GREY_600)
        self._detail_body = ft.Text("", selectable=True, size=14)
        self._detail_link = ft.Text("", selectable=True, size=11,
                                    color=ft.Colors.GREY_500,
                                    max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
        self._detail_star = ft.IconButton(ft.Icons.STAR_BORDER, tooltip="收藏")
        self._detail = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO,
                                 spacing=8, controls=[
                                     ft.Row([self._detail_title, self._detail_star],
                                            spacing=4),
                                     self._detail_meta,
                                     self._detail_body,
                                     self._detail_link,
                                 ])

        self._status = ft.Text("", size=12, color=ft.Colors.GREY_600)

        # ------------------------------------------------------------------ #
        # 两副形状共用同一批栏控件：宽窗把三栏并排，窄窗把当前不管的那两栏 `visible`
        # 关掉。**不换父节点、不重排**：换父要把整棵子树重新下发，而这一页的两个
        # ListView 在刷新期间正逐条落文章，中途被摘走就是白丢一截渲染。
        # ------------------------------------------------------------------ #
        self._panes = [
            ft.Container(expand=3, padding=8, content=self._source_pane),
            ft.Container(expand=4, padding=8, content=self._article_pane),
            ft.Container(expand=5, padding=12, content=self._detail),
        ]
        self._dividers = [ft.VerticalDivider(width=1), ft.VerticalDivider(width=1)]

        #: 级切换条：只在窄窗出现（宽窗三栏都在眼前，没有"跳一级"这件事）
        self._switch = ft.Row(spacing=0, visible=False,
                              vertical_alignment=ft.CrossAxisAlignment.CENTER)

        self.controls = [
            ft.Container(
                padding=ft.Padding(12, 10, 12, 4),
                content=ft.Row([
                    ft.Text("订阅", size=22, weight=ft.FontWeight.BOLD),
                    ft.Row([self._add_btn, self._refresh_all_btn], spacing=0),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)),
            self._progress,
            ft.Container(padding=ft.Padding(12, 0, 12, 4), content=self._status),
            ft.Divider(height=1),
            ft.Container(padding=ft.Padding(12, 0, 12, 6), content=self._switch),
            # 三栏按 **flex 比例** 分，不按定宽：定宽的那一版（260+340+剩余）在不换行
            # 的 Row 里合计 600+，344 那一档实测第二栏被右边切掉半截、第三栏整个在窗外
            # （第七十七轮像素，`out_portrait75_reading_rss_360.png`）。
            # 3:4:5 只解决了"被裁"，解决不了"读不了"（每栏 ~90–120px），所以窄窗走
            # 上面那条 `visible`：一次只摊开一栏，Row 里剩下那一栏自己占满整宽。
            # 不改成堆叠：三栏都是 `expand` 的 ListView，横着排才有有界高，一堆就塌。
            ft.Row(expand=True, spacing=0, controls=[
                self._panes[0], self._dividers[0],
                self._panes[1], self._dividers[1],
                self._panes[2],
            ]),
        ]
        self._render_switch()
        self._apply_shape()

    # ------------------------------------------------------------------ #
    # 档位与形状
    # ------------------------------------------------------------------ #
    def on_band_change(self, narrow: bool) -> None:
        """外壳（:class:`app.views.navigation.NavigationHost`）推来的档位变更。

        只实现这一个钩子就够：视图不该自己去猜 ``page.width`` 什么时候算数。
        """
        self._narrow = bool(narrow)
        self._apply_shape()

    def _apply_shape(self) -> None:
        """按档位摆：宽窗三栏齐亮、切换条收起；窄窗只亮当前那一级。"""
        self._switch.visible = self._narrow
        level = self._level_index()
        for index, pane in enumerate(self._panes):
            pane.visible = (not self._narrow) or index == level
        # 分隔条只有两条（夹在三栏中间），跟三栏一起 zip 会把第三条漏掉——
        # 窄窗里正文栏永远关不掉，摊开的还是"文章+正文"两栏。
        for divider in self._dividers:
            divider.visible = not self._narrow
        self._render_switch()

    def _level_index(self) -> int:
        return self.LEVELS.index(self._level)

    def _reachable(self, level: str) -> bool:
        if level == "sources":
            return True
        if level == "articles":
            return self._has_source
        return self._has_article

    def _render_switch(self) -> None:
        """级切换条：「订阅源 › 文章 › 正文」，当前那级实心、够不着的置灰。"""
        self._switch.controls.clear()
        for index, level in enumerate(self.LEVELS):
            if index:
                self._switch.controls.append(
                    ft.Text("›", size=13, color=ft.Colors.GREY_500))
            title = self._LEVEL_TITLES[level]
            if level == self._level:
                control = ft.FilledTonalButton(
                    title, disabled=not self._reachable(level), tooltip=f"当前：{title}")
            else:
                control = ft.TextButton(
                    title, disabled=not self._reachable(level),
                    tooltip=f"跳到{title}",
                    on_click=lambda e, lv=level: self._go(lv))
            self._switch.controls.append(control)

    def _go(self, level: str) -> None:
        if not self._reachable(level):
            return
        self._level = level
        self._apply_shape()

    def _clamp_level(self) -> None:
        """当前那一级够不着了就往前退（源被删光时人还停在"文章"，那一栏已经空了）。"""
        for index in range(self._level_index(), -1, -1):
            if self._reachable(self.LEVELS[index]):
                if self.LEVELS[index] != self._level:
                    self._level = self.LEVELS[index]
                    self._apply_shape()
                return

    def _pick_source(self, source_id: str) -> None:
        """点一个源：交给控制器，窄窗顺手跳到文章（不跳就只能再点一次切换条）。"""
        self._fire(self.on_select_source, source_id)
        if self._narrow:
            # 文章这一级的可达凭据本该由 ``render_sources`` 落，但控制器可能把渲染
            # 排到下一拍（异步刷新）；这一拍用户确实已经选中了源，不写就会原地不动。
            self._has_source = True
            self._go("articles")

    def _pick_article(self, article: dict) -> None:
        self._fire(self.on_select_article, article)
        if self._narrow:
            self._has_article = True
            self._go("detail")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 渲染接口
    # ------------------------------------------------------------------ #
    def set_status(self, text: str) -> None:
        self._status.value = text or ""

    def set_progress(self, done: int, total: int) -> None:
        """刷新进度：跑完（或没有源可刷）就把条收起来，别留一根满格假进度。"""
        total = int(total or 0)
        done = int(done or 0)
        running = total > 0 and done < total
        self._progress.visible = running
        self._progress.value = (done / total) if running else 0

    def render_sources(self, sources: list[dict], counts: dict[str, dict],
                       selected_id: str = "") -> None:
        self._sources.controls.clear()
        self._has_source = bool(str(selected_id or ""))
        self._source_count.value = f"{len(sources or [])} 个"
        if not sources:
            self._sources.controls.append(ft.Text(
                "还没有订阅源：点上方「+」填名称与 feed 地址。",
                size=12, color=ft.Colors.GREY_600))
            self._render_switch()
            self._clamp_level()
            return
        for source in sources:
            sid = str(source.get("id") or "")
            stat = counts.get(sid) or {}
            unread = int(stat.get("unread") or 0)
            # 「N 未读」写在副标题里，不塞进 trailing：trailing 一旦是 Row（计数 + 按钮），
            # 客户端把这一坨按不收缩的宽度排在最右，标题/副标题被挤成 0 宽——第七十八轮
            # 像素（`.probe/out_rss80_wide1.png`）里四行源只剩图标和「⋮」，名字整行不见。
            # 文章那一栏的 trailing 是单个 IconButton，同一档宽度下标题正常，所以照它学。
            meta = [f"{int(stat.get('total') or 0)} 篇"]
            if unread:
                meta.append(f"{unread} 未读")
            meta.append(f"刷新于 {_when(source.get('last_update_time')) or '从未'}")
            self._sources.controls.append(ft.ListTile(
                selected=sid == str(selected_id or ""),
                leading=ft.Icon(ft.Icons.RSS_FEED),
                title=ft.Text(str(source.get("name") or "(未命名)")
                              + ("" if source.get("enabled", True) else "（已停用）"),
                              max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                subtitle=ft.Text(" · ".join(meta),
                                 size=11, color=ft.Colors.GREY_500,
                                 max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                on_click=lambda e, i=sid: self._pick_source(i),
                trailing=ft.IconButton(ft.Icons.MORE_VERT, tooltip="本源操作",
                                       on_click=lambda e, s=source: self._show_dialog(
                                           self.build_source_menu(s))),
            ))
        # 可达性可能刚变（第一次选上源），窄窗那条切换条要跟着亮/灰
        self._render_switch()
        self._clamp_level()

    def render_articles(self, articles: list[dict], selected_id=None) -> None:
        self._articles.controls.clear()
        if not articles:
            # 不写"左边的源"：窄窗里上一级在切换条上，不在左边
            self._articles.controls.append(ft.Text(
                "这里还没有文章：先选一个订阅源，点「⋮」→「刷新本源」抓一次。",
                size=12, color=ft.Colors.GREY_600))
            return
        for article in articles:
            starred = bool(article.get("starred"))
            self._articles.controls.append(ft.ListTile(
                selected=str(article.get("id")) == str(selected_id),
                title=ft.Text(
                    str(article.get("title") or "(无标题)"),
                    size=14, weight=None if article.get("read")
                    else ft.FontWeight.W_500,
                    max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                subtitle=ft.Text(
                    " · ".join([p for p in (_when(article.get("pub_date")),
                                            str(article.get("author") or ""))
                                if p]),
                    size=11, color=ft.Colors.GREY_500,
                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                on_click=lambda e, a=article: self._pick_article(a),
                trailing=ft.IconButton(
                    ft.Icons.STAR if starred else ft.Icons.STAR_BORDER,
                    tooltip="取消收藏" if starred else "收藏",
                    on_click=lambda e, a=article: self._fire(
                        self.on_toggle_star, a)),
            ))

    def show_article(self, article: dict, body: str = "") -> None:
        """正文一栏。``body`` 由控制器去掉标签后送来（视图不认 HTML）。"""
        article = article or {}
        starred = bool(article.get("starred"))
        self._detail_title.value = str(article.get("title") or "(无标题)")
        self._detail_meta.value = " · ".join([
            p for p in (str(article.get("author") or ""),
                        _when(article.get("pub_date"))) if p])
        self._detail_body.value = body or "(这条没有正文，只有标题和链接)"
        self._detail_link.value = str(article.get("url") or "")
        self._detail_star.icon = ft.Icons.STAR if starred else ft.Icons.STAR_BORDER
        self._detail_star.on_click = (
            lambda e, a=article: self._fire(self.on_toggle_star, a))
        #: 「正文」这一级的可达凭据：没有打开的文章就不许停在正文级。
        self._has_article = True
        self._render_switch()

    def clear_detail(self, text: str = "选一个订阅源") -> None:
        self._detail_title.value = text
        self._detail_meta.value = ""
        self._detail_body.value = ""
        self._detail_link.value = ""
        self._detail_star.on_click = None
        self._has_article = False
        self._render_switch()
        self._clamp_level()

    # ------------------------------------------------------------------ #
    # 对话框（视图搭，控制器挂到 page 上）
    # ------------------------------------------------------------------ #
    def build_source_menu(self, source: dict) -> ft.AlertDialog:
        sid = str(source.get("id") or "")
        return ft.AlertDialog(
            modal=True,
            title=ft.Text(str(source.get("name") or sid)[:40]),
            content=ft.Column(tight=True, spacing=0, controls=[
                ft.ListTile(
                    leading=ft.Icon(ft.Icons.REFRESH), title=ft.Text("刷新本源"),
                    on_click=lambda e: self._close_and(
                        self.on_refresh_source, sid)),
                ft.ListTile(
                    leading=ft.Icon(ft.Icons.DELETE_OUTLINE),
                    title=ft.Text("删除本源", color=ft.Colors.RED_400),
                    on_click=lambda e, s=source: self._close_and(
                        self.on_remove_source, s)),
            ]),
            actions=[ft.TextButton("关闭", on_click=lambda e: self._close_dialog())],
        )

    def build_confirm_remove(self, source: dict, on_confirm) -> ft.AlertDialog:
        return ft.AlertDialog(
            modal=True,
            title=ft.Text("删除订阅源"),
            content=ft.Text(
                f"确定删除「{source.get('name') or source.get('id')}」吗？"
                "它的文章（含已读与收藏标记）会一起删掉。"),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self._close_dialog()),
                ft.TextButton("删除", on_click=lambda e, s=source: self._close_and(
                    on_confirm, s)),
            ],
        )

    def build_add_dialog(self) -> ft.AlertDialog:
        name = ft.TextField(label="名称", hint_text="留空则用地址当名称")
        url = ft.TextField(label="订阅地址（feed URL）",
                           hint_text="https://example.com/feed")

        def submit(_=None) -> None:
            self._close_dialog()
            self._fire(self.on_add_source, name.value or "", url.value or "")

        return ft.AlertDialog(
            modal=True,
            title=ft.Text("添加订阅源"),
            content=ft.Column(tight=True, spacing=8, controls=[
                name, url,
                ft.Text("只抓取标准 RSS/Atom。带规则的订阅源要在源文件里填规则字段，"
                        "本页暂不提供规则编辑。",
                        size=11, color=ft.Colors.ON_SURFACE_VARIANT),
            ]),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self._close_dialog()),
                ft.TextButton("添加", on_click=lambda e: submit()),
            ],
        )

    # ------------------------------------------------------------------ #
    # 对话框挂载细节（page 由 Flet 注入；离线未挂载时静默）
    # ------------------------------------------------------------------ #
    def _page(self):
        try:
            return self.page
        except Exception:
            return None

    def _show_dialog(self, dialog: ft.AlertDialog) -> None:
        page = self._page()
        if page is not None:
            page.show_dialog(dialog)

    def _close_dialog(self) -> None:
        page = self._page()
        if page is not None:
            page.pop_dialog()

    def _close_and(self, callback, arg) -> None:
        self._close_dialog()
        self._fire(callback, arg)
