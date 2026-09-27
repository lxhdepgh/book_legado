"""CheckView：书源检测结果渲染（M3，契约 §9）。

自旧冻结视图 ``check_view.py`` 的九态分类展示职责拆出（行为参照）。新视图是
**事件驱动的轻壳**：检测动作经 Controller 调 SourceCheckService，每完成一源经
:class:`~app.core.events.CheckFinishedEvent` 推一条结果进来渲染；视图自己不跑流水线。

红线（§7.5）：本文件零 app.services.* import；结果以 (source_id, state_class, summary)
数据快照注入。
"""
from __future__ import annotations

import flet as ft


#: 九态中文标签（与旧 check_view.CHECK_STATE_LABELS 对齐，行为参照）。
CHECK_STATE_LABELS = {
    "fully_usable": "完整可用",
    "list_usable": "列表可用",
    "search_only": "仅搜索可用",
    "explore_only": "仅发现可用",
    "content_unavailable": "阅读链路失败",
    "unknown": "待确认",
    "blocked": "访问被拦截",
    "unusable": "不可用",
}


def _state_color(state: str) -> str:
    if state == "fully_usable":
        return ft.Colors.GREEN
    if state == "blocked":
        return ft.Colors.GREY
    return ft.Colors.ORANGE


class CheckView(ft.Column):
    """检测：开始/停止 + 进度 + 逐源结果行（CheckFinishedEvent 驱动）。"""

    def __init__(self) -> None:
        super().__init__(spacing=10, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_start = None          # () -> None
        self.on_stop = None           # () -> None

        self._title = ft.Text("书源检测", size=24, weight=ft.FontWeight.BOLD)
        self._start_btn = ft.FilledButton(
            "开始检测", icon=ft.Icons.PLAY_ARROW,
            on_click=lambda e: self._fire(self.on_start))
        self._stop_btn = ft.OutlinedButton(
            "停止", icon=ft.Icons.STOP, disabled=True,
            on_click=lambda e: self._fire(self.on_stop))
        self._progress = ft.ProgressBar(value=0)
        self._summary = ft.Text("尚未检测", size=12, color=ft.Colors.GREY_600)
        self._rows = ft.ListView(expand=True, spacing=4)

        self.controls = [
            self._title,
            ft.Row([self._start_btn, self._stop_btn], spacing=8),
            self._progress,
            self._summary,
            ft.Divider(height=1),
            ft.Container(expand=True, content=self._rows),
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 渲染接口（Controller / CheckFinishedEvent 驱动）
    # ------------------------------------------------------------------ #
    def render_result(self, source_id: str, state_class: str, summary: str) -> None:
        """追加/刷新一条检测结果行（同一 source_id 去重覆盖）。"""
        label = CHECK_STATE_LABELS.get(state_class, state_class or "未知")
        color = _state_color(state_class or "")
        row = ft.ListTile(
            leading=ft.Icon(ft.Icons.CHECK_CIRCLE if state_class == "fully_usable"
                            else ft.Icons.BLOCK if state_class == "blocked"
                            else ft.Icons.INFO_OUTLINE, color=color),
            title=ft.Text(source_id, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Text(f"{label} · {summary or ''}", size=12,
                             color=ft.Colors.GREY_700),
        )
        # 同源重测：覆盖旧行
        for index, existing in enumerate(list(self._rows.controls)):
            if getattr(existing, "key", None) == source_id:
                self._rows.controls[index] = row
                row.key = source_id
                return
        row.key = source_id
        self._rows.controls.append(row)

    def set_running(self, running: bool, text: str = "") -> None:
        """检测运行态：开始按钮禁用/停止按钮启用。"""
        self._start_btn.disabled = running
        self._stop_btn.disabled = not running
        if text:
            self._summary.value = text

    def set_progress(self, done: int, total: int) -> None:
        self._progress.value = done / max(total, 1)
        self._summary.value = f"检测进度：{done}/{total}"

    def set_summary(self, text: str) -> None:
        self._summary.value = text or ""
