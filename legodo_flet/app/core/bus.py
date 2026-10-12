"""EventBus：进程内 typed 事件总线（§4.3 / §9.2）。

语义：

- :meth:`publish` **不阻塞调用方**——把每个处理器作为协程任务提交到
  EngineHub 的执行域 loop 上（§3.6 统一执行域），调用方拿到 future 列表可等待也可忽略；
- :meth:`subscribe` 按事件类型注册 async 处理器，返回退订句柄（可调用，重复退订幂等）；
- 退订安全：退订后不再投递；投递期间退订不影响本轮已快照的处理器集合；
- 单个处理器异常只记日志，不拖垮总线、不中断其他订阅者。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import threading
from collections import defaultdict
from typing import Callable

from app.core.engine_worker import EngineHub, engine_hub
from app.core.events import AppEvent
from app.core.lifecycle import Service

logger = logging.getLogger(__name__)

# 订阅处理器：async callable(event) -> None
Handler = Callable[[AppEvent], "asyncio.Future | None"]


class EventBus(Service):
    """进程内发布-订阅总线，服务解耦的唯一通知通道（§4.2 基础设施表）。"""

    name = "EventBus"
    dependencies: tuple[type[Service], ...] = (EngineHub,)

    def __init__(self, engine=None) -> None:
        super().__init__()
        self._engine: EngineHub = engine if engine is not None else engine_hub()
        self._handlers: dict[type[AppEvent], set[Handler]] = defaultdict(set)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 发布 / 订阅
    # ------------------------------------------------------------------ #
    def publish(self, event: AppEvent) -> list[concurrent.futures.Future]:
        """异步投递事件，立即返回每个处理器对应的 future 列表（不阻塞调用方）。"""
        with self._lock:
            handlers = list(self._handlers.get(type(event), ()))
        futures: list[concurrent.futures.Future] = []
        for handler in handlers:
            futures.append(self._engine.submit(self._invoke(handler, event)))
        return futures

    def subscribe(self, event_type: type[AppEvent], handler: Handler) -> Callable[[], None]:
        """订阅某类事件，返回退订句柄；重复退订安全。"""
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(
                f"EventBus 只接受 async 处理器，收到 {handler!r}；"
                "同步副作用请在服务内完成，不要经总线。")
        with self._lock:
            self._handlers[event_type].add(handler)

        def unsubscribe() -> None:
            with self._lock:
                self._handlers.get(event_type, set()).discard(handler)

        return unsubscribe

    # ------------------------------------------------------------------ #
    async def _invoke(self, handler: Handler, event: AppEvent) -> None:
        """单个处理器的隔离执行：异常记日志，绝不抛出到总线循环。"""
        try:
            await handler(event)
        except Exception:
            logger.exception(
                "EventBus 处理器 %s 处理 %s 失败", handler, type(event).__name__)
