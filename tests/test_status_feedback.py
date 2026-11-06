import asyncio
from datetime import timedelta

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.controller import Conflict
from src.models import State
from src.notifications import Notifications
from src.portal import Result
from src.web import create_app


async def test_viewer_does_not_cancel_inflight_navigation(controller):
    gate = asyncio.Event()
    class SlowPortal:
        def __init__(self, browser):
            pass
        async def run(self, profile, checking):
            self.progress("PORTAL_OPENING")
            await gate.wait()
            checking()
            return Result(State.NO_SLOTS, "NO_SLOTS")
    controller.portal_factory = SlowPortal
    await controller.request_check()
    await asyncio.sleep(0)
    try:
        async with TestClient(TestServer(create_app(controller, trusted_peer="127.0.0.1"))) as client:
            s = await (await client.get('/api/status')).json()
            assert s['busy'] and s['stage'] == 'PORTAL_OPENING'
            assert (await client.get('/browser')).status == 200
            with pytest.raises(aiohttp.WSServerHandshakeError) as error:
                await client.ws_connect('/ws', protocols=['binary', 'cita-csrf.' + s['csrf']])
            assert error.value.status == 409
            assert not controller.human and not controller.paused
            assert controller.busy and not controller.check_task.cancelled()
    finally:
        gate.set()
        await controller.check_task
    assert controller.state == State.NO_SLOTS
    assert controller.browser.starts == 1


async def test_notification_test_does_not_contact_portal(controller, clock):
    n = Notifications(lambda: controller.settings, controller.storage)
    controller.notifications = n
    await controller.command('test_notifications')
    profile, text = n.queue.get_nowait()
    assert profile is None and 'тест' in text.lower()
    assert 'свободная запись не обнаружена' in text
    assert controller.profiles[0].nie not in text
    assert controller.last_check is None and controller.browser.starts == 0
    with pytest.raises(Conflict):
        await controller.command('test_notifications')
    clock.now += timedelta(seconds=60)
    await controller.command('test_notifications')


async def test_manual_check_deadline_visible_when_automatic_disabled(controller, clock):
    controller.last_check = clock.now
    controller.settings.automatic = False
    status = controller.status()
    assert status['next_check'] is None
    assert status['next_allowed_check'] == (clock.now + timedelta(minutes=15)).isoformat()
