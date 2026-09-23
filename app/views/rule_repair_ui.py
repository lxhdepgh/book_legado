"""正文规则自愈的界面：诊断进度、提议弹窗、确认后写回书源。"""
from __future__ import annotations

from collections.abc import Callable, Iterable

import flet as ft

from app.services import rule_repair
from app.ui_feedback import page_is_alive, show_snack_bar


async def diagnose_and_offer(page: ft.Page, service, source, chapter, *,
                             book=None, extra_chapters: Iterable = (),
                             on_stage: Callable[[str], None] | None = None,
                             on_applied: Callable[[], None] | None = None) -> bool:
    """诊断正文规则；有提议就弹窗等确认，确认后写回并回调。

    出错和"没找到"都只是提示——书源不动，用户没有承担任何风险。
    """
    report = await rule_repair.diagnose_content_rule(
        source, chapter, book=book, extra_chapters=extra_chapters,
        on_stage=on_stage)
    if not page_is_alive(page):
        return False
    if report.fix is None:
        notice(page, f"没有找到可用的正文规则：{report.reason}")
        return False
    show_fix_dialog(page, service, report.fix, on_applied=on_applied)
    return True


def show_fix_dialog(page: ft.Page, service, fix: rule_repair.RuleFix, *,
                    on_applied: Callable[[], None] | None = None) -> None:
    """把提议摊开给用户看：改哪一条、改成什么、抽样复核结果如何。"""
    samples = "、".join(f"{item.chapter}（{item.chars} 字）"
                       for item in fix.samples) or "仅 1 页样本"

    def apply(_event=None):
        try:
            page.pop_dialog()
        except RuntimeError:
            pass
        try:
            backup = service.update_source_rule(
                fix.source_url, fix.field, fix.new_value)
        except Exception as exc:
            notice(page, f"写入书源失败：{exc}")
            return
        notice(page, f"已更新《{fix.source_name}》的正文规则"
                     + (f"，原文件已备份为 {backup.name}" if backup else ""))
        if on_applied is not None:
            on_applied()

    def cancel(_event=None):
        try:
            page.pop_dialog()
        except RuntimeError:
            pass

    preview = fix.samples[0].snippet if fix.samples else ""
    rows = [
        ft.Text(f"书源：{fix.source_name}"),
        ft.Text(f"抽样复核：{samples}", color=ft.Colors.GREY_700),
    ]
    if fix.note:
        rows.append(ft.Text(fix.note, color=ft.Colors.GREY_700, size=12))
    rows += [
        ft.Text("原规则", size=12, weight=ft.FontWeight.BOLD),
        ft.Text(fix.old_value, size=12, selectable=True),
        ft.Text("新规则", size=12, weight=ft.FontWeight.BOLD),
        ft.Text(fix.new_value, size=12, selectable=True,
                color=ft.Colors.BLUE_700),
    ]
    if preview:
        rows.append(ft.Text(f"正文预览：{preview}", size=12,
                            color=ft.Colors.GREY_700))
    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("正文规则修复"),
        content=ft.Container(width=560, content=ft.Column(
            tight=True, spacing=8, controls=rows)),
        actions=[
            ft.TextButton("取消", on_click=cancel),
            ft.Button("应用并重试", on_click=apply),
        ],
    )
    page.show_dialog(dialog)


def notice(page: ft.Page, message: str) -> None:
    show_snack_bar(page, ft.SnackBar(content=ft.Text(message, max_lines=4)))
