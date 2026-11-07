import asyncio
import logging
import os
from pathlib import Path

from aiohttp import web

from .browser import Browser, Desktop
from .controller import Controller
from .notifications import Notifications
from .storage import Storage
from .web import create_app


async def main():
    os.umask(0o077)
    # Third-party loggers may include URLs, request bodies and browser diagnostics.
    logging.disable(logging.CRITICAL)
    storage = Storage(Path("/data"))
    browser, desktop = Browser(), Desktop()
    controller = None
    notifications = Notifications(lambda: controller.settings, storage)
    controller = Controller(storage, browser, notifications)
    notifications.monitor_status = lambda: (
        f"Barcelona TIE\nСостояние: {controller.state.value}\n"
        f"Профиль: {controller.active.label if controller.active else '—'}\n"
        f"Мониторинг: {'включён' if controller.monitoring else 'выключен'}\n"
        f"Пауза: {'да' if controller.paused else 'нет'}")
    app = create_app(controller)

    async def lifecycle(app):
        try:
            await desktop.start()
            notifications.start()
            controller.loop_task = asyncio.create_task(controller.run())
            yield
        finally:
            await controller.close()
            await notifications.close()
            await desktop.close()
            storage.close()

    app.cleanup_ctx.append(lifecycle)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=15)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8099).start()
    stop = asyncio.Event()
    import signal
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        # Validation/startup exceptions may contain values read from the private store.
        # Never let Python format them into Supervisor's log.
        import sys
        print("CRITICAL_STARTUP_ERROR: проверьте локальные настройки и окружение", file=sys.stderr)
        raise SystemExit(1) from None
