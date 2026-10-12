"""UpdateService：应用更新检查（M5，契约 §9 / 设计 §1.17）。

定位（§1.17 红线）：**只检查、不静默自替换**。流程固定为
"检查 → 提示（UpdateAvailableEvent）→ 用户确认后下载到 downloads/ → 用户手动安装"。
刻意规避 Windows exe 自替换脚本 / macOS dmg 挂载 / Linux 包管理三端差异。

- ``check_update()``：经 aiohttp 查 GitHub/Gitee releases API（地址可配置
  ``update.api_url``，默认 GitHub latest release）；版本号与当前版本比较；
  命中新版本且未被忽略时发布 :class:`UpdateAvailableEvent`；
- ``download(update_info)``：把安装包拉到 :meth:`StorageService.download_dir()`，
  若 release 提供 digest（``<asset>.sha256`` 或资产内 ``digest`` 字段）则校验；
- ``ignore_version(version)``：经 SettingsService 记录忽略版本；
- ``SettingsService`` 记 ``update.last_check_time`` / ``update.ignored_version``。

测试不联网：构造期注入 ``http_get_json`` / ``http_download`` 两个异步可调用。
红线：本模块零 flet import；aiohttp 懒加载；跨平台。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.events import UpdateAvailableEvent
from app.core.lifecycle import Service
from app.core.settings import SettingsService
from app.core.storage import StorageService

logger = logging.getLogger(__name__)

# 默认 releases API（GitHub latest）。实际仓库地址经 settings.update.api_url 覆盖。
DEFAULT_API_URL = "https://api.github.com/repos/legado/legado/releases/latest"


@dataclass
class UpdateInfo:
    """一次可用更新的纯数据快照（视图层据此弹窗）。"""

    version: str
    notes: str
    url: str          # 安装包下载地址
    size: int = 0
    digest: str = ""  # 期望 sha256（hex），空串表示无可校验摘要


class UpdateService(Service):
    """更新检查与下载（不做安装）。"""

    name = "UpdateService"
    dependencies: tuple[type[Service], ...] = (StorageService, SettingsService)

    def __init__(self, storage=None, settings=None,
                 http_get_json=None, http_download=None,
                 current_version: str = "") -> None:
        super().__init__()
        self._storage = storage
        self._settings = settings
        # 网络入口可注入（测试替身，离线）；缺省用 aiohttp 真实请求。
        self._http_get_json = http_get_json
        self._http_download = http_download
        # 空串 = 本机版本未登记。别用 "0.0.0" 当默认：它在 ``_version_tuple`` 里
        # 退化成 (0,)，等于"任何远端版本都比本机新"，闸门恒放行，界面还显示一个
        # 不存在的版本号。留空时界面老实显示"未知"。
        self._current = str(current_version or "")

    @property
    def current_version(self) -> str:
        """本机版本（登记不到时为空串，界面据此显示「未知」）。"""
        return self._current

    async def on_start(self) -> None:
        hub = self.hub
        if self._storage is None and hub is not None:
            from app.core.storage import StorageService
            self._storage = hub.require(StorageService)
        if self._settings is None and hub is not None:
            from app.core.settings import SettingsService
            self._settings = hub.require(SettingsService)
        logger.info("UpdateService 已启动（当前版本 %s）", self._current or "未登记")

    def _publish(self, event) -> list:
        if self.bus is None:
            return []
        return self.bus.publish(event)

    # ------------------------------------------------------------------ #
    # 网络入口（默认 aiohttp；测试注入替身，全程不联网）
    # ------------------------------------------------------------------ #
    async def _get_json(self, url: str) -> dict:
        if self._http_get_json is not None:
            result = self._http_get_json(url)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        import aiohttp  # 懒加载
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"更新检查 HTTP {resp.status}")
                return await resp.json(content_type=None)

    async def _download(self, url: str) -> bytes:
        if self._http_download is not None:
            result = self._http_download(url)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        import aiohttp  # 懒加载
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"下载更新包 HTTP {resp.status}")
                return await resp.read()

    # ------------------------------------------------------------------ #
    # 检查
    # ------------------------------------------------------------------ #
    async def check_update(self) -> UpdateInfo | None:
        """查询 releases API；有更新且未被忽略时返回 UpdateInfo 并发布事件。

        无论是否有更新都会记录 ``update.last_check_time``；
        网络失败仅记日志并返回 None（不弹窗、不抛）。
        """
        url = (self._settings.get("update.api_url", "") if self._settings
               else None) or DEFAULT_API_URL
        try:
            data = await self._get_json(url)
        except Exception as exc:
            logger.warning("更新检查失败（忽略）: %s", exc)
            self._touch_last_check()
            return None

        info = _parse_release(data)
        self._touch_last_check()
        if info is None:
            return None

        if _version_tuple(info.version) <= _version_tuple(self._current):
            return None  # 不更新或已是最新
        ignored = (self._settings.get("update.ignored_version", "")
                   if self._settings else "")
        if ignored and ignored == info.version:
            return None  # 用户已忽略该版本

        self._publish(UpdateAvailableEvent(
            version=info.version, notes=info.notes, url=info.url))
        return info

    def _touch_last_check(self) -> None:
        if self._settings is not None:
            self._settings.set("update.last_check_time", time.time())

    async def ignore_version(self, version: str) -> None:
        """记录用户忽略某版本（该版本不再弹窗）。"""
        if self._settings is not None:
            self._settings.set("update.ignored_version", str(version or ""))

    # ------------------------------------------------------------------ #
    # 下载（落 downloads/，校验 sha256）
    # ------------------------------------------------------------------ #
    async def download(self, info: UpdateInfo) -> Path:
        """下载安装包到 downloads/；提供 digest 时校验 sha256，不符即删并抛错。"""
        data = await self._download(info.url)
        if info.digest:
            actual = hashlib.sha256(data).hexdigest()
            if actual.lower() != info.digest.lower():
                raise RuntimeError(
                    f"安装包哈希校验失败: 期望 {info.digest}，实得 {actual}")
        out_dir = (self._storage.download_dir() if self._storage is not None
                   else Path("downloads"))
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        name = info.url.rstrip("/").rsplit("/", 1)[-1] or f"update_{info.version}.bin"
        name = re.sub(r'[\\/:*?"<>|]+', "_", name)
        out_path = out_dir / name
        out_path.write_bytes(data)
        logger.info("更新包已下载: %s（%d 字节），请手动安装", out_path, len(data))
        return out_path


# --------------------------------------------------------------------------- #
# 解析 / 版本比较（纯函数，便于单测）
# --------------------------------------------------------------------------- #
def _parse_release(data: dict) -> UpdateInfo | None:
    """从 GitHub/Gitee release JSON 提取最新版本与首个安装包资产。"""
    tag = str(data.get("tag_name") or data.get("tag_name") or "").strip()
    if not tag:
        return None
    version = tag.lstrip("vV").strip()
    notes = str(data.get("body") or data.get("name") or "")
    assets = data.get("assets") or []
    url = ""
    size = 0
    digest = ""
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        browser_url = asset.get("browser_download_url") or asset.get("url") or ""
        if not browser_url:
            continue
        name = str(asset.get("name") or "")
        # 优先选可执行安装包（win/mac/linux 常规后缀），跳过校验和文档资产。
        if name.endswith(".sha256") or name.endswith(".txt"):
            digest = digest or browser_url
            continue
        if browser_url.lower().endswith(".sha256"):
            digest = digest or browser_url
            continue
        url = browser_url
        size = int(asset.get("size") or 0)
        if name.lower().endswith((".exe", ".msi", ".dmg", ".deb", ".rpm", ".AppImage")):
            break  # 命中安装包即止
    if not url:
        # 无资产时仍提示有更新（用户可经 release 页手动下载）。
        url = str(data.get("html_url") or "")
    return UpdateInfo(version=version, notes=notes, url=url, size=size, digest=digest)


def _version_tuple(version: str) -> tuple:
    """把 '1.2.3' / 'v1.2.3-rc1' 解析为可比较的数字元组。"""
    parts = re.findall(r"\d+", str(version or ""))
    return tuple(int(p) for p in parts) if parts else (0,)
