"""SourceImportView：书源导入页（本地文件 / 在线 URL / 二维码图片）。

形状对齐旧单体 ``old_code/app/views/import_view.py``：选完文件**先出预览卡**
（路径 / 总条数 / 分组分布 / 追加还是替换），点「确认导入」才落库；落库期间
「导入进度」区走进度条 + 阶段文字，跑完在同一位置换成结果汇总。
分层重构把预览和模式选择整块删掉了（选完文件直接落库），进度条也改了没人下发，
本轮补回。

灰框收敛：旧版「资源类型」下拉列 7 项，只有 1 项真能导入（其余点了只回一句
"待 M5 就绪"），和 URL 输入框并排看着像三块没内容的灰框。现在只留能用的动作，
类型不进界面（服务侧 ``kind`` 仍是参数）；两张卡（预览 / 进度结果）**只在有内容
时出现**，没选文件时不占位。

红线：本文件**不 import app.services.***；预览信息与结果全部由
:class:`~app.controllers.source.source_import_controller.SourceImportController` 注入。
"""
from __future__ import annotations

import flet as ft

#: 导入模式（与 SourceService.import_sources_json 的 mode 取值一致）。
MODE_APPEND = "append"
MODE_REPLACE = "replace"

_MODE_LABELS = {
    MODE_APPEND: ("追加", "把文件里的书源并进现有列表（按书源地址去重）"),
    MODE_REPLACE: ("替换", "用文件里的书源整体覆盖现有列表，落库前会再确认一次"),
}

#: 结果行里的类型中文名（服务侧的 ``kind`` 是英文标识，不能直接给用户看）。
_KIND_LABELS = {"book_source": "书源"}


def _card(controls: list, *, visible: bool = True) -> ft.Control:
    """带标题分隔的卡片：只有内容、只在有意义时出现（空卡就是块灰砖）。"""
    return ft.Card(
        visible=visible,
        content=ft.Container(
            padding=ft.Padding(16, 12, 16, 14),
            content=ft.Column(spacing=8, controls=controls)))


class SourceImportView(ft.Column):
    """导入页：动作行 + 预览卡（追加/替换）+ 进度与结果区。"""

    def __init__(self) -> None:
        super().__init__(spacing=14, scroll=ft.ScrollMode.AUTO)

        # -- 回调槽（Controller 填充） --
        self.on_pick_file = None          # () -> None
        self.on_pick_image = None         # () -> None（二维码/图片识别导入）
        self.on_import_url = None         # (url: str) -> None
        self.on_confirm_import = None     # (mode: str) -> None
        self.on_cancel_preview = None     # () -> None

        # 文件选择器（视图持有，Controller 经 on_pick_file 触发并读取路径）
        self.file_picker = ft.FilePicker()
        # 图片选择器（二维码识别导入；同样由 NavigationHost 注册进 page._services）
        self.image_picker = ft.FilePicker()

        self._pick_btn = ft.Button(
            "选择本地 JSON 文件", icon=ft.Icons.FILE_OPEN_OUTLINED,
            on_click=lambda e: self._fire(self.on_pick_file))
        self._pick_image_btn = ft.OutlinedButton(
            "从图片识别（二维码）", icon=ft.Icons.QR_CODE_SCANNER_OUTLINED,
            on_click=lambda e: self._fire(self.on_pick_image))

        # 在线 URL 导入：输入框 + 一个动作，不再夹一个只能取一个值的下拉
        self._url = ft.TextField(
            hint_text="书源 JSON 的地址，例如 https://…/bookSource.json",
            prefix_icon=ft.Icons.LINK, dense=True, expand=True)
        self._import_url_btn = ft.Button(
            "导入", icon=ft.Icons.CLOUD_DOWNLOAD_OUTLINED,
            on_click=lambda e: self._fire(
                self.on_import_url, (self._url.value or "").strip()))

        # -- 预览卡（选中文件后出现；确认/取消前不落库） --
        self._preview_path = ft.Text("", size=12, color=ft.Colors.GREY_600,
                                     max_lines=2,
                                     overflow=ft.TextOverflow.ELLIPSIS,
                                     selectable=True)
        self._preview_counts = ft.Text("", size=13)
        self._preview_groups = ft.Text("", size=12, color=ft.Colors.GREY_600,
                                       selectable=True)
        self._mode_group = ft.RadioGroup(
            value=MODE_APPEND,
            content=ft.Column(spacing=2, controls=[
                ft.Radio(value=mode, label=f"{name} —— {desc}")
                for mode, (name, desc) in _MODE_LABELS.items()]))
        self._confirm_btn = ft.FilledTonalButton(
            "确认导入", icon=ft.Icons.IMPORT_EXPORT,
            on_click=lambda e: self._fire(
                self.on_confirm_import, self.import_mode))
        self._cancel_btn = ft.TextButton(
            "取消", on_click=lambda e: self._fire(self.on_cancel_preview))
        self._preview_card = _card([
            ft.Row([ft.Icon(ft.Icons.VISIBILITY_OUTLINED, size=18,
                            color=ft.Colors.PRIMARY),
                    ft.Text("文件预览", size=14, weight=ft.FontWeight.W_600)],
                   spacing=6),
            self._preview_path,
            self._preview_counts,
            self._preview_groups,
            ft.Divider(height=1),
            self._mode_group,
            ft.Row([self._confirm_btn, self._cancel_btn], spacing=8),
        ], visible=False)

        # -- 进度 + 结果（同一位置，避免"结果跑到屏幕外"） --
        self._progress = ft.ProgressBar(visible=False, bar_height=6,
                                       border_radius=3)
        self._stage = ft.Text("", size=12, color=ft.Colors.GREY_600)
        self._summary = ft.Text("", size=13, weight=ft.FontWeight.W_500)
        self._details = ft.Column(spacing=2)
        self._result_card = _card([
            ft.Row([ft.Icon(ft.Icons.TASK_ALT, size=18, color=ft.Colors.PRIMARY),
                    ft.Text("导入进度", size=14, weight=ft.FontWeight.W_600)],
                   spacing=6),
            self._progress,
            self._stage,
            ft.Divider(height=1),
            self._summary,
            self._details,
        ], visible=False)

        self.controls = [
            ft.Text("导入资源", size=26, weight=ft.FontWeight.BOLD),
            ft.Text("从 Legado 格式的书源 JSON 文件、在线地址或二维码图片导入。",
                    size=13, color=ft.Colors.GREY_600),
            ft.Divider(height=2),
            ft.Row([self._pick_btn, self._pick_image_btn], spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            self._preview_card,
            ft.Row([self._url, self._import_url_btn], spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            self._result_card,
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 渲染接口（Controller 调用）
    # ------------------------------------------------------------------ #
    @property
    def import_mode(self) -> str:
        """预览卡上选中的导入模式。"""
        return self._mode_group.value or MODE_APPEND

    def render_preview(self, path: str, info: dict) -> None:
        """渲染文件预览；``info`` 为 ``preview_book_source_text`` 的快照。"""
        valid = int(info.get("valid", 0) or 0)
        broken = int(info.get("broken", 0) or 0)
        error = info.get("error") or ""
        self._preview_path.value = path or "（未提供文件名，内容已读入）"
        if error:
            # 读不出东西就没什么可确认的：预览卡不出现，错误落在进度卡位置。
            self._preview_card.visible = False
            self.show_stage(f"这个文件不能导入：{error}", finished=True)
            return
        groups = list(info.get("groups") or [])
        shown = "、".join(f"{name} {count} 条" for name, count in groups[:8])
        if len(groups) > 8:
            shown += f"… 共 {len(groups)} 个分组"
        self._preview_counts.value = (
            f"可导入 {valid} 条"
            + (f"，跳过 {broken} 条缺书源地址" if broken else ""))
        self._preview_groups.value = shown or "（无分组信息）"
        self._preview_card.visible = True
        self._set_actions_enabled(True)

    def clear_preview(self) -> None:
        self._preview_card.visible = False
        self._preview_path.value = ""
        self._preview_counts.value = ""
        self._preview_groups.value = ""
        self._mode_group.value = MODE_APPEND

    def show_stage(self, text: str, *, finished: bool = False) -> None:
        """只显示一行状态（发起动作前的校验反馈也走这张卡，不留隐形灰块）。"""
        self._result_card.visible = True
        self._progress.visible = False
        self._stage.value = text or ""
        self._summary.value = ""
        self._details.controls.clear()
        if finished:
            self.set_busy(False)

    def set_importing(self, importing: bool, stage: str = "") -> None:
        """开关进度区：True 显示进度条 + 阶段文字并锁住动作按钮。

        False 且卡里再没别的内容时整张卡收起——留一张空卡就是一块没人要的灰砖。
        """
        self.set_busy(bool(importing))
        self._progress.visible = bool(importing)
        self._progress.value = None            # 先回不定长，拿到条数再变确定
        if importing:
            self._result_card.visible = True
            # 确认过后决策卡收起：在途时留着「确认导入/取消」只会让人再点一次。
            self._preview_card.visible = False
            self._stage.value = stage or "正在导入…"
            self._summary.value = ""
            self._details.controls.clear()
            return
        self._stage.value = ""
        if not (self._summary.value or self._details.controls):
            self._result_card.visible = False

    def set_progress(self, done: int, total: int) -> None:
        """逐条落库的条数进度；``total`` 为 0 时保持不定长。"""
        self._result_card.visible = True
        if total > 0:
            self._progress.visible = True
            self._progress.value = max(0.0, min(1.0, done / total))
            self._stage.value = f"校验并落库 {done}/{total}"
        else:
            self._progress.visible = True
            self._progress.value = None

    def render_result(self, kind: str, count: int, errors: list) -> None:
        """渲染导入结果（ImportFinishedEvent：kind/count/errors 汇总）。"""
        errors = list(errors or [])
        self._result_card.visible = True
        self._progress.visible = False
        self._stage.value = ""
        self._summary.value = (f"{_KIND_LABELS.get(kind, kind)}导入完成："
                               f"成功 {count} 条")
        self._details.controls.clear()
        for line in errors:
            self._details.controls.append(
                ft.Text(f"· {line}", size=12, color=ft.Colors.ORANGE_800,
                        selectable=True))
        self.set_busy(False)
        self.clear_preview()

    def set_busy(self, busy: bool) -> None:
        """导入在途时锁住三个发起入口（重复点击会把两份文件叠着往库里灌）。"""
        for control in (self._pick_btn, self._pick_image_btn,
                        self._url, self._import_url_btn):
            control.disabled = bool(busy)
        self._set_actions_enabled(not busy)

    def _set_actions_enabled(self, enabled: bool) -> None:
        for control in (self._confirm_btn, self._cancel_btn):
            control.disabled = not enabled
