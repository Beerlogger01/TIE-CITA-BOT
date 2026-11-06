import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.browser import Browser
from src.controller import Controller
from src.models import Applicant
from src.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def applicant():
    return Applicant(id=1, label="Антон", enabled=True, full_name="TEST APPLICANT",
                     nie="X" + "0" * 7 + "T")  # Synthetic; not a real applicant.


@pytest.fixture
async def mock_portal():
    data = {"outcome": "no_slots", "visits": [], "forms": 0, "status": 200}

    async def handler(request):
        if request.path == "/favicon.ico":
            return web.Response(status=204)
        data["visits"].append(request.path)
        page = {"/": "province", "/procedure": "procedure", "/entry": "entry", "/applicant": "applicant",
                "/summary": "summary", "/result": data["outcome"], "/confirmed": "confirmed"}[request.path]
        if request.path == "/summary":
            posted = await request.post()
            assert posted["document"].startswith(("X", "Y"))
            assert posted["kind"] == "nie"
            data["forms"] += 1
        response = web.Response(text=(FIXTURES / f"{page}.html").read_text(), content_type="text/html",
                                status=data["status"] if request.path == "/result" else 200)
        if request.path == "/":
            response.set_cookie("mock_session", "one-existing-session", httponly=True)
        else:
            assert request.cookies.get("mock_session") == "one-existing-session"
        return response

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    async with TestServer(app) as server:
        data["url"] = str(server.make_url("/"))
        yield data


@pytest.fixture
async def browser():
    executable = os.environ.get("BROWSER_EXECUTABLE")
    if not executable and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        executable = shutil.which("chromium")
    b = Browser(headless=True, executable_path=executable)
    await b.start()
    try:
        yield b
    finally:
        await b.close()


class FakeNotifications:
    def __init__(self):
        self.messages = []

    def emit(self, profile, state, **kwargs):
        self.messages.append((profile.id if profile else None, state))

    async def sensors(self, status):
        pass


class FakeBrowser:
    def __init__(self):
        self.alive = False
        self.starts = self.closes = 0
        self.page = object()

    async def start(self):
        self.alive = True
        self.starts += 1

    async def close(self):
        self.alive = False
        self.closes += 1


@pytest.fixture
def clock():
    class Clock:
        now = datetime(2026, 11, 5, 13, 45, tzinfo=timezone.utc)

        def __call__(self):
            return self.now
    return Clock()


@pytest.fixture
def controller(tmp_path, applicant, clock):
    storage = Storage(tmp_path / "data")
    storage.save_profiles([applicant, applicant.model_copy(update={"id": 2, "label": "Наталья", "nie": "Y" + "0" * 7 + "T"})])
    c = Controller(storage, FakeBrowser(), FakeNotifications(), clock=clock, memory=lambda: 100)
    yield c
    storage.close()
