import asyncio
import os
import signal
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
    SHUTDOWN_TIMEOUT = 3
    def __init__(self, *, headless=False, executable_path=None):
        self.headless = headless
        self.executable_path = executable_path
        self.playwright = self.browser = self.context = self.page = None
        self.http_status = None
        self.extra_tab_blocked = False
        self.processes = {}
        self.responsive = True

    @staticmethod
    def descendants():
        records = {}
        for file in Path('/proc').glob('[0-9]*/stat'):
            try:
                fields = file.read_text().rsplit(')',1)[1].split()
                records[int(file.parent.name)] = (int(fields[1]),fields[19])
            except (OSError,ValueError,IndexError):
                pass
        owned = {os.getpid()}
        while True:
            more = {pid for pid,(parent,_) in records.items() if parent in owned}
            if more <= owned:
                break
            owned |= more
        return {pid:records[pid][1] for pid in owned if pid != os.getpid()}

    @property
    def process_running(self):
        return bool(self.browser and self.browser.is_connected())

    @property
    def is_blank(self):
        try:
            return not self.page or self.page.is_closed() or self.page.url in ('','about:blank')
        except Exception:
            return True

    @property
    def portal_loaded(self):
        from urllib.parse import urlsplit
        try:
            u = urlsplit(self.page.url)
            return u.scheme == 'https' and u.hostname == 'icp.administracionelectronica.gob.es'
        except Exception:
            return False

    @property
    def usable(self):
        return self.alive and not self.is_blank and self.responsive and self.page.url.startswith(('https://','http://'))

    async def probe(self):
        try:
            await asyncio.wait_for(self.page.locator('body').count(), 2)
            self.responsive = True
        except Exception:
            self.responsive = False
        return self.responsive

    @property
    def alive(self):
        return bool(self.browser and self.browser.is_connected() and self.page and not self.page.is_closed())

    async def start(self):
        if self.alive:
            raise RuntimeError("SESSION_ALREADY_EXISTS")
        if self.browser or self.playwright:
            await self.close()
        before = self.descendants()
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
            self.responsive = True
            self.processes = {pid:start for pid,start in self.descendants().items() if pid not in before}
            return self.page
        except BaseException:
            self.processes.update({pid:start for pid,start in self.descendants().items() if pid not in before})
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
        # Refresh owned descendants before graceful shutdown can orphan them.
        for pid,start in list(self.processes.items()):
            try:
                fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
                if fields[19] != start:
                    del self.processes[pid]
            except (OSError,ValueError,IndexError):
                del self.processes[pid]
        all_processes = self.descendants()
        for pid,start in list(all_processes.items()):
            try:
                current = pid
                while current not in self.processes and current != os.getpid():
                    fields = Path(f'/proc/{current}/stat').read_text().rsplit(')',1)[1].split()
                    current = int(fields[1])
                if current in self.processes:
                    self.processes[pid] = start
            except (OSError,ValueError,IndexError):
                pass
        try:
            if self.browser:
                try:
                    await asyncio.wait_for(self.browser.close(), self.SHUTDOWN_TIMEOUT)
                except Exception:
                    pass
        finally:
            if self.playwright:
                # Do not cancel the driver's transport waiter before it reaps its
                # process/pipes. Kill our processes on timeout, then let it finish.
                stop_task = asyncio.create_task(self.playwright.stop())
                done,_ = await asyncio.wait({stop_task}, timeout=self.SHUTDOWN_TIMEOUT)
                if not done:
                    self.force_close()
                    done,_ = await asyncio.wait({stop_task}, timeout=1)
                if not done:
                    stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
            self.force_close()

    def force_close(self):
        # Kill only this launch's recorded processes, with PID reuse protection.
        for pid,start in self.processes.items():
            try:
                fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
                if fields[19] == start:
                    os.kill(pid, signal.SIGKILL)
            except (OSError,ValueError,IndexError):
                pass
        self.processes.clear()
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
