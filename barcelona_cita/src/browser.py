import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright


class Desktop:
    """Private display and loopback-only VNC endpoints, never Docker published ports."""

    def __init__(self):
        self.children = []

    async def start(self):
        commands = [
            ["Xvfb", ":99", "-screen", "0", "1280x800x24", "-nolisten", "tcp", "-ac"],
            ["openbox"],
            ["x11vnc", "-display", ":99", "-localhost", "-rfbport", "5900", "-nopw",
             "-forever", "-shared", "-noxdamage", "-quiet"],
            ["/usr/bin/python3", "-m", "websockify", "127.0.0.1:6081", "127.0.0.1:5900"],
        ]
        try:
            for i, cmd in enumerate(commands):
                child = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                self.children.append(child)
                if i == 0:
                    for _ in range(50):
                        if Path("/tmp/.X11-unix/X99").exists():
                            break
                        if child.returncode is not None:
                            raise RuntimeError("DISPLAY_START_FAILED")
                        await asyncio.sleep(.1)
                    else:
                        raise RuntimeError("DISPLAY_START_FAILED")
            for _ in range(50):
                if any(c.returncode is not None for c in self.children):
                    raise RuntimeError("DESKTOP_START_FAILED")
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", 6081)
                    writer.close()
                    await writer.wait_closed()
                    return
                except OSError:
                    await asyncio.sleep(.1)
            raise RuntimeError("VNC_START_FAILED")
        except BaseException:
            await self.close()
            raise

    async def close(self):
        for child in reversed(self.children):
            if child.returncode is None:
                child.terminate()
                try:
                    await asyncio.wait_for(child.wait(), 5)
                except TimeoutError:
                    child.kill()
                    await child.wait()
        self.children.clear()


class Browser:
    def __init__(self, *, headless=False, executable_path=None):
        self.headless = headless
        self.executable_path = executable_path
        self.playwright = self.browser = self.context = self.page = None
        self.http_status = None
        self.extra_tab_blocked = False

    @property
    def alive(self):
        return bool(self.browser and self.browser.is_connected() and self.page and not self.page.is_closed())

    async def start(self):
        if self.alive:
            raise RuntimeError("SESSION_ALREADY_EXISTS")
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless, executable_path=self.executable_path,
                channel=None if self.executable_path else "chromium",
                # HA doesn't permit the user namespace/seccomp setup needed by Chromium's
                # sandbox. Run as unprivileged cita, without SYS_ADMIN or host networking.
                chromium_sandbox=False,
                args=["--disable-dev-shm-usage", "--disable-background-networking", "--no-first-run"],
            )
            self.context = await self.browser.new_context(
                viewport={"width": 1240, "height": 700}, locale="es-ES", timezone_id="Europe/Madrid",
                accept_downloads=False,
            )
            self.page = await self.context.new_page()
            self.page.set_default_timeout(12000)
            self.page.set_default_navigation_timeout(40000)
            self.context.on("page", self._extra_page)
            self.page.on("response", self._response)
            self.page.on("dialog", lambda dialog: dialog.dismiss())
            self.http_status = None
            self.extra_tab_blocked = False
            return self.page
        except BaseException:
            await self.close()
            raise

    async def _extra_page(self, page):
        if page != self.page:
            self.extra_tab_blocked = True
            await page.close()

    def _response(self, response):
        if response.request.is_navigation_request() and response.frame == self.page.main_frame:
            self.http_status = response.status

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self.playwright:
                await self.playwright.stop()
            self.playwright = self.browser = self.context = self.page = None


def memory_mb():
    """Container RSS estimate; prefer cgroup usage, then sum this process tree."""
    for path in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        try:
            return int(Path(path).read_text()) // (1024 * 1024)
        except (OSError, ValueError):
            pass
    total = 0
    for path in Path("/proc").glob("[0-9]*/status"):
        try:
            total += next(int(x.split()[1]) for x in path.read_text().splitlines() if x.startswith("VmRSS:"))
        except (OSError, StopIteration, ValueError):
            pass
    return total // 1024
