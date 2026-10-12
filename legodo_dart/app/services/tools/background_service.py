"""BackgroundService：全应用一层背景图的落点与偏好（第七十四轮）。

**为什么把图拷进数据目录，而不是直接用用户选中的那个路径**：原图可能在移动盘、网盘
或一个马上会被整理的下载文件夹里，下次开机它就没了——界面上剩一块裂图，而用户已经
不知道自己改过什么。所以这里把图**归一后拷进 ``<data>/backgrounds/``**，库里存的键值
是那份拷贝的路径；换图时旧的那份（只在我们自己目录里的那份）随手删掉，不留垃圾。

归一走 Pillow：长边压到 :data:`_MAX_EDGE`、转 JPEG。一张 6000×4000 的手机照片直接铺
满窗口，客户端每次重建都要解它，几百 MB 的原图不该天天被解码。没装 Pillow 就原样
拷贝（功能不退化成"用不了"，只是不压）。

服务层零 flet（红线）：本模块只管文件与偏好；把路径落成窗口上那一层在
:meth:`~app.views.navigation.NavigationHost.set_background`（视图层），变更经
:class:`SettingsChangedEvent` 由装配根搬运——切页、缩放重建外壳都不会把背景弄丢。
"""
from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

from app.core.lifecycle import Service
from app.core.storage import StorageService

logger = logging.getLogger(__name__)

#: 背景图偏好的 SettingsService 键（空串 = 没有背景）。
BACKGROUND_IMAGE_KEY = "appearance.background.image"

#: 归一后的长边上限（像素）。窗口再宽也只是一张铺满的位图，超过这个数看不出差别，
#: 却会把解码内存和数据目录一起撑大。
_MAX_EDGE = 2560

#: JPEG 质量：背景层永远罩着一层压暗、又坐在文字后面，85 已经看不出压缩痕迹。
_JPEG_QUALITY = 85


class BackgroundService(Service):
    """背景图：拷进数据目录 + 归一 + 单键偏好（换图/清除都发 SettingsChangedEvent）。"""

    name = "BackgroundService"
    dependencies: tuple[type[Service], ...] = (StorageService,)

    def __init__(self, storage: StorageService | None = None, settings=None) -> None:
        super().__init__()
        self._storage = storage
        self._settings = settings

    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            self._storage = self.hub.require(StorageService)
        if self._settings is None and self.hub is not None:
            try:
                from app.core.settings import SettingsService
                self._settings = self.hub.require(SettingsService)
            except Exception:
                logger.debug("SettingsService 未就绪，背景图只活在本次会话", exc_info=True)

    # ------------------------------------------------------------------ #
    # 读写面
    # ------------------------------------------------------------------ #
    @property
    def current(self) -> str:
        """当前背景图的绝对路径字符串；没有、或文件已经不在了 → 空串。

        "文件没了就当没有"是必须的：这一层的替代品是"纯色底"，裂图比没背景难看。
        """
        raw = self._raw_value()
        if not raw:
            return ""
        try:
            if Path(str(raw)).is_file():
                return str(raw)
        except OSError:
            return ""
        return ""

    async def install(self, path: str | Path) -> str:
        """把用户选的图归一拷进数据目录并落偏好，返回落点路径。

        **是协程**（与本机字体 ``add_font_file`` 同一惯例）：读原图 + 解码 + 缩放是
        几百毫秒到几秒的重活，控制器那边是界面循环，同步方法会直接把窗口冻在那里。

        顺序是**先写新图、再落键、最后删旧图**：落键之后界面已经指向新文件，这时删
        旧的才不会闪出一道"背景既没旧的也没新的"的空窗。
        """
        src = Path(str(path))
        if not src.is_file():
            raise FileNotFoundError(f"图片文件不存在：{src}")
        dest_dir = self._dir()
        dest = self._normalize(src, dest_dir)
        previous = self._raw_value()
        self._write_key(str(dest))
        self._prune(previous, dest)
        return str(dest)

    async def clear(self) -> None:
        """去掉背景：落空串 + 删掉我们自己目录里的那份（不碰目录外的文件）。"""
        raw = self._raw_value()
        self._write_key("")
        self._prune(raw, None)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _dir(self) -> Path:
        """背景图落点：数据目录 backgrounds/（与用户字体同侧——缓存目录是可回收的）。"""
        base = (self._storage.paths.data if self._storage is not None else None)
        if base is None:
            from app.services.app_paths import default_app_paths
            base = default_app_paths().data
        path = Path(base) / "backgrounds"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _normalize(src: Path, dest_dir: Path) -> Path:
        """长边压到 :data:`_MAX_EDGE` 的 JPEG；没装 Pillow 就原样拷贝同名文件。"""
        raw = src.read_bytes()
        digest = hashlib.md5(raw).hexdigest()[:16]
        try:
            from PIL import Image
        except ImportError:
            logger.debug("Pillow 不可用，背景图原样拷贝（未压缩）")
            dest = dest_dir / f"{digest}{src.suffix.lower() or '.png'}"
            dest.write_bytes(raw)
            return dest
        import io

        with Image.open(io.BytesIO(raw)) as im:
            # 背景层不需要 alpha：压暗层盖在它上面，带透明通道的图反而多一倍解码量。
            im = im.convert("RGB")
            im.thumbnail((_MAX_EDGE, _MAX_EDGE))
            dest = dest_dir / f"{digest}.jpg"
            im.save(dest, format="JPEG", quality=_JPEG_QUALITY)
        return dest

    def _prune(self, previous, keep: Path | None) -> None:
        """删掉被换下来的那份：只删**我们目录里**的路径，别碰用户自己的文件。"""
        if not previous:
            return
        if keep is not None and str(previous) == str(keep):
            return
        old = Path(str(previous))
        try:
            old.relative_to(self._dir())
        except (ValueError, OSError):
            return
        try:
            old.unlink()
        except OSError:
            logger.debug("旧背景图没删掉：%s", old, exc_info=True)

    def _raw_value(self):
        if self._settings is None:
            return ""
        try:
            return self._settings.get(BACKGROUND_IMAGE_KEY, "")
        except Exception:
            logger.debug("读背景图偏好失败", exc_info=True)
            return ""

    def _write_key(self, value: str) -> None:
        if self._settings is None:
            return
        self._settings.set(BACKGROUND_IMAGE_KEY, value)


__all__ = ["BackgroundService", "BACKGROUND_IMAGE_KEY"]
