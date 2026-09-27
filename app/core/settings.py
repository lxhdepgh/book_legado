"""SettingsService：全局偏好统一读写（§4.2 / §6.3 settings 表 / §9.2）。

- ``get``/``set`` 均为**同步**接口：内存缓存优先，写库 + 缓存 + 发布事件一气呵成；
- 值以 JSON 文本落 ``settings.value`` 列（§6.3 value(JSON)）；
- 每次 set 经 EventBus 发 :class:`~app.core.events.SettingsChangedEvent`，
  同时同步触发按键订阅的处理器（协程派发到 EngineHub）；
- 模块级访问器在"无服务"环境下回退：``get_setting`` 返回默认值，
  ``set_setting`` 只记内存——保证引擎模块独立可跑。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Iterable

from app.core.events import SettingsChangedEvent
from app.core.lifecycle import Service
from app.core.storage import StorageService

logger = logging.getLogger(__name__)

# 键订阅处理器：async callable(SettingsChangedEvent) -> None
KeyHandler = Callable[[SettingsChangedEvent], "object"]


class SettingsService(Service):
    """外观/UA/超时/开关等全局偏好的统一落点（吸收 appearance.json 等散落配置）。"""

    name = "SettingsService"
    dependencies: tuple[type[Service], ...] = (StorageService,)

    def __init__(self, storage: StorageService | None = None, engine=None) -> None:
        super().__init__()
        self._storage = storage
        self._cache: dict[str, Any] = {}
        self._subs: list[tuple[frozenset[str], KeyHandler]] = []
        if engine is None:
            from app.core.engine_worker import engine_hub
            engine = engine_hub()
        self._engine = engine

    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        # 构造时未注入 storage 的，从 hub 依赖面取已注册的 StorageService
        if self._storage is None and self.hub is not None:
            self._storage = self.hub.require(StorageService)
        conn = self._storage.db if self._storage is not None else None
        if conn is not None:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            self._cache = {r[0]: json.loads(r[1]) for r in rows}
        _bind(self)

    async def on_stop(self) -> None:
        _unbind(self)
        # B6：停止后清空键订阅列表，避免残留 handler 持有已释放视图/服务的闭包。
        self._subs.clear()

    # ------------------------------------------------------------------ #
    # 同步读写面
    # ------------------------------------------------------------------ #
    def get(self, key: str, default: Any = None) -> Any:
        """内存缓存优先取值；未命中返回 default（无服务环境由模块访问器兜底）。"""
        return self._cache.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """写内存 + 写库 + 广播 SettingsChangedEvent + 触发键订阅。"""
        old = self._cache.get(key)
        self._cache[key] = value
        conn = self._storage.db if self._storage is not None else None
        if conn is not None:
            payload = json.dumps(value, ensure_ascii=False)
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, payload))
        if self.bus is not None:
            self.bus.publish(SettingsChangedEvent(key=key, value=value, old_value=old))
        for keys, handler in self._subs:
            if key in keys:
                self._engine.submit(self._invoke(handler, key, value, old))

    def subscribe(self, keys: Iterable[str], handler: KeyHandler) -> Callable[[], None]:
        """订阅指定键的变更，返回退订句柄。"""
        frozen = frozenset(keys)
        self._subs.append((frozen, handler))

        def unsubscribe() -> None:
            self._subs[:] = [s for s in self._subs if s[0] is not frozen]

        return unsubscribe

    @staticmethod
    async def _invoke(handler: KeyHandler, key: str, value: Any, old: Any) -> None:
        try:
            await handler(SettingsChangedEvent(key=key, value=value, old_value=old))
        except Exception:
            logger.exception("SettingsService 键订阅处理器 %s 失败", handler)


# ---------------------------------------------------------------------- #
# 模块级访问器：无服务环境下引擎模块可独立运行
# ---------------------------------------------------------------------- #
_SETTINGS: SettingsService | None = None
_MEMORY: dict[str, Any] = {}


def default_settings() -> SettingsService | None:
    """当前已启动的 SettingsService；未启动返回 None。"""
    return _SETTINGS


def get_setting(key: str, default: Any = None) -> Any:
    """取偏好；服务未启动时只查模块内存回退字典，再无则返回 default。"""
    if _SETTINGS is not None:
        return _SETTINGS.get(key, default)
    return _MEMORY.get(key, default)


def set_setting(key: str, value: Any) -> None:
    """写偏好；服务未启动时仅记内存（不写库、不发事件）。"""
    if _SETTINGS is not None:
        _SETTINGS.set(key, value)
    else:
        _MEMORY[key] = value


def _bind(settings: SettingsService) -> None:
    global _SETTINGS
    _SETTINGS = settings


def _unbind(settings: SettingsService) -> None:
    global _SETTINGS
    if _SETTINGS is settings:
        _SETTINGS = None
