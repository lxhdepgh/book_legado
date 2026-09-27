"""WebDavService：WebDAV 云备份 / 恢复 / 远端列表（M5，契约 §3 / 设计 §1.5）。

职责（§1.5）：

- 备份编排"**导出 zip → 上传**"：把书源 / 书架 / 设置 / （可选）替换规则 /
  词典 / RSS 源各自导出为 JSON，打成带完整性清单的 zip 上传到 WebDAV；
- 恢复"**拉取 → 校验 → 导入**"：先下载备份包，**强制校验** zip 结构 /
  manifest 内每个成员的 SHA-256 / JSON 可解析性，任一不通过即中止且**不写入**；
- 备份 JSON 结构与 Legado 备份格式对齐（跨平台互操作）：书源数组即
  :meth:`SourceService.export_sources_json` 的产物，书架为 Legado Book 记录数组。

客户端选型裁决（契约 §3 要求写明理由）：**采用 aiohttp 自实现 PROPFIND/PUT/GET**，
不引入 webdavclient3。理由：

1. 本服务跑在主进程异步网络栈上（aiohttp 3.14 已是全项目统一 HTTP 客户端），
   webdavclient3 底层是 requests 同步库，需要 ``loop.run_in_executor`` 线程池桥接，
   徒增阻塞风险与依赖；
2. 我们只用到 WebDAV 三个最小动作（PROPFIND 列目录 / PUT 上传 / GET 下载，
   外加可选 MKCOL 建目录），自实现约百行、语义透明，无需为全量 WebDAV
   特性（锁/版本控/属性补丁）背一个重型依赖；
3. aiohttp 自实现天然复用现有超时 / UA / 代理配置，三端一致。

红线：本模块零 flet import；aiohttp 懒加载；跨平台；恢复是高风险写操作——
**未确认不下载**（``restore(..., confirm=True)`` 才动手）、**校验失败绝不落库**、
落库前先把将被覆盖的现状在 ``<数据根>/restore-safety/`` 留一份。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.core.events import BackupFinishedEvent, BackupProgressEvent
from app.core.lifecycle import Service

logger = logging.getLogger(__name__)

# 备份包格式标识与版本（恢复时校验，未来格式演进据此兼容）。
BACKUP_FORMAT = "legado-desktop-backup"
BACKUP_VERSION = 1
# 远端备份目录（与 Legado 约定的 legado/ 前缀对齐）。
REMOTE_BACKUP_DIR = "legado/backup"

#: 界面「备份对象」下拉的键 → 该对象在 zip 里的成员文件名。
#: ``backup(kind)`` 按这张表裁剪成员，所以 kind 不再是"只进事件的标签"。
BACKUP_TARGETS: dict[str, tuple[str, ...]] = {
    "sources": ("bookSources.json",),
    "bookshelf": ("bookShelf.json",),
    "replace": ("replaceRules.json",),
    "dict": ("dictRules.json",),
    "rss": ("rssSources.json",),
    "config": ("config.json",),
}
#: 全量备份的别名（历史调用方传 "full"）。
_ALL_KINDS = ("all", "full", "")

#: zip 成员名 → 给人看的名字。进度句子里用得到（"正在恢复 书架…"），
#: 光报 ``bookShelf.json`` 用户不知道那一行动的是他自己的书架。
#: 与控制器 ``_KIND_LABELS`` 不是重复：那张表键是下拉框的 kind，这张表键是成员名。
_MEMBER_LABELS: dict[str, str] = {
    "bookSources.json": "书源",
    "bookShelf.json": "书架",
    "replaceRules.json": "替换规则",
    "dictRules.json": "词典",
    "rssSources.json": "订阅源",
    "config.json": "设置",
    "meta.json": "备份清单",
}


def _member_label(name: str) -> str:
    return _MEMBER_LABELS.get(str(name or ""), str(name or "数据"))


def _target_members(kind: str) -> tuple[str, ...] | None:
    """kind 对应的成员名集合；全量返回 None（不裁剪）。"""
    if str(kind or "") in _ALL_KINDS:
        return None
    return BACKUP_TARGETS.get(str(kind or ""))


@dataclass
class BackupResult:
    """备份 / 恢复结果（视图层只读展示）。"""

    ok: bool
    detail: str = ""
    path: str = ""          # 远端备份包路径（backup）或本地临时拉取路径（restore）
    size: int = 0           # 备份包字节数
    members: dict = field(default_factory=dict)  # 各成员条数摘要（书源数等）
    snapshot: str = ""      # 恢复前本地留底目录（未留底为空）


# --------------------------------------------------------------------------- #
# WebDAV 客户端：aiohttp 自实现 PROPFIND/PUT/GET（懒加载 aiohttp）。
# 设计为鸭子类型协议：测试可注入"本地目录当远端"的假客户端，零网络。
# --------------------------------------------------------------------------- #
class WebDavClient:
    """最小 WebDAV 客户端（PROPFIND 列目录 / PUT 上传 / GET 下载 / MKCOL）。

    全部方法为协程；``base_url`` 为 WebDAV 根（如 https://dav.example.com/remote.php/dav/files/user/）。
    """

    def __init__(self, base_url: str, username: str = "", password: str = "",
                 timeout: float = 30.0) -> None:
        self._base = str(base_url or "").rstrip("/")
        self._username = username or ""
        self._password = password or ""
        self._timeout = float(timeout)
        self._session = None  # aiohttp.ClientSession，懒建

    # -- 内部 --------------------------------------------------------------- #
    def _ensure_session(self):
        """懒建 aiohttp 会话（aiohttp 本模块外不 import，测试替身不触发）。"""
        if self._session is not None:
            return self._session
        import aiohttp  # 懒加载：无 WebDAV 配置 / 纯单测时不依赖
        auth = None
        if self._username:
            auth = aiohttp.BasicAuth(self._username, self._password)
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        self._session = aiohttp.ClientSession(auth=auth, timeout=timeout)
        return self._session

    @staticmethod
    def _join(base: str, path: str) -> str:
        """拼远端绝对 URL（path 以 / 开头视为相对 WebDAV 根）。"""
        path = str(path or "").lstrip("/")
        from urllib.parse import quote
        # 逐段 URL 编码，保留目录分隔符
        segs = [quote(seg, safe="") for seg in path.split("/") if seg]
        return base + "/" + "/".join(segs)

    # -- 对外动作 ----------------------------------------------------------- #
    async def list(self, path: str = "") -> list[dict]:
        """PROPFIND Depth:1 列目录，返回 [{name, path, size, modified, is_dir}]。"""
        import aiohttp
        import xml.etree.ElementTree as ET
        session = self._ensure_session()
        url = self._join(self._base, path)
        async with session.request(
                "PROPFIND", url, headers={"Depth": "1"}) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"PROPFIND 失败 HTTP {resp.status}: {url}")
            body = await resp.read()
        ns = "{DAV:}"
        root = ET.fromstring(body)
        entries: list[dict] = []
        for resp_el in root.findall(f"{ns}response"):
            href_el = resp_el.find(f"{ns}href")
            if href_el is None or not href_el.text:
                continue
            href = href_el.text
            prop = resp_el.find(f"{ns}propstat/{ns}prop")
            is_dir = False
            size = 0
            modified = ""
            if prop is not None:
                res_type = prop.find(f"{ns}resourcetype/{ns}collection")
                is_dir = res_type is not None
                size_el = prop.find(f"{ns}getcontentlength")
                if size_el is not None and size_el.text:
                    try:
                        size = int(size_el.text)
                    except ValueError:
                        size = 0
                mod_el = prop.find(f"{ns}getlastmodified")
                if mod_el is not None:
                    modified = mod_el.text or ""
            name = href.rstrip("/").rsplit("/", 1)[-1]
            if not name:  # 跳过目录自引用
                continue
            entries.append({
                "name": name, "path": href, "size": size,
                "modified": modified, "is_dir": is_dir,
            })
        return entries

    async def download(self, path: str) -> bytes:
        """GET 拉取远端文件，返回字节。"""
        session = self._ensure_session()
        url = self._join(self._base, path)
        async with session.get(url) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"下载失败 HTTP {resp.status}: {path}")
            return await resp.read()

    async def upload(self, path: str, data: bytes) -> None:
        """PUT 上传字节到远端路径（中间目录需预先存在，见 mkdir）。"""
        session = self._ensure_session()
        url = self._join(self._base, path)
        async with session.put(url, data=data) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"上传失败 HTTP {resp.status}: {path}")

    async def mkdir(self, path: str) -> None:
        """MKCOL 建目录（已存在 / 不支持时静默忽略，不阻断备份）。"""
        session = self._ensure_session()
        url = self._join(self._base, path)
        try:
            async with session.request("MKCOL", url) as resp:
                # 201 建成功；405 已存在；409 父目录缺失——都不阻断
                if resp.status not in (201, 405, 409):
                    logger.warning("MKCOL %s -> HTTP %s（忽略）", path, resp.status)
        except Exception:
            logger.exception("MKCOL %s 异常（忽略）", path)

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


# --------------------------------------------------------------------------- #
# 服务本体
# --------------------------------------------------------------------------- #
class WebDavService(Service):
    """WebDAV 备份 / 恢复编排（契约 §3）。

    协作者全部可注入（测试替身）；未注入时 ``on_start`` 经 ServiceHub 解析。
    ReplaceRuleService / DictService / RssService 由 M5 其它并行线交付，
    本服务经鸭子类型**可选**调用其导出方法——未注册时备份包内缺该成员，
    不阻断、不硬依赖，避免跨线编译耦合。
    """

    name = "WebDavService"
    dependencies: tuple[type[Service], ...] = ()

    def __init__(self, source_service=None, settings=None, storage=None,
                 client: WebDavClient | None = None,
                 replace_service=None, dict_service=None,
                 rss_service=None) -> None:
        super().__init__()
        self._source = source_service
        self._settings = settings
        self._storage = storage
        self._client = client
        self._replace = replace_service
        self._dict = dict_service
        self._rss = rss_service

    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        hub = self.hub
        if self._source is None and hub is not None:
            try:
                from app.services.sources.source_service import SourceService
                self._source = hub.require(SourceService)
            except Exception:
                self._source = None
        if self._settings is None and hub is not None:
            from app.core.settings import SettingsService
            self._settings = hub.require(SettingsService)
        if self._storage is None and hub is not None:
            from app.core.storage import StorageService
            self._storage = hub.require(StorageService)
        # 可选协作者：未注册/并行线未交付时降级为 None（备份缺成员不阻断）。
        if self._replace is None and hub is not None:
            self._replace = _try_require(hub, "ReplaceRuleService")
        if self._dict is None and hub is not None:
            self._dict = _try_require(hub, "DictService")
        if self._rss is None and hub is not None:
            self._rss = _try_require(hub, "RssService")
        logger.info("WebDavService 已启动")

    async def on_stop(self) -> None:
        if self._client is not None and isinstance(self._client, WebDavClient):
            try:
                await self._client.aclose()
            except Exception:
                logger.exception("WebDavClient 关闭异常")

    def _publish(self, event) -> list:
        if self.bus is None:
            return []
        return self.bus.publish(event)

    def _emit_progress(self, kind: str, stage: str,
                       done: int = 0, total: int = 0) -> None:
        """发布逐阶段进度（total=0 表示这一拍给不出条数，界面走不定长进度条）。

        备份/恢复是全流程最慢的两个动作（导出六类数据 → 逐个算 SHA-256 → 走网络
        → 整表落库），只有一句"正在备份"挂十几秒跟卡死没法区分；而 :meth:`backup`
        的返回值要等全部做完才有，中途什么都拿得到——只能靠事件。
        """
        if self.bus is None:
            return
        try:
            self.bus.publish(BackupProgressEvent(
                kind=kind, stage=str(stage or ""), done=int(done), total=int(total)))
        except Exception:
            # 进度是装饰，备份是正事：总线/事件环出毛病时只记一条 warning，
            # 绝不能让用户点了「立即备份」却收到"操作失败"。
            logger.warning("备份进度发布失败: %s", stage, exc_info=True)

    def _resolve_client(self) -> WebDavClient | None:
        """注入优先；否则从 SettingsService 读 webdav.* 配置建客户端。"""
        if self._client is not None:
            return self._client
        if self._settings is None:
            return None
        base = self._settings.get("webdav.url", "")
        if not base:
            return None
        return WebDavClient(
            base,
            self._settings.get("webdav.username", ""),
            self._settings.get("webdav.password", ""))

    # ------------------------------------------------------------------ #
    # 备份：导出 JSON → 打 zip（含 hash 清单）→ 上传
    # ------------------------------------------------------------------ #
    async def backup(self, kind: str = "all") -> BackupResult:
        """按 :data:`BACKUP_TARGETS` 备份所选对象并上传。

        ``kind`` 以前只是塞进事件的标签——选「词典」照样把整库打包传上去，用户以
        为备份是分开的。现在它真正决定 zip 里有哪些成员；``all``/``full`` 是全量。
        """
        kind = str(kind or "all").strip() or "all"
        wanted = _target_members(kind)
        if wanted is None and kind not in _ALL_KINDS:
            return BackupResult(
                False, f"未知备份对象：{kind}（可用：{'、'.join(sorted(BACKUP_TARGETS))}、all）")
        if self._source is None:
            result = BackupResult(False, "书源服务未就绪，无法备份")
            self._publish(BackupFinishedEvent(kind=kind, ok=False, detail=result.detail))
            return result
        client = self._resolve_client()
        if client is None:
            result = BackupResult(False, "未配置 WebDAV 连接（webdav.url）")
            self._publish(BackupFinishedEvent(kind=kind, ok=False, detail=result.detail))
            return result

        try:
            self._emit_progress(kind, "正在导出各服务的数据…")
            members, summary = await self._collect_members(
                lambda name: self._emit_progress(
                    kind, f"正在导出{_member_label(name)}…"),
                only=set(wanted) if wanted is not None else None)
        except Exception as exc:
            logger.exception("备份导出阶段失败")
            result = BackupResult(False, f"导出失败: {exc}")
            self._publish(BackupFinishedEvent(kind=kind, ok=False, detail=result.detail))
            return result
        if wanted is not None:
            skipped = [name for name in wanted if name not in members]
            members = {name: text for name, text in members.items()
                       if name in wanted}
            if skipped:
                summary["skipped"] = skipped
        if not members:
            result = BackupResult(False, f"「{kind}」没有可备份的内容")
            self._publish(BackupFinishedEvent(kind=kind, ok=False, detail=result.detail))
            return result
        summary["members"] = len(members)

        # 逐个成员算 SHA-256，写进 manifest（恢复时据此校验完整性）。
        import hashlib
        manifest: dict[str, str] = {}
        total = len(members)
        for index, (name, text) in enumerate(members.items(), start=1):
            manifest[name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            self._emit_progress(
                kind, f"正在校验 {_member_label(name)}（{index}/{total}）",
                done=index, total=total)
        meta = {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
            "kind": kind,
            "exported_at": int(time.time()),
            "manifest": manifest,
        }

        # 打 zip：meta.json 在前，各 JSON 成员在后。
        self._emit_progress(kind, f"正在压缩打包（{total} 个文件）")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
            for name, text in members.items():
                zf.writestr(name, text)
        data = buf.getvalue()

        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        remote_path = f"{REMOTE_BACKUP_DIR}/legado_backup_{kind}_{ts}.zip"
        try:
            self._emit_progress(kind, f"正在上传（{len(data)} 字节）…")
            await client.mkdir(REMOTE_BACKUP_DIR)
            await client.upload(remote_path, data)
        except Exception as exc:
            logger.exception("备份上传失败")
            result = BackupResult(False, f"上传失败: {exc}", size=len(data))
            self._publish(BackupFinishedEvent(kind=kind, ok=False, detail=result.detail))
            return result

        # 只有包里真装了书源才报条数：备词典时报"书源 0 条"是凭空多出来的一句。
        counted = (f"，书源 {summary.get('book_sources', 0)} 条"
                   if "bookSources.json" in members else "")
        detail = f"备份成功 → {remote_path}（{len(data)} 字节{counted}）"
        result = BackupResult(True, detail, path=remote_path,
                              size=len(data), members=summary)
        self._publish(BackupFinishedEvent(kind=kind, ok=True, detail=detail))
        return result

    async def _collect_members(self, on_export=None,
                               only: set[str] | None = None
                               ) -> tuple[dict[str, str], dict]:
        """从各协作服务导出成员 JSON 文本；可选服务未就绪则缺省。

        ``on_export(name)`` 每导完一个成员调一次（进度上报钩子）。总数事先不知道
        ——``replaceRules.json`` 这些要看并行线服务在不在——所以钩子只给名字，
        进度事件那一侧 ``total`` 留 0。

        ``only`` 是给定的成员白名单：选「词典」却把几百条书源整个序列化一遍，既慢
        又会在进度里报出"正在导出书源"——用户看到的是它没点过的东西。
        """
        def _exported(name: str) -> None:
            if on_export is not None:
                try:
                    on_export(name)
                except Exception:      # 进度钩子绝不能拖垮导出
                    logger.warning("备份导出进度上报失败", exc_info=True)

        def _need(name: str) -> bool:
            """这个成员这次要不要导（白名单外的一律不碰：导出动作可能很重）。"""
            return only is None or name in only

        members: dict[str, str] = {}
        summary: dict[str, int] = {}

        # 书源：SourceService 导出 JSON 数组文本（Legado 书源格式对齐）。
        if _need("bookSources.json"):
            sources_text = await self._source.export_sources_json()
            members["bookSources.json"] = sources_text
            _exported("bookSources.json")
            try:
                summary["book_sources"] = len(json.loads(sources_text))
            except Exception:
                summary["book_sources"] = 0

        # 书架：Legado Book 记录数组（与 bookshelf.json 磁盘格式一致）。
        if _need("bookShelf.json"):
            bookshelf = list(getattr(self._source, "bookshelf", []) or [])
            members["bookShelf.json"] = json.dumps(
                bookshelf, ensure_ascii=False, indent=2)
            summary["bookshelf"] = len(bookshelf)
            _exported("bookShelf.json")

        # 设置：只备份读者偏好子集，排除 WebDAV 凭据本身（安全）。
        if _need("config.json"):
            members["config.json"] = json.dumps(
                self._export_settings(), ensure_ascii=False, indent=2)
            _exported("config.json")

        # 以下为可选并行线服务：鸭子类型探测其导出方法，缺省不阻断。
        for member, svc, method in (
                ("replaceRules.json", self._replace, "export_rules_json"),
                ("dictRules.json", self._dict, "export_rules_json"),
                ("rssSources.json", self._rss, "export_sources_json")):
            if not _need(member):
                continue
            text = await _maybe_export(svc, method)
            if text is not None:
                members[member] = text
                _exported(member)
        return members, summary

    def _export_settings(self) -> dict:
        """导出读者偏好子集（剔除连接凭据 / 临时状态键）。"""
        if self._settings is None:
            return {}
        banned = {"webdav.password", "webdav.token", "update.ignored_version"}
        out: dict = {}
        # SettingsService 无全量迭代接口，按已知 reader.* 前缀白名单导出。
        # 键名必须是**实际写入的那些**：阅读偏好统一是 ``reader.<ReaderView 偏好名>``
        # （ReadingService.set_reader_preference），此前这里列的是
        # ``reader.font.family``/``reader.font.size``/``reader.line_spacing`` 这种
        # 没人写过的键，读者设置永远备不出去。
        for key in ("reader.font_family", "reader.font_size",
                    "reader.line_height", "reader.paragraph_spacing",
                    "reader.max_width", "reader.indent", "reader.text_align",
                    "reader.background", "reader.show_status",
                    "reader.text_selection",
                    "reader.shortcuts", "reader.tts_rate", "theme.skin"):
            if key in banned:
                continue
            val = self._settings.get(key, None)
            if val is not None:
                out[key] = val
        return out

    # ------------------------------------------------------------------ #
    # 恢复：下载 → 校验完整性 → 导入（校验失败绝不写）
    # ------------------------------------------------------------------ #
    async def restore(self, url: str, token: str = "",
                      confirm: bool = False) -> BackupResult:
        """从远端备份包恢复。``url`` 为远端 zip 路径；``token`` 为一次性口令覆盖。

        校验顺序：zip 可打开 → meta.json 可读且格式/版本匹配 → manifest 每个成员
        哈希一致 → 各 JSON 可解析。任一失败即返回且**不触碰本地数据**。

        ``confirm``：调用方**确认过**这是用户要干的事。恢复是整表级覆盖（导入的
        「整表替换」尚且要弹确认框，这里更狠：点的人看不见包里装的是什么），所以
        没确认就直接返回、连下载都不做。界面侧确认框点「确认恢复」后传 ``True``；
        绕过界面的调用（脚本 / 网络桥）也必须显式传——**默认值保守**是这条的参数顺序。
        """
        remote_path = str(url or "")
        if not remote_path:
            result = BackupResult(False, "未指定备份包路径")
            self._publish(BackupFinishedEvent(kind="restore", ok=False, detail=result.detail))
            return result
        if not confirm:
            result = BackupResult(
                False, "恢复会覆盖本地书源/书架/设置，需确认后才执行（未下载、未改动）",
                path=remote_path)
            self._publish(BackupFinishedEvent(kind="restore", ok=False, detail=result.detail))
            return result
        client = self._resolve_client()
        if client is None:
            result = BackupResult(False, "未配置 WebDAV 连接（webdav.url）")
            self._publish(BackupFinishedEvent(kind="restore", ok=False, detail=result.detail))
            return result

        # 1) 下载
        try:
            self._emit_progress("restore", "正在下载备份包…")
            data = await client.download(remote_path)
        except Exception as exc:
            logger.exception("恢复下载失败")
            result = BackupResult(False, f"下载备份包失败: {exc}", path=remote_path)
            self._publish(BackupFinishedEvent(kind="restore", ok=False, detail=result.detail))
            return result

        # 2) 校验（失败即返回，不写入）
        try:
            self._emit_progress("restore", "正在校验备份包完整性…")
            members = _validate_backup_zip(data)
        except _BackupCorrupt as exc:
            logger.warning("备份包校验失败，已拒绝恢复: %s", exc)
            result = BackupResult(False, f"备份包校验失败，未做任何修改: {exc}",
                                  path=remote_path)
            self._publish(BackupFinishedEvent(kind="restore", ok=False, detail=result.detail))
            return result

        # 3) 校验通过才动手：先本地留底，再导入（高风险写操作）
        self._emit_progress(
            "restore", f"正在把将被覆盖的现状留一份（{len(members)} 个文件）…")
        snapshot = await self._snapshot_local(set(members))
        try:
            applied = await self._apply_members(
                members,
                lambda name, index, total: self._emit_progress(
                    "restore", f"正在恢复{_member_label(name)}（{index}/{total}）",
                    done=index, total=total))
        except Exception as exc:
            logger.exception("恢复导入失败")
            result = BackupResult(False, f"导入失败: {exc}", path=remote_path,
                                  snapshot=snapshot)
            self._publish(BackupFinishedEvent(kind="restore", ok=False, detail=result.detail))
            return result

        detail = (f"恢复成功 ← {remote_path}（{json.dumps(applied, ensure_ascii=False)}）"
                  + (f"；恢复前的本地数据留底在 {snapshot}" if snapshot else ""))
        result = BackupResult(True, detail, path=remote_path,
                              size=len(data), members=applied, snapshot=snapshot)
        self._publish(BackupFinishedEvent(kind="restore", ok=True, detail=detail))
        return result

    # ------------------------------------------------------------------ #
    # 恢复前的本地留底（保险丝，不是关卡）
    # ------------------------------------------------------------------ #
    def _snapshot_root(self) -> Path | None:
        """留底根目录：``<数据根>/restore-safety/``（不放 cache，缓存会被清）。"""
        if self._storage is None:
            return None
        try:
            return Path(self._storage.paths.data) / "restore-safety"
        except Exception:
            logger.warning("数据根不可用，恢复前不留底", exc_info=True)
            return None

    async def _snapshot_local(self, overwriting: set[str]) -> str:
        """把**将被覆盖的那几样**现状先写到本地一份，返回目录路径（拿不到就返回 ""）。

        校验通过的备份包照样可能是**错的那一个**（旧机器、旧版本、传错文件）；
        没有这份留底，"恢复完才发现不对"就只能重头配几百条源。只写成员名相交的那些
        ——包里没有 ``bookShelf.json`` 就别为书架留底。

        失败**不阻断**恢复：留底是保险丝，不能反过来变成用户点「恢复」却什么都不发生的
        第三个理由。
        """
        if self._source is None or not overwriting:
            return ""
        root = self._snapshot_root()
        if root is None:
            return ""
        try:
            members, _ = await self._collect_members(
                lambda name: self._emit_progress(
                    "restore", f"正在取{_member_label(name)}现状（留底）…"),
                only=set(overwriting))
        except Exception:
            logger.warning("恢复前取现状失败，跳过留底继续恢复", exc_info=True)
            return ""
        wanted = {name: text for name, text in members.items() if name in overwriting}
        if not wanted:
            return ""
        try:
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
            target = root / stamp
            n = 1
            while target.exists():   # 同一秒内两次恢复：别把第一次的留底盖掉
                target = root / f"{stamp}_{n}"
                n += 1
            target.mkdir(parents=True)
            for name, text in wanted.items():
                (target / name).write_text(text, encoding="utf-8")
            return str(target)
        except Exception:
            logger.warning("恢复前留底写盘失败，跳过留底继续恢复", exc_info=True)
            return ""

    async def _apply_members(self, members: dict[str, str],
                             on_applied=None) -> dict:
        """把校验过的各成员写回对应服务。调用前已通过完整性校验。

        ``on_applied(name, index, total)`` 每落库一个成员调一次；``total`` 是包里的
        成员数（有的成员本地没有对应服务会跳过，所以 ``index`` 可能提前收尾——
        进度条宁可少跑一格，也不要最后一步突然跳满又退回）。
        """
        applied: dict[str, int] = {}
        total = len(members)
        index = 0

        def _done(name: str) -> None:
            nonlocal index
            index += 1
            if on_applied is None:
                return
            try:
                on_applied(name, index, total)
            except Exception:          # 进度钩子绝不能打断落库
                logger.warning("恢复进度上报失败", exc_info=True)

        if "bookSources.json" in members and self._source is not None:
            count, errors = await self._source.import_sources_json(members["bookSources.json"])
            applied["book_sources"] = count
            _done("bookSources.json")
            if errors:
                logger.info("书源恢复告警 %d 条", len(errors))

        if "bookShelf.json" in members:
            bookshelf = json.loads(members["bookShelf.json"])
            if isinstance(bookshelf, list) and self._source is not None:
                self._source.bookshelf = bookshelf
                self._source.save_bookshelf()
                applied["bookshelf"] = len(bookshelf)
            _done("bookShelf.json")

        if "config.json" in members and self._settings is not None:
            config = json.loads(members["config.json"])
            if isinstance(config, dict):
                for key, value in config.items():
                    self._settings.set(key, value)
                applied["config"] = len(config)
            _done("config.json")

        # 可选并行线成员：鸭子类型探测导入方法。
        for member, svc, method in (
                ("replaceRules.json", self._replace, "import_rules_json"),
                ("dictRules.json", self._dict, "import_rules_json"),
                ("rssSources.json", self._rss, "import_sources_json")):
            if member in members:
                n = await _maybe_import(svc, method, members[member])
                if n is not None:
                    applied[member] = n
                _done(member)
        return applied

    # ------------------------------------------------------------------ #
    async def list_remote(self) -> list[dict]:
        """列出远端备份目录下的 .zip 备份包（按修改时间倒序）。"""
        client = self._resolve_client()
        if client is None:
            return []
        try:
            entries = await client.list(REMOTE_BACKUP_DIR)
        except Exception:
            logger.exception("列远端备份目录失败")
            return []
        zips = [e for e in entries
                if not e["is_dir"] and e["name"].lower().endswith(".zip")]
        zips.sort(key=lambda e: e["modified"], reverse=True)
        return zips


# --------------------------------------------------------------------------- #
# 完整性校验（高风险恢复前的强制闸门）
# --------------------------------------------------------------------------- #
class _BackupCorrupt(Exception):
    """备份包校验失败（结构 / hash / JSON 任一不过）。"""


def _validate_backup_zip(data: bytes) -> dict[str, str]:
    """解开备份 zip，校验 meta.json + 逐成员 SHA-256 + JSON 可解析性。

    Returns:
        {成员名: 文本}  通过校验的成员字典。
    Raises:
        _BackupCorrupt: 任一校验失败即抛，调用方不得写入。
    """
    import hashlib
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise _BackupCorrupt(f"非 zip 包: {exc}") from exc

    names = set(zf.namelist())
    if "meta.json" not in names:
        raise _BackupCorrupt("缺少 meta.json 清单")
    try:
        meta = json.loads(zf.read("meta.json").decode("utf-8"))
    except Exception as exc:
        raise _BackupCorrupt(f"meta.json 不可解析: {exc}") from exc
    if meta.get("format") != BACKUP_FORMAT:
        raise _BackupCorrupt(f"备份格式不符: {meta.get('format')!r}")
    if int(meta.get("version", 0)) > BACKUP_VERSION:
        raise _BackupCorrupt(f"备份版本过高: {meta.get('version')}")

    manifest = meta.get("manifest") or {}
    members: dict[str, str] = {}
    for name, expected_hash in manifest.items():
        if name not in names:
            raise _BackupCorrupt(f"清单成员缺失: {name}")
        raw = zf.read(name)
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected_hash:
            raise _BackupCorrupt(f"成员哈希不匹配: {name}")
        text = raw.decode("utf-8")
        # JSON 成员必须可解析（config/bookShelf/bookSources 均为 JSON）。
        try:
            json.loads(text)
        except Exception as exc:
            raise _BackupCorrupt(f"成员 {name} JSON 不可解析: {exc}") from exc
        members[name] = text
    return members


# --------------------------------------------------------------------------- #
# 可选协作服务的鸭子类型调用（并行线未交付时静默降级）
# --------------------------------------------------------------------------- #
def _try_require(hub, class_name: str):
    """经类名懒取已注册服务；未注册 / 导入失败返回 None（不阻断启动）。"""
    try:
        import inspect
        mod_map = {
            "ReplaceRuleService": "app.services.tools.replace_rule_service",
            "DictService": "app.services.tools.dict_service",
            "RssService": "app.services.content.rss_service",
        }
        mod = __import__(mod_map[class_name], fromlist=[class_name])
        stype = getattr(mod, class_name)
        return hub.require(stype)
    except Exception:
        return None


async def _maybe_export(svc, method_name: str):
    """鸭子类型调 ``export_*_json`` 导出；无此方法或失败返回 None。"""
    method = getattr(svc, method_name, None)
    if method is None:
        return None
    try:
        result = method()
        if asyncio.iscoroutine(result):
            result = await result
        return result
    except Exception:
        logger.exception("可选服务导出 %s 失败（忽略）", method_name)
        return None


async def _maybe_import(svc, method_name: str, text: str):
    """鸭子类型调 ``import_*``；无此方法返回 None。"""
    method = getattr(svc, method_name, None)
    if method is None:
        return None
    try:
        result = method(text)
        if asyncio.iscoroutine(result):
            result = await result
        return result[0] if isinstance(result, tuple) else result
    except Exception:
        logger.exception("可选服务导入 %s 失败（忽略）", method_name)
        return None
