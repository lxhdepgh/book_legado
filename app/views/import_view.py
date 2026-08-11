"""
书源导入页面 - 支持从 JSON 文件导入书源（追加或替换）
"""
from __future__ import annotations

import json

import flet as ft

from app.services.source_service import SourceService


class ImportView(ft.Column):
    """书源导入页面"""

    def __init__(self, service: SourceService, on_imported=None):
        super().__init__(spacing=20, scroll=ft.ScrollMode.AUTO)
        self.service = service
        self.on_imported = on_imported

        # 预创建 Ref
        self._path_ref = ft.Ref[ft.TextSpan]()
        self._total_ref = ft.Ref[ft.TextSpan]()
        self._groups_ref = ft.Ref[ft.TextSpan]()
        self._mode_ref = ft.Ref[ft.RadioGroup]()
        self._progress_ref = ft.Ref[ft.ProgressBar]()
        self._result_ref = ft.Ref[ft.Text]()
        self._importing = False

        # 文件选择器
        self.file_picker = ft.FilePicker()

        self._pick_btn = ft.Button(
            "选择书源 JSON 文件",
            icon=ft.Icons.FILE_OPEN,
            on_click=self._on_pick_click,
        )

        # 预览区域
        self._preview_card = ft.Card(
            visible=False,
            content=ft.Container(
                padding=20,
                content=ft.Column([
                    ft.Text("文件预览", size=18, weight=ft.FontWeight.BOLD),
                    ft.Divider(),
                    ft.Text("路径: ", spans=[ft.TextSpan("", ref=self._path_ref)]),
                    ft.Text("总数: ", spans=[ft.TextSpan("", ref=self._total_ref)]),
                    ft.Text("分组: ", spans=[ft.TextSpan("", ref=self._groups_ref)]),
                    ft.Divider(),
                    ft.Text("导入模式:"),
                    ft.RadioGroup(
                        ref=self._mode_ref,
                        content=ft.Column([
                            ft.Radio(value="append", label="追加 - 将书源追加到现有列表中（自动去重）"),
                            ft.Radio(value="replace", label="替换 - 用导入的书源完全替换现有列表"),
                        ]),
                    ),
                    ft.Divider(),
                    ft.FilledTonalButton(
                        "确认导入",
                        icon=ft.Icons.IMPORT_EXPORT,
                        on_click=self._do_import,
                    ),
                ]),
            ),
        )

        # 进度/结果区域
        self._result_card = ft.Card(
            visible=False,
            content=ft.Container(
                padding=20,
                content=ft.Column([
                    ft.Text("导入结果", size=18, weight=ft.FontWeight.BOLD),
                    ft.Divider(),
                    ft.ProgressBar(ref=self._progress_ref, visible=False),
                    ft.Text("", ref=self._result_ref),
                ]),
            ),
        )

        # 布局
        self.controls = [
            ft.Container(
                content=ft.Column([
                    ft.Text("导入书源", size=28, weight=ft.FontWeight.BOLD),
                    ft.Text("从 Legado 格式的 JSON 文件中导入书源。支持追加（自动去重）和替换两种模式。",
                            color=ft.Colors.GREY_600),
                    ft.Divider(height=2),
                    self._pick_btn,
                ]),
                padding=ft.Padding(0, 0, 0, 10),
            ),
            self._preview_card,
            self._result_card,
        ]

    def _on_pick_click(self, e):
        """打开文件选择对话框（Flet 0.86.x 中 pick_files 为 async 方法）"""
        self.page.run_task(self._pick_file)

    async def _pick_file(self):
        """Flet 0.86+ 直接从 pick_files() 获取返回值。"""
        try:
            files = await self.file_picker.pick_files(
                allow_multiple=False,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["json"],
            )
        except Exception as err:
            self._show_error(f"打开文件选择器失败: {err}")
            return
        if not files:
            return
        self._process_file(files[0])

    def _process_file(self, selected_file):
        file_path = selected_file.path
        if not file_path:
            self._show_error("无法取得所选文件的本地路径")
            return
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, list):
                self._show_error("所选文件不是有效的书源 JSON（需要顶层为数组）")
                return

            total = len(data)
            groups = {}
            for item in data:
                if not isinstance(item, dict):
                    self._show_error("书源 JSON 数组中包含非对象数据")
                    return
                g = item.get("bookSourceGroup", "未分组") or "未分组"
                groups[g] = groups.get(g, 0) + 1

            group_summary = ", ".join(f"{k}({v})" for k, v in
                                       sorted(groups.items(),
                                              key=lambda x: -x[1])[:10])
            if len(groups) > 10:
                group_summary += f"... 共{len(groups)}个分组"

            self._path_ref.current.text = file_path
            self._total_ref.current.text = str(total)
            self._groups_ref.current.text = group_summary
            self._mode_ref.current.value = "append"
            self._preview_card.visible = True
            self._result_card.visible = False
            self.update()

        except json.JSONDecodeError as err:
            self._show_error(f"JSON 解析失败: {err}")
        except Exception as err:
            self._show_error(f"读取文件失败: {err}")

    def _do_import(self, e):
        mode = self._mode_ref.current.value or "append"
        if mode == "replace":
            existing = len(self.service.sources)
            incoming = self._total_ref.current.text or "0"
            dialog = ft.AlertDialog(
                modal=True,
                title=ft.Text("替换全部现有书源？"),
                content=ft.Text(
                    f"现有 {existing} 条书源将被所选文件中的 {incoming} 条书源完全替换。此操作不可撤销。"),
                actions=[
                    ft.TextButton("取消", on_click=lambda event: self.page.pop_dialog()),
                    ft.Button("确认替换", color=ft.Colors.RED,
                              on_click=self._confirm_replace),
                ])
            self.page.show_dialog(dialog)
            return
        self._perform_import(mode)

    def _confirm_replace(self, _):
        self.page.pop_dialog()
        self._perform_import("replace")

    def _perform_import(self, mode):
        if self._importing:
            return
        self._importing = True
        file_path = self._path_ref.current.text
        previous_sources = list(self.service.sources)

        self._result_card.visible = True
        self._progress_ref.current.visible = True
        self._result_ref.current.value = "正在导入..."
        self.update()

        try:
            result = self.service.import_sources(file_path, mode)
            self._progress_ref.current.visible = False

            if mode == "replace":
                msg = (f"导入完成！\n"
                       f"共导入 {result['imported']} 条书源（替换模式）")
            else:
                msg = (f"导入完成！\n"
                       f"总 {result['total']} 条 | "
                       f"新增 {result['imported']} 条 | "
                       f"重复 {result['duplicates']} 条 | "
                       f"错误 {result['errors']} 条")

            self._result_ref.current.value = msg

            try:
                self.service.save()
                self._result_ref.current.value += "\n已保存到文件。"
            except Exception as save_err:
                self.service.sources = previous_sources
                self._result_ref.current.value = (
                    f"导入未完成：保存失败，已恢复原有书源。\n{save_err}")
                self.update()
                return

            self.update()

            if self.on_imported:
                self.on_imported(result)

        except Exception as err:
            self._progress_ref.current.visible = False
            self._result_ref.current.value = f"导入失败: {err}"
            self.update()
        finally:
            self._importing = False

    def _show_error(self, msg: str):
        self._preview_card.visible = False
        self._result_card.visible = True
        self._progress_ref.current.visible = False
        self._result_ref.current.value = msg
        self.update()
