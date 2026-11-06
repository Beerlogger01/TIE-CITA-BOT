import asyncio
import hmac
import secrets
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web
from pydantic import ValidationError

from ..controller import Conflict

ASSETS = Path(__file__).parent
SECRET_FIELDS = ("telegram_token", "smtp_password")


def create_app(controller, *, trusted_peer="172.30.32.2", novnc_path=Path("/usr/share/novnc"),
               vnc_backend="http://127.0.0.1:6081"):
    csrf = secrets.token_urlsafe(32)

    @web.middleware
    async def security(request, handler):
        # Verify the socket peer, never X-Forwarded-For or a caller-supplied ingress header.
        if request.remote != trusted_peer:
            raise web.HTTPForbidden(text="Только Home Assistant Ingress")
        if request.method not in {"GET", "HEAD"}:
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), csrf):
                raise web.HTTPForbidden(text="Недопустимый запрос")
            if request.content_type != "application/json":
                raise web.HTTPUnsupportedMediaType()
        try:
            response = await handler(request)
        except Conflict as exc:
            return web.json_response({"error": str(exc)}, status=409, headers={"Cache-Control": "no-store"})
        except (ValidationError, ValueError, TypeError, KeyError):
            # Never serialize validation inputs, exceptions or submitted PII.
            return web.json_response({"error": "Действие недоступно или данные некорректны. Проверьте паузу, активную сессию, заполнение профилей и минимальный интервал."}, status=409,
                                     headers={"Cache-Control": "no-store"})
        except web.HTTPException:
            raise
        except Exception:
            controller.storage.event(None, "WEB_REQUEST_FAILED")
            return web.json_response({"error": "Ошибка обработки запроса"}, status=500)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Content-Security-Policy":
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data: blob:; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"})
        return response

    app = web.Application(middlewares=[security], client_max_size=64 * 1024)

    async def index(request):
        return web.FileResponse(ASSETS / "index.html")

    async def status(request):
        return web.json_response({**controller.status(), "csrf": csrf})

    async def command(request):
        data = await request.json()
        await controller.command(data["action"], data)
        return web.json_response({"ok": True})

    async def profiles(request):
        if request.method == "GET":
            return web.json_response([p.model_dump(mode="json") for p in controller.profiles])
        controller.save_profiles(await request.json())
        return web.json_response({"ok": True})

    async def settings(request):
        stored = controller.settings.model_dump(mode="json")
        if request.method == "GET":
            flags = [key for key in SECRET_FIELDS if stored[key]]
            for key in SECRET_FIELDS:
                stored[key] = ""
            return web.json_response({**stored, "secret_fields_set": flags})
        raw = await request.json()
        clear = raw.pop("clear_secrets", [])
        raw.pop("secret_fields_set", None)
        for key in SECRET_FIELDS:
            if key not in clear and not raw.get(key):
                raw[key] = stored[key]
        controller.save_settings(raw)
        return web.json_response({"ok": True})

    async def browser(request):
        # This endpoint never launches Chromium, creates a context or navigates the portal.
        return web.FileResponse(ASSETS / "browser.html")

    async def socket(request):
        protocols = request.headers.get("Sec-WebSocket-Protocol", "").split(",")
        if f"cita-csrf.{csrf}" not in [p.strip() for p in protocols]:
            raise web.HTTPForbidden()
        # Merely opening the viewer must not cancel an in-flight page.goto (about:blank).
        await controller.takeover(interrupt=False)
        ws = web.WebSocketResponse(protocols=["binary"], heartbeat=25, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        controller.viewers += 1
        try:
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.ws_connect(vnc_backend, protocols=["binary"], max_msg_size=4 * 1024 * 1024) as backend:
                    async def relay(source, target):
                        async for message in source:
                            if message.type == WSMsgType.BINARY:
                                await target.send_bytes(message.data)
                            elif message.type == WSMsgType.TEXT:
                                await target.send_str(message.data)
                            else:
                                break
                    tasks = [asyncio.create_task(relay(ws, backend)), asyncio.create_task(relay(backend, ws))]
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
        except (aiohttp.ClientError, OSError):
            controller.storage.event(None, "VNC_CONNECTION_FAILED")
        finally:
            controller.viewers -= 1
            # Disconnecting a viewer NEVER resumes automation.
            await ws.close()
        return ws

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/command", command)
    app.router.add_get("/api/profiles", profiles)
    app.router.add_post("/api/profiles", profiles)
    app.router.add_get("/api/settings", settings)
    app.router.add_post("/api/settings", settings)
    app.router.add_get("/browser", browser)
    app.router.add_get("/ws", socket)
    for name in ("app.js", "browser.js", "style.css"):
        async def asset(request, filename=name):
            return web.FileResponse(ASSETS / filename)
        app.router.add_get(f"/{name}", asset)
    if novnc_path.is_dir():
        app.router.add_static("/novnc/", novnc_path, follow_symlinks=False)
    return app
