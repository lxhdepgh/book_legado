"""WebDavView：备份设置页（M5，§1.5 / 契约 §3 / §13）。

WebDAV 连接（地址/账号/令牌）+ 备份对象选择 + 备份/恢复按钮；逐阶段进度由
BackupProgressEvent、结果由 BackupFinishedEvent 驱动渲染。**零 app.services.*
import**：动作经 Controller 调 WebDavService。
"""
from __future__ import annotations

import flet as ft


class WebDavView(ft.Column):
    """备份设置：连接表单 + 备份对象 + 远端清单/恢复 + 结果日志。"""

    #: 可备份对象 (传给服务的 kind, 界面文案)；键与 WebDavService.BACKUP_TARGETS 对齐。
    #: 老写法把中文标签当 kind 传下去，服务只认它是事件标签，选什么都不管用——
    #: 每次都是整库备份。
    BACKUP_TARGETS = (("all", "全部"), ("sources", "书源"), ("bookshelf", "书架"),
                      ("replace", "替换规则"), ("dict", "词典"),
                      ("rss", "订阅源"), ("config", "设置"))

    def __init__(self) -> None:
        super().__init__(spacing=8, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_save_config = None     # (config: dict) -> None
        self.on_backup = None          # (kind: str) -> None
        self.on_restore = None         # (path: str) -> None
        self.on_list_remote = None     # () -> None

        self._title = ft.Text("备份与恢复", size=20, weight=ft.FontWeight.BOLD)
        self._url = ft.TextField(label="WebDAV 地址",
                                 hint_text="https://dav.example.com/backups")
        self._username = ft.TextField(label="账号")
        self._password = ft.TextField(label="令牌/密码", password=True,
                                      can_reveal_password=True)
        # 远端备份清单：不列出来就得让用户手敲一个 zip 路径（没人敲得对）。
        self._remote = ft.Dropdown(
            label="远端备份包", width=380, dense=True, options=[],
            on_select=lambda e: self._choose_remote(getattr(e, "value", None)))
        self._list_remote_btn = ft.OutlinedButton(
            "列出远端备份", icon=ft.Icons.REFRESH,
            on_click=lambda e: self._fire(self.on_list_remote))
        self._restore_path = ft.TextField(
            label="远端备份包路径",
            hint_text="点「列出远端备份」选一个，或直接填 .zip 路径")
        self._kind = ft.Dropdown(
            label="备份对象", value=self.BACKUP_TARGETS[0][0], width=160, dense=True,
            options=[ft.dropdown.Option(key, text)
                     for key, text in self.BACKUP_TARGETS])
        self._save_btn = ft.OutlinedButton(
            "保存连接", icon=ft.Icons.SAVE,
            on_click=lambda e: self._fire(self.on_save_config, self._read_config()))
        self._backup_btn = ft.FilledButton(
            "立即备份", icon=ft.Icons.BACKUP,
            on_click=lambda e: self._fire(self.on_backup, self._kind.value))
        self._restore_btn = ft.OutlinedButton(
            "从云端恢复", icon=ft.Icons.RESTORE,
            on_click=lambda e: self._fire(self.on_restore,
                                          self._restore_path.value))
        self._log = ft.Text("", size=12, color=ft.Colors.GREY_700, selectable=True)
        # 逐阶段进度：备份/恢复全程十几拍（导出 → 校验 → 压缩 → 上传），只有一句
        # "正在备份"挂着，和卡死没法区分。条与阶段文案同生死。
        self._progress = ft.ProgressBar(visible=False, bar_height=6, border_radius=3)
        self._stage = ft.Text("", size=12, color=ft.Colors.GREY_600)

        self.controls = [
            self._title,
            self._url, self._username, self._password,
            ft.Row([self._kind, self._save_btn], spacing=8),
            ft.Row([self._backup_btn], spacing=8),
            ft.Divider(height=24),
            ft.Row([self._remote, self._list_remote_btn], spacing=8),
            self._restore_path,
            ft.Row([self._restore_btn], spacing=8),
            self._progress,
            self._stage,
            self._log,
        ]

    # ------------------------------------------------------------------ #
    def _choose_remote(self, path) -> None:
        """选中某个远端备份即填进恢复路径框（手填仍然可用）。"""
        if path:
            self._restore_path.value = str(path)

    # ------------------------------------------------------------------ #
    def _read_config(self) -> dict:
        return {
            "url": self._url.value or "",
            "username": self._username.value or "",
            "password": self._password.value or "",
        }

    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    def set_config(self, config: dict) -> None:
        config = config or {}
        self._url.value = config.get("url", "")
        self._username.value = config.get("username", "")
        self._password.value = config.get("password", "")
        self._restore_path.value = config.get("restore_path", "")

    def set_result(self, ok: bool, detail: str) -> None:
        mark = "成功" if ok else "失败"
        self._log.value = f"[{mark}] {detail or ''}"

    def set_progress(self, stage: str, done: int = 0, total: int = 0) -> None:
        """逐阶段进度：``total`` 为 0 时走不定长动画，只报阶段文案。"""
        self._stage.value = str(stage or "")
        self._progress.visible = True
        self._progress.value = (max(0.0, min(1.0, done / total))
                                if total else None)

    def clear_progress(self) -> None:
        """收尾：进度条收掉，阶段文案留在原地当最后一拍的说明。"""
        self._progress.visible = False
        self._progress.value = None

    def set_remotes(self, entries: list[dict]) -> None:
        """填入远端备份清单（``[{name,path,size,modified}]``，服务侧已按时间倒序）。"""
        self._remote.options = [
            ft.dropdown.Option(
                str(item.get("path") or item.get("name") or ""),
                f"{item.get('name')}（{int(item.get('size') or 0)} 字节 · "
                f"{item.get('modified') or '时间未知'}）")
            for item in entries or []]
        self._remote.value = None
        self._remote.disabled = not self._remote.options
