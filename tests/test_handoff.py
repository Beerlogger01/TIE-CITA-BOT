"""Real headed Chromium -> Xvfb -> x11vnc -> websockify -> ingress proxy -> noVNC.

The portal, HA authentication gate and Telegram delivery are local test doubles.
The desktop, WebSockets, browser clients and remote key input are real.
"""
import asyncio
import os
import shutil
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
from playwright.async_api import expect

from src.browser import Browser, Desktop
from src.controller import Controller, Conflict
from src.models import Settings, State
from src.notifications import Notifications
from src.portal import Portal
from src.web import create_app


@asynccontextmanager
async def ingress_proxy(upstream):
    prefix = "/api/hassio_ingress/mock/"
    async with aiohttp.ClientSession() as session:
        async def handler(request):
            if request.path == "/favicon.ico":
                return web.Response(status=204)
            if request.cookies.get("ha_mock_login") != "yes":
                raise web.HTTPUnauthorized()
            if request.path == "/hassio/ingress/local_barcelona_cita":
                raise web.HTTPFound(prefix)
            tail = request.path.removeprefix(prefix)
            url = str(upstream.make_url("/" + tail.lstrip("/")))
            if request.headers.get("Upgrade", "").lower() == "websocket":
                protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")]
                async with session.ws_connect(url, protocols=protocols) as backend:
                    ws = web.WebSocketResponse(protocols=["binary"])
                    await ws.prepare(request)
                    async def relay(source, target):
                        async for message in source:
                            if message.type == WSMsgType.BINARY:
                                await target.send_bytes(message.data)
                            elif message.type == WSMsgType.TEXT:
                                await target.send_str(message.data)
                    tasks = [asyncio.create_task(relay(ws, backend)), asyncio.create_task(relay(backend, ws))]
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for t in tasks:
                            t.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        await ws.close()
                    return ws
            headers = {"X-Ingress-Path": prefix.rstrip("/")}
            if request.headers.get("X-CSRF-Token"):
                headers["X-CSRF-Token"] = request.headers["X-CSRF-Token"]
            if request.content_type == "application/json":
                headers["Content-Type"] = "application/json"
            async with session.request(request.method, url, data=await request.read(), headers=headers) as response:
                return web.Response(body=await response.read(), status=response.status, headers={
                    k: v for k, v in response.headers.items() if k.lower() in
                    {"content-type", "content-security-policy", "cache-control", "referrer-policy"}})
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        async with TestServer(app) as server:
            yield server


@pytest.mark.desktop
@pytest.mark.parametrize("device", ["desktop", "android", "iphone"])
async def test_notification_link_controls_existing_tab(controller, mock_portal, monkeypatch, device):
    if not all(shutil.which(p) for p in ("Xvfb", "x11vnc", "websockify", "openbox")):
        pytest.skip("Install Xvfb/x11vnc/noVNC/websockify/openbox or run in the built image")
    monkeypatch.setenv("DISPLAY", ":99")
    desktop = Desktop()
    target = Browser(headless=False, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
    client_browser = Browser(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
    mock_portal["outcome"] = "possible"
    controller.browser = target
    controller.portal_factory = lambda b: Portal(b, url=mock_portal["url"])
    controller.settings = Settings(telegram_enabled=True, telegram_token="TEST_ONLY",
        telegram_polling_enabled=True, ingress_url="https://ha.example.invalid/hassio/ingress/local_barcelona_cita",
        ha_notifications=False)
    notifications = Notifications(lambda: controller.settings, controller.storage)
    controller.notifications = notifications
    payloads = []
    invitations = [notifications.recipients.invite(controller.settings) for _ in range(2)]
    registered = asyncio.Event()
    updates_sent = False
    async def telegram_api(token, method, payload):
        nonlocal updates_sent
        if method == 'getWebhookInfo':
            return {'url':''}
        if method == 'getUpdates':
            if updates_sent:
                await asyncio.Event().wait()
            updates_sent = True
            return [{'update_id':i+1,'message':{'chat':{'id':1001+i,'type':'private','first_name':'TEST'},
                                              'text':'/start '+code}} for i,code in enumerate(invitations)]
        if method == 'sendMessage':
            if payload['chat_id'] == '1002':
                registered.set()
            return {}
        raise AssertionError('Unexpected Telegram method')
    monkeypatch.setattr(notifications.receiver,'api',telegram_api)
    async def capture(url, payload, **kwargs):
        assert controller.paused and target.alive
        payloads.append(payload)
    monkeypatch.setattr(notifications, "post", capture)
    try:
        await desktop.start()
        notifications.start()
        await asyncio.wait_for(registered.wait(),5)
        await controller.request_check()
        await controller.check_task
        await asyncio.wait_for(notifications.queue.join(), 5)
        assert controller.state == State.POSSIBLE_SLOTS and controller.paused
        assert len(payloads) == 2
        link = payloads[0]["reply_markup"]["inline_keyboard"][0][0]["url"]
        page, context, browser_object = target.page, target.context, target.browser
        url_before = page.url
        cookies_before = await context.cookies()
        # Unsaved JS state proves the original document survives handoff.
        await page.evaluate("window.handoffMarker = 'original-document'")
        app = create_app(controller, trusted_peer="127.0.0.1")
        async with TestServer(app) as upstream, ingress_proxy(upstream) as gateway:
            async with aiohttp.ClientSession() as unauthenticated:
                r = await unauthenticated.get(gateway.make_url(urlsplit(link).path))
                assert r.status == 401
            await client_browser.start()
            # A different client/context has only a simulated HA login, no portal cookie.
            await client_browser.context.add_cookies([{"name": "ha_mock_login", "value": "yes",
                                                       "url": str(gateway.make_url("/"))}])
            if device != "desktop":
                await client_browser.page.set_viewport_size({"width": 390 if device == "iphone" else 412, "height": 844})
            errors = []
            client_browser.page.on("pageerror", lambda e: errors.append(str(e)))
            await client_browser.page.goto(str(gateway.make_url(urlsplit(link).path)))
            await client_browser.page.get_by_role("link", name="Открыть рабочий стол").click()
            await expect(client_browser.page.locator("#connection")).to_contain_text("ручное управление", timeout=15000)
            assert controller.human and controller.paused and controller.viewers == 1
            assert target.page is page and target.context is context and target.browser is browser_object
            assert page.url == url_before
            assert await context.cookies() == cookies_before
            assert await page.evaluate("window.handoffMarker") == "original-document"
            # Send keys through noVNC's mobile keyboard, not through the target Playwright page.
            # The local fixture autofocuses this field before handoff.
            assert await page.locator("#human-input").evaluate("e => e === document.activeElement")
            await client_browser.page.locator("#keyboard-input").fill("REMOTE")
            for _ in range(100):
                if await page.locator("#human-input").input_value() == "REMOTE":
                    break
                await asyncio.sleep(.05)
            assert await page.locator("#human-input").input_value() == "REMOTE"
            assert mock_portal["forms"] == 1 and mock_portal["visits"].count("/") == 1
            assert len(context.pages) == 1 and not errors
            await client_browser.page.get_by_role("button", name="Отключить управление").click()
            for _ in range(50):
                if controller.viewers == 0:
                    break
                await asyncio.sleep(.05)
            assert controller.viewers == 0 and controller.paused and not controller.human
            with pytest.raises(Conflict):
                await controller.command("recover")
            assert target.page is page
            await controller.command("recover", {"confirm_discard": True})
            assert controller.state == State.IDLE and not target.alive
    finally:
        await client_browser.close()
        await controller.close()
        await notifications.close()
        await desktop.close()
