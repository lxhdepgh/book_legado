"""Run reading engines on a loop of their own, so rule work cannot freeze the UI.

The Flet event loop serves clicks, scrolling and painting, and the reader used
to run its engines on that very loop: loading a 3000-chapter catalog evaluates
its TOC rules synchronously for ~1.2s, and the window stops answering for that
whole time.  Engine coroutines now run on a dedicated loop on a daemon thread
and the UI just awaits a future.

Only the Python half moves.  The QuickJS runtime already runs on its own thread
with a foreground/background priority queue (`legado_js.QuickJsDispatcher`), so
`@js:` rules keep their scheduling; HTML parsing and rule evaluation in Python
were what held the loop.

The engine object itself is shared with the UI: attribute reads and writes
(`current_book`, `js_variables`, `js`) keep working exactly as they did when
both halves lived on one loop, and only coroutine methods are dispatched.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import threading


class EngineHub:
    """An event loop on a daemon thread, shared by the process's engines."""

    def __init__(self, name: str = "legado-engines") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()

    def loop(self) -> asyncio.AbstractEventLoop:
        """The hub loop, starting its thread on first use."""
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._serve, name=self._name, daemon=True)
                self._thread.start()
        self._ready.wait()
        return self._loop

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    def submit(self, coroutine) -> concurrent.futures.Future:
        """Schedule a coroutine here; safe to call from any thread."""
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop())


_HUB = EngineHub()


def engine_hub() -> EngineHub:
    """The process-wide hub the views put their engines on."""
    return _HUB


class EngineProxy:
    """The UI-facing handle of an engine that runs on an :class:`EngineHub`.

    Coroutine methods are dispatched to the hub and awaited from the caller's
    loop; everything else reaches the shared engine object unchanged, so the
    call sites keep reading `engine.current_book` and `engine.js_variables` as
    if the engine were still local.
    """

    def __init__(self, hub: EngineHub, engine) -> None:
        object.__setattr__(self, "_hub", hub)
        object.__setattr__(self, "_engine", engine)

    def __getattr__(self, name):
        engine = self._engine
        value = getattr(engine, name)
        # An instance attribute is a stand-in someone put there (a test double,
        # say); it stays where it is instead of being marshalled to the hub.
        if name in getattr(engine, "__dict__", ()):
            return value
        return self._dispatch(value) if inspect.iscoroutinefunction(value) else value

    def __setattr__(self, name, value) -> None:
        setattr(self._engine, name, value)

    def _dispatch(self, method):
        async def call(*args, **kwargs):
            engine = self._engine
            if getattr(engine, "interaction_loop", None) is None:
                # WebView 与验证码回调要碰界面控件，由它们所在的循环执行。
                engine.interaction_loop = asyncio.get_running_loop()
            return await asyncio.wrap_future(
                self._hub.submit(method(*args, **kwargs)))
        return call


def worker_engine(engine, hub: EngineHub | None = None) -> EngineProxy:
    """Put an already-built engine on the hub and return its UI-side handle."""
    return EngineProxy(hub or engine_hub(), engine)
