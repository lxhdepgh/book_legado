"""WebDavController：备份设置页编排（M5，§1.5 / 契约 §3）。

连接配置存 SettingsService；备份/恢复经 WebDavService 派发。

**结果行只有一支笔**（这一条以前不是）：本页发起的动作由 ``ui_call`` 的 future 收尾
写结果（``_after``，措辞带中文对象名），:class:`BackupFinishedEvent` 只用来收进度条；
只有本页从没发起过动作时（别处触发的备份）事件才补写结果。两条路都写同一行时，
谁的字最终上屏取决于事件与回调谁先跑到，同一件事两种说法。
:class:`BackupProgressEvent` 负责逐阶段进度（导出 → 校验 → 压缩 → 上传 → 落库）。
"""
from __future__ import annotations

import logging

import flet as ft

from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.tools.web_dav_service import WebDavService
except Exception:  # pragma: no cover - 并行线未落盘
    class WebDavService:  # type: ignore[no-redef]
        pass

try:
    from app.core.events import BackupFinishedEvent, BackupProgressEvent
except Exception:  # pragma: no cover - 事件随并行线落地
    BackupFinishedEvent = BackupProgressEvent = None  # type: ignore[assignment]

#: WebDAV 连接配置在 SettingsService 中的统一键。
WEBDAV_CONFIG_KEY = "webdav.config"

#: kind → 界面文案（服务侧 BACKUP_TARGETS 的镜像；控制器不 import 服务模块，
#: 只依赖其 kind 契约，避免把服务 import 拖进界面派发路径）。
_KIND_LABELS = {"all": "全部", "sources": "书源", "bookshelf": "书架",
                "replace": "替换规则", "dict": "词典", "rss": "订阅源",
                "config": "设置"}


def _kind_label(kind) -> str:
    return _KIND_LABELS.get(str(kind or ""), str(kind or "全部"))


class WebDavController(BaseController):
    """备份设置：读连接配置 / 保存 / 备份 / 恢复 / 事件结果。"""

    destination = "tools.webdav"

    def on_attach(self) -> None:
        self._svc = self._safe_service(WebDavService)
        self._restore_inflight = False
        # 本页有没有发起过动作：决定结果行归 future 路径还是归事件（见模块文档）。
        self._started_action = False
        self._view.on_save_config = self.save_config
        self._view.on_backup = self.backup
        self._view.on_restore = self.restore
        self._view.on_list_remote = self.list_remotes
        if BackupFinishedEvent is not None:
            self.subscribe(BackupFinishedEvent, self._on_finished)
        if BackupProgressEvent is not None:
            self.subscribe(BackupProgressEvent, self._on_progress)
        self._load_config()

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    def _page(self):
        """对话框要挂的 page：未挂载时 `Control.page` 抛 RuntimeError，自己吞掉。"""
        view = self._view
        if view is None:
            return None
        try:
            return view.page
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    def _settings(self):
        try:
            from app.core.settings import SettingsService
            return self.service(SettingsService)
        except Exception:
            return None

    def _load_config(self) -> None:
        settings = self._settings()
        config = {}
        if settings is not None:
            try:
                config = settings.get(WEBDAV_CONFIG_KEY, {}) or {}
            except Exception:
                config = {}
        self._view.set_config(config)

    def save_config(self, config: dict) -> None:
        settings = self._settings()
        if settings is None:
            self._view.set_result(False, "SettingsService 未就绪")
            return
        config = config or {}
        try:
            settings.set(WEBDAV_CONFIG_KEY, config)
            # 服务 _resolve_client 读的是三个平铺键——只写嵌套 dict 会
            # "保存成功、备份却报未配置"。两套键必须同写。
            settings.set("webdav.url", str(config.get("url", "") or ""))
            settings.set("webdav.username", str(config.get("username", "") or ""))
            settings.set("webdav.password", str(config.get("password", "") or ""))
        except Exception as exc:
            self._view.set_result(False, f"保存失败：{exc}")
            return
        self._view.set_result(True, "连接配置已保存")

    def backup(self, kind: str) -> None:
        if self._svc is None:
            self._view.set_result(False, "WebDavService 未就绪")
            return
        self._started_action = True
        label = _kind_label(kind)
        # 结果行是"这一趟开始了/结束了"，阶段行是"现在正做到哪一步"（服务事件填）。
        self._view.set_progress(f"正在备份{label}…")
        self._view.set_result(True, f"正在备份：{label} …")
        future = self.ui_call(self._svc.backup(kind))
        future.add_done_callback(self.on_done(lambda f: self._after(f, kind)))

    def list_remotes(self) -> None:
        """拉远端备份清单填进下拉框：不列出来，恢复就得靠用户手敲 zip 路径。"""
        if self._svc is None:
            self._view.set_result(False, "WebDavService 未就绪")
            return
        self._started_action = True
        self._view.set_result(True, "正在列出远端备份…")
        future = self.ui_call(self._svc.list_remote())
        future.add_done_callback(self.on_done(lambda f: self._after_list(f)))

    def _after_list(self, future) -> None:
        if self._view is None:
            return
        self._view.clear_progress()
        try:
            entries = future.result()
        except Exception as exc:
            self._view.set_result(False, f"列出远端备份失败：{exc}")
            return
        entries = list(entries or [])
        self._view.set_remotes(entries)
        if not entries:
            self._view.set_result(
                False, "远端还没有备份包（或未配置 WebDAV 连接）")
            return
        self._view.set_result(True, f"远端备份 {len(entries)} 个，选一个恢复")

    def restore(self, path: str = "", confirmed: bool = False) -> None:
        if self._svc is None:
            self._view.set_result(False, "WebDavService 未就绪")
            return
        # 服务的 restore 参数是**远端备份包路径**，不是 WebDAV 基地址。
        path = str(path or "").strip()
        if not path:
            self._view.set_result(
                False, "请先点「列出远端备份」选一个备份包（或手填 .zip 路径）")
            return
        if not confirmed:
            # 点一下就整表落库不行：恢复覆盖的是书源表 + 书架 + 设置，而点的人
            # 看不见包里装的是什么。与导入的「整表替换」同一纪律：**拿不到 page
            # 就拒绝执行**，而不是"那就别问了直接恢复"。
            page = self._page()
            if page is None:
                self._view.set_result(False, "界面尚未就绪，本次恢复没有执行")
                return
            page.show_dialog(self._restore_dialog(path))
            return
        if getattr(self, "_restore_inflight", False):
            # 连点两下「确认恢复」= 两次覆盖：第二次的"恢复前留底"抄的是第一次
            # 恢复完的样子，真正的旧数据早就没了。
            self._view.set_result(False, "上一次恢复还在进行，请等它结束")
            return
        self._restore_inflight = True
        self._started_action = True
        self._view.set_progress("正在从云端恢复…")
        self._view.set_result(True, "正在从云端恢复…")
        config = self._read_saved_config()
        future = self.ui_call(
            self._svc.restore(path, config.get("password", ""), True))
        future.add_done_callback(self.on_done(self._after_restore))

    def _after_restore(self, future) -> None:
        self._restore_inflight = False      # 这一趟结束了（成功失败都算）
        self._after(future)

    def _restore_dialog(self, path: str) -> ft.AlertDialog:
        name = str(path or "").rstrip("/").split("/")[-1] or "（未命名备份包）"
        return ft.AlertDialog(
            modal=True,
            title=ft.Text("用云端备份覆盖本地数据？"),
            content=ft.Column(tight=True, spacing=8, controls=[
                ft.Text(f"将从 {name} 恢复。"),
                ft.Text(f"本地现有的 {self._existing_count()} 条书源，连同书架与设置"
                        "会被包里的内容覆盖。"),
                ft.Text("恢复前会自动在本地留一份现状（结果行里给出目录），"
                        "但那份留底只保这一次。", size=12),
                ft.Text("此操作不可撤销。", color=ft.Colors.RED_400, size=12),
            ]),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self._close_dialog()),
                ft.Button("确认恢复", icon=ft.Icons.CLOUD_DOWNLOAD,
                          color=ft.Colors.RED,
                          on_click=lambda e: self._confirm_restore(path)),
            ])

    def _confirm_restore(self, path: str) -> None:
        self._close_dialog()
        if self._view is not None:
            self.restore(path, confirmed=True)

    def _existing_count(self) -> int:
        """确认框里的"现有多少条书源"（读服务侧列表，取不到当 0，不拦确认）。"""
        try:
            from app.services.sources.source_service import SourceService
            source = self.service(SourceService)
        except Exception:
            return 0
        try:
            return len(getattr(source, "sources", None) or [])
        except Exception:
            return 0

    def _close_dialog(self) -> None:
        page = self._page()
        try:
            if page is not None:
                page.pop_dialog()
        except Exception:
            pass

    def _read_saved_config(self) -> dict:
        settings = self._settings()
        if settings is None:
            return {}
        config = {}
        try:
            config = dict(settings.get(WEBDAV_CONFIG_KEY, {}) or {})
        except Exception:
            config = {}
        # 嵌套 dict 缺项时用服务真正读的平铺键回填。
        for key, setting_key in (("url", "webdav.url"),
                                 ("username", "webdav.username"),
                                 ("password", "webdav.password")):
            if not config.get(key):
                try:
                    config[key] = str(settings.get(setting_key, "") or "")
                except Exception:
                    config[key] = ""
        return config

    def _after(self, future, kind: str = "") -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            try:
                result = future.result()
            except Exception as exc:
                self._view.set_result(False, f"操作失败：{exc}")
                return
            ok = bool(getattr(result, "ok", True))
            detail = str(getattr(result, "detail", "") or result or "")
            # 结果行带上用户选的那个对象的中文名：只写 "备份成功 → …zip" 的话，
            # 过一会儿就想不起来刚才备的是词典还是整库。
            self._view.set_result(
                ok, f"{_kind_label(kind)}：{detail}" if kind else detail)
        finally:
            # 收尾必关进度条：这一路抛异常时服务可能连 BackupFinishedEvent 都没发出去，
            # 只等事件的话条会一直转下去。
            self._view.clear_progress()

    # 进度路径：WebDavService 逐阶段发布 BackupProgressEvent。
    async def _on_progress(self, event) -> None:
        """把"现在正做到哪一步"写上屏。

        **不节流**（§5.11 的 0.5s 合并是给逐条高频事件的）：一次全量备份统共十几拍，
        而节流会把最后几拍推到收尾之后再落——进度条在结果都出来了还在转。
        """
        view = self._view
        if view is None:      # EventBus 异步派发：detach 挡不住已排进去的事件
            return
        view.set_progress(str(getattr(event, "stage", "") or ""),
                          int(getattr(event, "done", 0) or 0),
                          int(getattr(event, "total", 0) or 0))

    # 收尾路径：只负责关掉进度条；结果行归 ``_after``（本页发起过动作时）。
    async def _on_finished(self, event) -> None:
        if self._view is None:
            return
        self._view.clear_progress()
        if getattr(self, "_started_action", False):
            return
        # 别处触发的备份（脚本 / 网络桥）没有本页的 future 收尾，只能由事件报。
        self._view.set_result(bool(getattr(event, "ok", False)),
                              f"[{getattr(event, 'kind', '')}] {getattr(event, 'detail', '')}")
