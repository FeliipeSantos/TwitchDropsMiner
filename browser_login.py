"""User-controlled Twitch login in an isolated, persistent Chrome/Edge profile.

Only this browser instance and Twitch's own requests are observed. Passwords,
personal browser profiles, and challenge responses are never read or automated.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path
from time import monotonic
from typing import Any, Callable
from urllib.parse import urlsplit

import aiohttp

from exceptions import LoginException

TWITCH_URL = "https://www.twitch.tv/drops/inventory"
GQL_URL = "https://gql.twitch.tv/gql"
WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
logger = logging.getLogger("TwitchDrops.browser")
CAPTURE_HEADERS = {
    "authorization", "client-id", "client-integrity", "client-session-id",
    "client-version", "x-device-id", "content-type",
}


def find_browser() -> str:
    candidates = []
    for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("LOCALAPPDATA")):
        if root:
            candidates.extend([
                Path(root, "Google/Chrome/Application/chrome.exe"),
                Path(root, "Microsoft/Edge/Application/msedge.exe"),
            ])
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        if executable := shutil.which(name):
            return executable
    raise LoginException("Install Google Chrome or Microsoft Edge to sign in to Twitch.")


class BrowserLogin:
    def __init__(self, profile: Path, proxy: str = "", notify: Callable[[str], None] | None = None):
        self.profile = profile.resolve()
        self.proxy = proxy
        self.notify = notify or (lambda message: None)
        self.process: asyncio.subprocess.Process | None = None
        self.http: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.reader: asyncio.Task | None = None
        self.session_id = ""
        self.headers: dict[str, str] = {}
        self.captured_at = 0.0
        self._sequence = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._refresh_lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()
        self._token: str | None = None
        self._device_id: str | None = None
        self._authenticated = False
        self._gql_inflight = 0

    async def _launch(self, *, debugging: bool, minimized: bool, url: str) -> None:
        executable = find_browser()
        self.profile.mkdir(parents=True, exist_ok=True)
        args = [executable, f"--user-data-dir={self.profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-background-mode", url]
        if debugging:
            args[-1:-1] = ["--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"]
        if self.proxy:
            parsed = urlsplit(self.proxy)
            if parsed.username or parsed.password:
                raise LoginException("Browser login requires a proxy without username/password authentication.")
            args.insert(-1, f"--proxy-server={self.proxy}")
        options: dict[str, Any] = {}
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 7 if minimized else 1
            options["startupinfo"] = startup
        self.process = await asyncio.create_subprocess_exec(
            *args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options
        )

    async def manual_login(self) -> None:
        # The user signs in with an ordinary browser: no CDP connection or
        # debugging switches exist during authentication. Only after they close
        # this instance do we reopen its dedicated profile to restore the session.
        await self.close()
        await self._launch(debugging=False, minimized=False, url="https://www.twitch.tv/login")
        try:
            await asyncio.wait_for(self.process.wait(), 900)
        except asyncio.TimeoutError as exc:
            raise LoginException("After signing in to Twitch, close all windows of the browser opened by the miner to continue. Keep the miner open.") from exc

    async def start(self, *, minimized: bool = False) -> None:
        if self.ws is not None and not self.ws.closed:
            return
        await self.close()
        port_file = self.profile / "DevToolsActivePort"
        port_file.unlink(missing_ok=True)
        await self._launch(debugging=True, minimized=minimized, url="about:blank")
        try:
            for _ in range(100):
                if self.process.returncode is not None:
                    raise LoginException("The browser did not start. Close the previous miner instance and try again.")
                try:
                    lines = port_file.read_text().splitlines()
                    port = int(lines[0])
                    endpoint = lines[1]
                    if not 0 < port < 65536 or not endpoint.startswith("/devtools/browser/"):
                        raise ValueError("Invalid debugging endpoint")
                    break
                except (OSError, ValueError, IndexError):
                    await asyncio.sleep(0.2)
            else:
                raise LoginException("Unable to connect to the browser. Restart the miner and try again.")
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False)
            self.ws = await self.http.ws_connect(f"ws://127.0.0.1:{port}{endpoint}")
            self.reader = asyncio.create_task(self._read())
            target = await self.command("Target.createTarget", {"url": "about:blank"}, browser=True)
            attached = await self.command("Target.attachToTarget", {
                "targetId": target["targetId"], "flatten": True,
            }, browser=True)
            self.session_id = attached["sessionId"]
            await self.command("Network.enable")
            await self.command("Page.enable")
        except BaseException:
            await self.close()
            raise

    async def _read(self) -> None:
        assert self.ws is not None
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                if "id" in data:
                    future = self._pending.get(data["id"])
                    if future is not None and not future.done():
                        if "error" in data:
                            future.set_exception(LoginException("The browser could not execute the requested operation."))
                        else:
                            future.set_result(data.get("result", {}))
                elif (data.get("sessionId") == self.session_id
                      and data.get("method") == "Network.requestWillBeSent"):
                    request = data["params"]["request"]
                    if request.get("url") == GQL_URL:
                        headers = {k.lower(): str(v) for k, v in request.get("headers", {}).items()}
                        if (headers.get("client-id") == WEB_CLIENT_ID
                                and headers.get("client-integrity")
                                and self._gql_inflight == 0):
                            self.headers = {k: v for k, v in headers.items() if k in CAPTURE_HEADERS}
                            self.captured_at = monotonic()
        except Exception:
            logger.exception("Browser connection reader failed")
            raise
        finally:
            logger.info("Browser connection closed")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(LoginException("The login browser was closed. Restart the miner and keep its browser open or minimized."))

    async def command(self, method: str, params: dict | None = None, *, browser: bool = False) -> dict:
        if self.ws is None or self.ws.closed:
            raise LoginException("The Twitch browser was closed. Restart the miner.")
        self._sequence += 1
        sequence = self._sequence
        future = asyncio.get_running_loop().create_future()
        self._pending[sequence] = future
        payload: dict[str, Any] = {"id": sequence, "method": method, "params": params or {}}
        if not browser:
            payload["sessionId"] = self.session_id
        try:
            await self.ws.send_json(payload)
            return await asyncio.wait_for(future, 45)
        except asyncio.TimeoutError as exc:
            raise LoginException("The browser did not respond. Check the Twitch window and try again.") from exc
        finally:
            self._pending.pop(sequence, None)

    async def evaluate(self, expression: str) -> Any:
        result = await self.command("Runtime.evaluate", {
            "expression": expression, "awaitPromise": True, "returnByValue": True,
        })
        if "exceptionDetails" in result:
            raise LoginException("The Twitch page did not respond to the request. Check your connection and reload the miner.")
        return result.get("result", {}).get("value")

    async def cookies(self) -> dict[str, str]:
        result = await self.command("Network.getCookies", {"urls": [TWITCH_URL]})
        return {c["name"]: c["value"] for c in result["cookies"]}

    async def login(self, *, token: str | None = None, device_id: str | None = None,
                    forget: bool = False) -> dict[str, str]:
        if forget:
            await self.start(minimized=True)
            await self.command("Network.clearBrowserCookies")
            await self.close()
            self._token = self._device_id = None
        if token is None:
            await self.manual_login()
            self.notify("Browser closed. Confirming your session in a new minimized window. Keep this window open or minimized while using the miner.")
            await asyncio.sleep(3)
        await self.start(minimized=True)
        if token:
            # Restore only the miner's own saved WEB session in its dedicated profile.
            values = [{"name": "auth-token", "value": token, "domain": ".twitch.tv",
                       "path": "/", "secure": True}]
            if device_id:
                values.append({"name": "unique_id", "value": device_id,
                               "domain": ".twitch.tv", "path": "/", "secure": True})
            await self.command("Network.setCookies", {"cookies": values})
        elif not (await self.cookies()).get("auth-token"):
            raise LoginException("The browser was closed before login completed. Sign in to Twitch before closing its windows; keep the miner open.")
        self.headers.clear()
        await self.command("Page.navigate", {"url": TWITCH_URL})
        last_token = ""
        for _ in range(90):
            cookie = await self.cookies()
            current_token = cookie.get("auth-token", "")
            if current_token and self.headers.get("authorization", "").split(" ", 1)[-1] == current_token:
                if self.headers.get("x-device-id"):
                    self._authenticated = True
                    self._token = current_token
                    self._device_id = self.headers["x-device-id"]
                    ua = await self.evaluate("navigator.userAgent")
                    return {"token": current_token, "device_id": self.headers["x-device-id"],
                            "user_agent": ua, "session_id": self.headers.get("client-session-id", "")}
            if current_token and current_token != last_token:
                last_token = current_token
                await self.command("Page.navigate", {"url": TWITCH_URL})
            await asyncio.sleep(1)
        raise LoginException("The browser session was not recognized. Check your Twitch login and try again.")

    async def refresh(self) -> None:
        async with self._refresh_lock:
            before = self.captured_at
            await self.command("Page.reload", {"ignoreCache": True})
            for _ in range(60):
                cookie = await self.cookies()
                if not cookie.get("auth-token"):
                    raise LoginException("Your Twitch session expired. Use Help -> invalidate session and sign in again.")
                if (self.captured_at > before and self.headers.get("authorization", "").split(" ", 1)[-1]
                        == cookie["auth-token"]):
                    return
                await asyncio.sleep(1)
            raise LoginException("Twitch did not refresh the browser session. Check the Twitch page and try again.")

    def connected(self) -> bool:
        return (self.ws is not None and not self.ws.closed and self.reader is not None
                and not self.reader.done())

    async def reconnect(self) -> None:
        async with self._reconnect_lock:
            if self.connected():
                return
            if self._token is None:
                raise LoginException("Sign in to Twitch before loading campaigns.")
            self.notify("Reopening the miner browser with your saved session. Keep it open or minimized.")
            await self.login(token=self._token, device_id=self._device_id)

    async def gql(self, operations: Any, *, read_only: bool = False) -> Any:
        if self._token is not None and not self.connected():
            await self.reconnect()
        if not self._authenticated:
            raise LoginException("Sign in to Twitch before loading campaigns.")
        if monotonic() - self.captured_at > 600:
            await self.refresh()
        headers = dict(self.headers)
        headers.setdefault("content-type", "text/plain;charset=UTF-8")
        # Browser supplies User-Agent, Origin and Referer itself. Cross-origin GQL
        # uses the captured OAuth header, just like the Twitch web application.
        expression = """(async () => {
            const response = await fetch('https://gql.twitch.tv/gql', {
                method: 'POST', credentials: 'same-origin', headers: HEADERS, body: BODY
            });
            return {status: response.status, body: await response.json()};
        })()""".replace("HEADERS", json.dumps(headers)).replace("BODY", json.dumps(json.dumps(operations)))
        self._gql_inflight += 1
        try:
            try:
                result = await self.evaluate(expression)
            finally:
                self._gql_inflight -= 1
        except LoginException:
            if not read_only or self.connected():
                raise
            await self.reconnect()
            return await self.gql(operations, read_only=False)
        if not isinstance(result, dict) or "body" not in result:
            raise LoginException("Unexpected Twitch response in the browser.")
        if result["status"] == 401:
            raise LoginException("Your Twitch session expired. Invalidate the session in the Help tab and sign in again.")
        if result["status"] >= 500:
            raise LoginException("Twitch is unavailable. Please try again later.")
        return result["body"]

    async def close(self) -> None:
        if self.ws is not None and not self.ws.closed:
            with suppress(Exception):
                if self.reader is not None and not self.reader.done():
                    await self.command("Browser.close", browser=True)
            await self.ws.close()
        if self.reader is not None:
            self.reader.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.reader
        if self.http is not None:
            await self.http.close()
        if self.process is not None and self.process.returncode is None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.process.wait(), 5)
            if self.process.returncode is None:
                self.process.terminate()
                await self.process.wait()
        self.ws = self.http = self.reader = self.process = None
        self.session_id = ""
        self.headers.clear()
        self._authenticated = False
