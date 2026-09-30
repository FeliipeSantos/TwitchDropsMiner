import asyncio
import json
from contextlib import asynccontextmanager
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

import twitch
import browser_login
from browser_login import BrowserLogin, GQL_URL, WEB_CLIENT_ID
from constants import ClientType
from exceptions import LoginException


@pytest.mark.asyncio
async def test_device_rejection_has_useful_error_instead_of_keyerror():
    response = SimpleNamespace(status=400, json=AsyncMock(return_value={"status": 400, "message": "invalid client"}))
    @asynccontextmanager
    async def request(*args, **kwargs):
        yield response
    client = SimpleNamespace(gui=SimpleNamespace(login=Mock()), _client_type=ClientType.ANDROID_APP,
                             request=request)
    state = twitch._AuthState(client)
    state.device_id = "device"
    with pytest.raises(LoginException, match="rejected device-code"):
        await state._oauth_login()


def fake_client(tmp_path, monkeypatch, responses, cookie=None):
    jar = aiohttp.CookieJar()
    if cookie:
        jar.update_cookies(cookie, ClientType.WEB.CLIENT_URL)
    session = SimpleNamespace(cookie_jar=jar, headers={})
    @asynccontextmanager
    async def request(*args, **kwargs):
        status, body = responses.pop(0)
        yield SimpleNamespace(status=status, json=AsyncMock(return_value=body))
    async def coro_unless_closed(coro):
        return await coro
    login = SimpleNamespace(update=Mock(), wait_for_login_press=AsyncMock())
    gui = SimpleNamespace(login=login, grab_attention=Mock(), coro_unless_closed=coro_unless_closed,
                          help=SimpleNamespace(_invalidate_button=Mock()))
    browser = SimpleNamespace(login=AsyncMock(return_value={
        "token": "web-token", "device_id": "browser-device", "session_id": "browser-session",
        "user_agent": "Chrome-Test",
    }))
    client = SimpleNamespace(get_session=AsyncMock(return_value=session), request=request,
                             _browser=browser, _browser_forget=False, _client_type=ClientType.ANDROID_APP,
                             gui=gui, print=Mock())
    monkeypatch.setattr(twitch, "COOKIES_PATH", tmp_path / "cookies.jar")
    return client, twitch._AuthState(client), jar


@pytest.mark.asyncio
async def test_fresh_login_uses_browser_and_saves_web_session(tmp_path, monkeypatch):
    client, state, jar = fake_client(tmp_path, monkeypatch, [
        (200, {"client_id": WEB_CLIENT_ID, "user_id": "42"}),
    ])
    await state.validate()
    assert state.user_id == 42
    assert state.device_id == "browser-device"
    assert client._client_type.CLIENT_ID == WEB_CLIENT_ID
    assert jar.filter_cookies(ClientType.WEB.CLIENT_URL)["auth-token"].value == "web-token"
    client.gui.login.wait_for_login_press.assert_awaited_once()
    assert state._logged_in.is_set()


@pytest.mark.asyncio
async def test_existing_android_session_is_preserved(tmp_path, monkeypatch):
    client, state, jar = fake_client(tmp_path, monkeypatch, [
        (200, {"client_id": ClientType.ANDROID_APP.CLIENT_ID, "user_id": "23"}),
    ], {"auth-token": "old-token", "unique_id": "old-device"})
    await state.validate()
    assert state.access_token == "old-token"
    assert state.device_id == "old-device"
    client._browser.login.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_session_is_restored_in_own_browser(tmp_path, monkeypatch):
    client, state, jar = fake_client(tmp_path, monkeypatch, [
        (200, {"client_id": WEB_CLIENT_ID, "user_id": "42"}),
        (200, {"client_id": WEB_CLIENT_ID, "user_id": "42"}),
    ], {"auth-token": "web-token", "unique_id": "browser-device"})
    await state.validate()
    client._browser.login.assert_awaited_once_with(token="web-token", device_id="browser-device", forget=False)
    client.gui.login.wait_for_login_press.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_session_requests_new_login(tmp_path, monkeypatch):
    client, state, jar = fake_client(tmp_path, monkeypatch, [
        (401, {}), (200, {"client_id": WEB_CLIENT_ID, "user_id": "42"}),
    ], {"auth-token": "expired"})
    await state.validate()
    client._browser.login.assert_awaited_once_with(token=None, device_id=None, forget=True)
    client.gui.login.wait_for_login_press.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_client_cookie_is_not_deleted(tmp_path, monkeypatch):
    client, state, jar = fake_client(tmp_path, monkeypatch, [
        (200, {"client_id": "unknown-client", "user_id": "42"}),
    ], {"auth-token": "other-token"})
    with pytest.raises(LoginException, match="another Twitch client"):
        await state.validate()
    assert jar.filter_cookies(ClientType.WEB.CLIENT_URL)["auth-token"].value == "other-token"
    client._browser.login.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_failure_does_not_replace_saved_cookie(tmp_path, monkeypatch):
    client, state, jar = fake_client(tmp_path, monkeypatch, [(503, {})], {"auth-token": "keep-token"})
    with pytest.raises(LoginException, match="validate"):
        await state.validate()
    client._browser.login.assert_not_awaited()
    assert jar.filter_cookies(ClientType.WEB.CLIENT_URL)["auth-token"].value == "keep-token"


@pytest.mark.asyncio
async def test_browser_gql_keeps_browser_headers_and_does_not_retry_mutation(tmp_path):
    browser = BrowserLogin(tmp_path)
    browser._authenticated = True
    browser.headers = {"client-id": WEB_CLIENT_ID, "authorization": "OAuth example",
                       "client-integrity": "example-integrity", "x-device-id": "example-device"}
    browser.captured_at = monotonic()
    browser.evaluate = AsyncMock(return_value={"status": 200, "body": {"data": {"claimDropRewards": {"status": "OK"}}}})
    ops = {"operationName": "DropsPage_ClaimDropRewards", "variables": {"id": 'quote"test'}}
    response = await browser.gql(ops)
    assert response["data"]["claimDropRewards"]["status"] == "OK"
    browser.evaluate.assert_awaited_once()
    expression = browser.evaluate.call_args.args[0]
    assert "example-integrity" in expression
    assert "example-device" in expression
    assert "credentials: 'same-origin'" in expression
    assert browser._gql_inflight == 0


@pytest.mark.asyncio
async def test_browser_exception_does_not_retry_mutation(tmp_path):
    browser = BrowserLogin(tmp_path)
    browser._authenticated = True
    browser.captured_at = monotonic()
    browser.evaluate = AsyncMock(side_effect=LoginException("closed"))
    with pytest.raises(LoginException, match="closed"):
        await browser.gql({"operationName": "DropsPage_ClaimDropRewards"})
    browser.evaluate.assert_awaited_once()
    assert browser._gql_inflight == 0


@pytest.mark.asyncio
async def test_closed_browser_reports_login_error(tmp_path):
    browser = BrowserLogin(tmp_path)
    with pytest.raises(LoginException, match="closed"):
        await browser.command("Network.getCookies")


@pytest.mark.asyncio
async def test_browser_capture_restricted_to_twitch_and_own_tab(tmp_path):
    browser = BrowserLogin(tmp_path)
    browser.session_id = "own-tab"
    class Messages:
        closed = False
        def __aiter__(self):
            return self.messages()
        async def messages(self):
            for url, session in [("https://example.org/gql", "own-tab"), (GQL_URL, "other-tab"), (GQL_URL, "own-tab")]:
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps({
                    "sessionId": session, "method": "Network.requestWillBeSent",
                    "params": {"request": {"url": url, "headers": {
                        "Client-Id": WEB_CLIENT_ID, "Client-Integrity": "twitch-header",
                        "Authorization": "OAuth example", "Password": "must-not-capture",
                    }}},
                }))
    browser.ws = Messages()
    await browser._read()
    assert browser.headers["client-integrity"] == "twitch-header"
    assert "password" not in browser.headers


@pytest.mark.asyncio
async def test_manual_browser_launch_has_no_debugging_switches(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_login, "find_browser", lambda: "chrome.exe")
    spawn = AsyncMock(return_value=SimpleNamespace(wait=AsyncMock(return_value=0)))
    monkeypatch.setattr(browser_login.asyncio, "create_subprocess_exec", spawn)
    browser = BrowserLogin(tmp_path)
    await browser._launch(debugging=False, minimized=False, url="https://www.twitch.tv/login")
    args = spawn.call_args.args
    assert not any("remote-debugging" in value for value in args)
    assert not any("enable-automation" in value for value in args)
    assert args[-1] == "https://www.twitch.tv/login"
    assert any(str(tmp_path) in value for value in args)


@pytest.mark.asyncio
async def test_fresh_browser_login_waits_for_manual_phase_before_connecting(tmp_path):
    browser = BrowserLogin(tmp_path)
    calls = []
    async def manual():
        calls.append("manual")
    async def start(**kwargs):
        calls.append("connect")
    async def command(method, params=None):
        if method == "Page.navigate":
            browser.headers = {"authorization": "OAuth web-token", "x-device-id": "device"}
        return {}
    browser.manual_login = manual
    browser.start = start
    browser.command = command
    browser.cookies = AsyncMock(return_value={"auth-token": "web-token"})
    browser.evaluate = AsyncMock(return_value="Chrome")
    result = await browser.login()
    assert calls == ["manual", "connect"]
    assert result["token"] == "web-token"


@pytest.mark.asyncio
async def test_restore_does_not_require_manual_phase(tmp_path):
    browser = BrowserLogin(tmp_path)
    browser.manual_login = AsyncMock()
    browser.start = AsyncMock()
    async def command(method, params=None):
        if method == "Page.navigate":
            browser.headers = {"authorization": "OAuth web-token", "x-device-id": "device"}
        return {}
    browser.command = command
    browser.cookies = AsyncMock(return_value={"auth-token": "web-token"})
    browser.evaluate = AsyncMock(return_value="Chrome")
    await browser.login(token="web-token")
    browser.manual_login.assert_not_awaited()


@pytest.mark.asyncio
async def test_closing_before_login_has_actionable_error(tmp_path):
    browser = BrowserLogin(tmp_path)
    browser.manual_login = AsyncMock()
    browser.start = AsyncMock()
    browser.cookies = AsyncMock(return_value={})
    with pytest.raises(LoginException, match="before login completed"):
        await browser.login()


@pytest.mark.asyncio
async def test_interrupted_read_only_query_reconnects_once(tmp_path):
    browser = BrowserLogin(tmp_path)
    browser._authenticated = True
    browser.captured_at = monotonic()
    browser.evaluate = AsyncMock(side_effect=[LoginException("closed"), {"status": 200, "body": {"data": {}}}])
    async def ensure_capture_is_available():
        assert browser._gql_inflight == 0
    browser.reconnect = AsyncMock(side_effect=ensure_capture_is_available)
    response = await browser.gql({"operationName": "ViewerDropsDashboard"}, read_only=True)
    assert response == {"data": {}}
    browser.reconnect.assert_awaited_once()
    assert browser.evaluate.await_count == 2
    assert browser._gql_inflight == 0


@pytest.mark.asyncio
async def test_failed_reconnected_read_does_not_loop(tmp_path):
    browser = BrowserLogin(tmp_path)
    browser._authenticated = True
    browser.captured_at = monotonic()
    browser.evaluate = AsyncMock(side_effect=LoginException("closed"))
    browser.reconnect = AsyncMock()
    with pytest.raises(LoginException, match="closed"):
        await browser.gql({"operationName": "ViewerDropsDashboard"}, read_only=True)
    assert browser.evaluate.await_count == 2
    browser.reconnect.assert_awaited_once()
