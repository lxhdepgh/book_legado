"""RuleRepairView：正文规则自愈的选择器 + 报告确认写回（M3，契约 §9）。

自旧冻结视图 ``rule_repair_ui.py`` 的"提议弹窗、确认后写回"职责拆出。新视图是
事件驱动轻壳：进页先由 Controller 喂两组下拉（书源、该源已缓存目录里的章节），
用户点「开始诊断」才起一轮自愈；服务发 :class:`RepairProgressEvent`（阶段进度）与
:class:`RepairReportReadyEvent`（报告就绪）后渲染候选修复；**只有用户点确认**才经
Controller 写回书源（红线 §3.2：不自动改源）。

两条选择器形状的来由：
- 章节下拉只能装"本地已有的样本"。视图不关心它们是书架缓存目录还是上一轮检测
  抽到的三章——Controller 把它们摊平成 (key, 文案) 喂进来，粘贴框是零样本时的出口。
- key 的语义（``"组:章"``）由 Controller 解释，视图只负责存取，避免界面反过来
  依赖诊断的数据结构。
- 两条下拉外面各套一只 ``ft.Semantics``：合着的 Dropdown 在语义树里没有文字，
  读屏念不出选中了哪个源/哪一章（Dropdown 本身在 flet 0.86 没有 aria_label 字段），
  标签由 ``_sync_labels`` 跟着选中值走。

红线（§7.5）：本文件零 app.services.* import；报告以 dict 快照注入。
报告 dict 结构：report_id / source_id / source_name / field / old_value /
new_value / samples / note；``has_fix`` 为假时另有 ``reason``（此时只渲染原因，
不出现"应用"按钮）。
"""
from __future__ import annotations

import flet as ft

#: 空态文案：进页与"新诊断起手（旧报告必须收起）"都回到这一行。
_IDLE_STATUS = "选一个书源和一章正文；自愈只给候选，写回要你确认。"


class RuleRepairView(ft.Column):
    """自愈：选源 + 选章 → 诊断进度 → 报告卡（原规则 vs 新规则 + 抽样复核）。"""

    def __init__(self) -> None:
        super().__init__(spacing=10, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_confirm_fix = None      # (report: dict) -> None
        self.on_dismiss = None          # (report: dict) -> None
        self.on_start_diagnosis = None  # () -> None
        self.on_source_picked = None    # (source_url: str) -> None
        self.on_chapter_picked = None   # (chapter_key: str) -> None
        self.on_reload_sources = None   # () -> None

        self._title = ft.Text("正文规则自愈", size=24, weight=ft.FontWeight.BOLD)
        self._status = ft.Text(_IDLE_STATUS, size=12, color=ft.Colors.GREY_600)

        # -- 诊断目标：哪个书源、拿哪一章做样本 --
        # 事件面上只有 ``e.control``：flet 0.86 的 ``Event`` 就 name/data/control 三个
        # 字段，没有 ``.value``。读 ``getattr(e, "value", None)`` 会永远拿到 None，
        # 而静态测试都是直接调槽，照不出来——第六十七轮的浏览器探针抓的就是这个。
        self._source_dd = ft.Dropdown(
            label="书源", width=320, dense=True, enable_filter=True, options=[],
            on_select=lambda e: self._pick_fired(self.on_source_picked, e))
        self._chapter_dd = ft.Dropdown(
            label="正文章节", width=360, dense=True, enable_filter=True, options=[],
            on_select=lambda e: self._pick_fired(self.on_chapter_picked, e))
        # 读屏面：flet 0.86.5 的 Dropdown 压根没有 aria_label 这个字段，而合着的框在
        # 语义树里是一个**只有 role=button / aria-expanded、没有任何文字**的节点
        # （第六十七轮界面取证：aria-label 与 textContent 全空，值只画在 canvas 上，
        # 选项文字只有展开那一刻才在树里）。所以选中了哪个源/哪一章，只能由外层
        # Semantics 自己念一遍，并跟着选中值同步。
        self._source_sem = ft.Semantics(label="书源：未选择", content=self._source_dd)
        self._chapter_sem = ft.Semantics(label="正文章节：未选择",
                                         content=self._chapter_dd)
        self._reload_btn = ft.IconButton(
            ft.Icons.REFRESH, tooltip="重新载入书源与章节",
            on_click=lambda e: self._fire(self.on_reload_sources))
        self._start_btn = ft.FilledButton(
            "开始诊断", icon=ft.Icons.SEARCH,
            on_click=lambda e: self._fire(self.on_start_diagnosis))
        #: 零样本出口：本地既没读过也没检过的源，只有用户手上的一个正文页地址。
        self._manual = ft.TextField(
            label="正文页地址（本地没有可读目录时手填一章）",
            width=520, dense=True, text_size=12, visible=False)
        self._hint = ft.Text("", size=12, color=ft.Colors.GREY_600)
        self._spinner = ft.ProgressRing(width=16, height=16, visible=False)

        # 报告卡（空态占位；render_report 填充内容）
        self._card = ft.Container(visible=False)
        self._body = ft.Column(spacing=6)

        self.controls = [
            self._title,
            ft.Row([self._source_sem, self._chapter_sem, self._reload_btn,
                    self._start_btn, self._spinner], spacing=8, wrap=True),
            self._manual,
            self._hint,
            self._status,
            ft.Container(expand=True, content=self._card),
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    def _pick_fired(self, callback, event) -> None:
        """用户点了下拉里的一项：先让读屏标签跟上，再转给 Controller。

        程序改 ``value``（列表页带源进来那条路）不走这里，那条路各走
        ``set_*_options`` / ``add_source_option`` 末尾的同步。
        """
        self._sync_labels()
        self._fire(callback, event.control.value)

    def _sync_labels(self) -> None:
        """读屏标签 = "名字：当前选中项的文字"，没选中就说没选中。"""
        self._source_sem.label = self._selection_label("书源", self._source_dd)
        self._chapter_sem.label = self._selection_label("正文章节", self._chapter_dd)

    @staticmethod
    def _selection_label(base: str, dropdown) -> str:
        value = str(dropdown.value or "")
        text = next((str(option.text or "") for option in dropdown.options or []
                     if option.key == value), "")
        return f"{base}：{text or '未选择'}"

    # ------------------------------------------------------------------ #
    # 渲染接口
    # ------------------------------------------------------------------ #
    def set_status(self, text: str) -> None:
        self._status.value = text or ""

    def render_report(self, report: dict) -> None:
        """渲染一条自愈报告；空 dict/None 回到选择器空态。

        ``has_fix`` 为假时**只说原因、不给按钮**：诊断大多数时候是以"没找到可写回
        的规则"结束的（页面被拦截、原规则本地重放又成功、候选都没过复核…），
        把这种结果画成一条空规则等用户点"应用"，等于让界面说谎。
        """
        if not report:
            self._card.visible = False
            self.set_status(_IDLE_STATUS)
            return
        source_name = report.get("source_name") or report.get("source_id", "")
        samples = report.get("samples") or []
        if not report.get("has_fix", True):
            self._card.content = ft.Card(
                content=ft.Container(
                    padding=16,
                    content=ft.Column([
                        ft.Text(f"书源：{source_name}", weight=ft.FontWeight.W_500),
                        ft.Text(report.get("reason") or "没有找到可写回的规则。",
                                size=13, selectable=True),
                        ft.Text("书源未做任何改动。", size=12,
                                color=ft.Colors.GREY_600),
                    ], tight=True, spacing=8)))
            self._card.visible = True
            self.set_status("这次自愈没有给出可确认的提议（书源未改动）")
            return
        sample_text = "、".join(
            f"{s.get('chapter', '')}（{s.get('chars', 0)} 字）"
            for s in samples if isinstance(s, dict)) or "仅 1 页样本"

        rows = [
            ft.Text(f"书源：{source_name}", weight=ft.FontWeight.W_500),
            ft.Text(f"抽样复核：{sample_text}", size=12, color=ft.Colors.GREY_700),
        ]
        if report.get("note"):
            rows.append(ft.Text(report["note"], size=12, color=ft.Colors.GREY_600))
        rows += [
            ft.Text("原规则", size=12, weight=ft.FontWeight.BOLD),
            ft.Text(report.get("old_value", ""), size=12, selectable=True),
            ft.Text("新规则", size=12, weight=ft.FontWeight.BOLD),
            ft.Text(report.get("new_value", ""), size=12, selectable=True,
                     color=ft.Colors.BLUE_700),
        ]
        preview = (samples[0].get("snippet", "") if samples
                   and isinstance(samples[0], dict) else "")
        if preview:
            rows.append(ft.Text(f"正文预览：{preview}", size=12,
                                 color=ft.Colors.GREY_500))

        report_slot = report
        self._card.content = ft.Card(
            content=ft.Container(
                padding=16,
                content=ft.Column([
                    *rows,
                    ft.Row([
                        ft.TextButton("忽略", on_click=lambda e: self._fire(
                            self.on_dismiss, report_slot)),
                        ft.FilledTonalButton(
                            "应用并重试", icon=ft.Icons.BUILD_OUTLINED,
                            on_click=lambda e: self._fire(
                                self.on_confirm_fix, report_slot)),
                    ], alignment=ft.MainAxisAlignment.END),
                ], tight=True, spacing=8)))
        self._card.visible = True
        self.set_status(f"已收到《{source_name}》的自愈候选，请确认是否写回")

    # ------------------------------------------------------------------ #
    # 选择器接口（Controller 喂数据，视图不解释 key 语义）
    # ------------------------------------------------------------------ #
    def set_source_options(self, options: list[tuple[str, str]],
                           current: str = "") -> None:
        """回填书源下拉（(bookSourceUrl, 显示名) 列表）。

        ``current`` 不在表里就落回没选中：填一个 options 里没有的键会渲染成空白，
        看着像列表加载坏了（同 CodeEditorView 的口径）。
        """
        self._source_dd.options = [
            ft.dropdown.Option(key, text) for key, text in list(options or [])]
        self._pick(self._source_dd, current)
        self._sync_labels()

    def add_source_option(self, url: str, label: str) -> None:
        """把一条书源**并进**下拉并选中（列表页跳转过来时表里可能还没有它）。"""
        url = str(url or "")
        if not url:
            return
        if url not in [option.key for option in self._source_dd.options or []]:
            self._source_dd.options = list(self._source_dd.options or []) + [
                ft.dropdown.Option(url, label or url)]
        self._source_dd.value = url
        self._sync_labels()

    def set_chapter_options(self, options: list[tuple[str, str]],
                            current: str = "") -> None:
        """回填章节下拉：``[(候选 key, 文案)]``，key 由 Controller 解释。"""
        self._chapter_dd.options = [
            ft.dropdown.Option(key, text) for key, text in list(options or [])]
        self._pick(self._chapter_dd, current)
        self._sync_labels()

    @staticmethod
    def _pick(dropdown, current: str) -> None:
        keys = [option.key for option in dropdown.options or []]
        if current and current in keys:
            dropdown.value = current
        elif not current and len(keys) == 1:
            # 只有一条候选时不必再让人点一次；多条留未选中，免得默认拿第一本
            # 书的第一章去抓——那不是用户挑的。
            dropdown.value = keys[0]
        else:
            dropdown.value = None

    def set_chapter_hint(self, text: str) -> None:
        self._hint.value = text or ""

    def set_manual_visible(self, flag: bool) -> None:
        """零样本时把"手填一个正文页地址"的出口亮出来。"""
        self._manual.visible = bool(flag)

    def get_selection(self) -> dict:
        """当前选中的源、章节 key 与手填地址（只回字符串）。"""
        return {"source_url": str(self._source_dd.value or ""),
                "chapter_key": str(self._chapter_dd.value or ""),
                "manual_url": str(self._manual.value or "").strip()}

    def set_diagnosing(self, running: bool, text: str = "") -> None:
        """在途：按钮按不动 + spinner 支起来；``text`` 非空才改状态行。

        进度文案由 Controller 逐拍喂进来（阶段事件不节流），所以这里空串表示
        "只是收/起个转圈，别把刚写上的阶段字盖掉"。
        """
        self._start_btn.disabled = bool(running)
        self._spinner.visible = bool(running)
        if text:
            self.set_status(text)
