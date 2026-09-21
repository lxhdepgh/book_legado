"""Interactive Edge/CDP adapter used where Flet WebView is unavailable."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess

import aiohttp


class DesktopBrowserError(RuntimeError):
    pass


class EdgeCdpSession:
    def __init__(self, profile_dir: str | Path):
        self.profile_dir = Path(profile_dir)
        self.process = None
        self.port = 0
        self.target_ws = ""
        self.initial_url = ""

    @staticmethod
    def find_edge() -> Path | None:
        candidates = []
        for env_name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            root = os.environ.get(env_name)
            if root:
                candidates.append(Path(root) / "Microsoft/Edge/Application/msedge.exe")
        return next((path for path in candidates if path.exists()), None)

    @staticmethod
    def cookie_header(cookies: list[dict], url: str) -> str:
        from urllib.parse import urlsplit
        host = (urlsplit(url).hostname or "").lower()
        values = []
        for item in cookies or []:
            domain = str(item.get("domain") or "").lstrip(".").lower()
            if domain and (host == domain or host.endswith("." + domain)):
                values.append(f"{item.get('name', '')}={item.get('value', '')}")
        return "; ".join(values)

    async def start(self, url: str) -> None:
        self.initial_url = str(url)
        edge = self.find_edge()
        if edge is None:
            raise DesktopBrowserError("未找到 Microsoft Edge，无法打开网页验证")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.process = subprocess.Popen(
            [str(edge), f"--remote-debugging-port={self.port}",
             "--remote-allow-origins=http://127.0.0.1",
             f"--user-data-dir={self.profile_dir}", "--new-window", str(url)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags,
        )
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            try:
                await self._select_target(url)
                if self.target_ws:
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                pass
            if self.process.poll() is not None:
                break
            await asyncio.sleep(0.2)
        await self.close()
        raise DesktopBrowserError("Edge 网页验证窗口启动失败")

    async def _select_target(self, preferred_url="") -> None:
        timeout = aiohttp.ClientTimeout(total=2)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{self.port}/json/list") as response:
                targets = await response.json()
        pages = [item for item in targets if item.get("type") == "page" and
                 item.get("webSocketDebuggerUrl")]
        if not pages:
            raise DesktopBrowserError("Edge 中没有可读取的网页")
        preferred = next((item for item in pages if preferred_url and
                          str(item.get("url", "")).startswith(preferred_url)), pages[0])
        self.target_ws = preferred["webSocketDebuggerUrl"]

    async def _command(self, ws, command_id: int, method: str, params=None):
        await ws.send_json({"id": command_id, "method": method,
                            "params": params or {}})
        while True:
            message = await ws.receive(timeout=10)
            if message.type != aiohttp.WSMsgType.TEXT:
                raise DesktopBrowserError("Edge 调试连接已断开")
            payload = json.loads(message.data)
            if payload.get("id") == command_id:
                if payload.get("error"):
                    raise DesktopBrowserError(str(payload["error"]))
                return payload.get("result") or {}

    async def capture(self, script: str = "", delay_ms: int = 0,
                      html: str = "", source_regex: str = "",
                      override_regex: str = "") -> dict:
        await self._select_target()
        script_value = None
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.target_ws, max_msg_size=64 * 1024 * 1024) as ws:
                command_id = 1
                if html:
                    await self._command(ws, command_id, "Runtime.evaluate", {
                        "expression": (
                            "document.open();document.write(" +
                            json.dumps(str(html), ensure_ascii=False) +
                            ");document.close()"),
                        "awaitPromise": True, "returnByValue": True})
                    command_id += 1
                if script:
                    evaluated = await self._command(ws, command_id, "Runtime.evaluate", {
                        "expression": str(script), "awaitPromise": True,
                        "returnByValue": True})
                    script_value = (evaluated.get("result") or {}).get("value")
                    command_id += 1
                    if delay_ms:
                        await asyncio.sleep(max(0, delay_ms) / 1000)
                # Re-read the value after any later navigation cleared it; the
                # webjs rule protocol stores it on window.__legadoJsResult.
                js_result = script_value
                readback = await self._command(ws, command_id, "Runtime.evaluate", {
                    "expression": ("window.__legadoJsResult===undefined"
                                   "?null:window.__legadoJsResult"),
                    "returnByValue": True})
                command_id += 1
                readback_value = (readback.get("result") or {}).get("value")
                if readback_value is not None:
                    js_result = readback_value
                value = await self._command(ws, command_id, "Runtime.evaluate", {
                    "expression": ("({html:document.documentElement.outerHTML,url:location.href,"
                                   "resources:performance.getEntriesByType('resource').map(e=>e.name)})"),
                    "returnByValue": True,
                })
                cookies = await self._command(ws, command_id + 1, "Network.getAllCookies")
        page = ((value.get("result") or {}).get("value") or {})
        final_url = str(page.get("url") or "")
        result = {
            "body": str(page.get("html") or ""), "headers": {"content-type": "text/html"},
            "status": 200, "url": final_url,
            "cookie": self.cookie_header(cookies.get("cookies") or [], final_url),
        }
        if script:
            result["jsResult"] = js_result
        from app.services.java_regex import compile_pattern
        if source_regex:
            pattern = compile_pattern(source_regex)
            matched = next((str(item) for item in page.get("resources") or []
                            if pattern.fullmatch(str(item))), "")
            if matched:
                result.update(body=matched, source=matched,
                              url=self.initial_url or final_url)
        if override_regex and compile_pattern(override_regex).fullmatch(final_url):
            result.update(body=final_url, overrideUrl=final_url,
                          url=self.initial_url or final_url)
        return result

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 3)
            except subprocess.TimeoutExpired:
                process.kill()
