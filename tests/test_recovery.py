import asyncio
from datetime import timedelta

import pytest

from src.controller import Conflict
from src.models import State
from src.portal import Result


def portal(controller,result,gate=None):
    class Portal:
        def __init__(self,browser):pass
        async def run(self,profile,checking):
            if gate:await gate.wait()
            checking();return result
    controller.portal_factory=Portal


async def test_blank_error_auto_releases_and_recovery_allows_later_check(controller,clock):
    controller.browser.is_blank=True
    class Bad:
        def __init__(self,browser):pass
        async def run(self,profile,checking):raise OSError('PRIVATE_VALUE')
    controller.portal_factory=Bad
    await controller.request_check();await controller.check_task
    assert not controller.browser.alive and not controller.paused
    assert controller.state==State.ERROR
    last=controller.last_check
    result=await controller.command('recover')
    assert result['ok'] and controller.state==State.IDLE and controller.last_check==last
    with pytest.raises(Conflict):await controller.request_check()
    controller.browser.is_blank=False
    clock.now+=timedelta(minutes=30)
    portal(controller,Result(State.NO_SLOTS,'NO_SLOTS'))
    await controller.request_check();await controller.check_task
    assert controller.state==State.NO_SLOTS


async def test_stop_during_blank_navigation_cancels_and_releases(controller):
    controller.browser.is_blank=True
    portal(controller,Result(State.NO_SLOTS,'NO_SLOTS'),asyncio.Event())
    await controller.command('monitor_start')
    await controller.request_check();await asyncio.sleep(0)
    result=await controller.command('monitor_stop')
    assert result['ok'] and not controller.busy and not controller.browser.alive
    assert controller.state==State.IDLE and not controller.monitoring


async def test_candidate_never_closed_without_boolean_confirmation(controller,clock):
    portal(controller,Result(State.POSSIBLE_SLOTS,'POSSIBLE_SLOTS_UNRESERVED'))
    await controller.request_check();await controller.check_task
    page=controller.browser.page
    for data in [{},{'confirm_discard':'false'}]:
        with pytest.raises(Conflict):await controller.command('recover',data)
        assert controller.browser.page is page and controller.browser.closes==0
    # Freshness may expire, but the protected-session latch must survive.
    controller.possible_available=False
    controller.state=State.SESSION_EXPIRED
    with pytest.raises(Conflict):await controller.command('recover')
    await controller.command('recover',{'confirm_discard':True})
    assert not controller.browser.alive and controller.state==State.IDLE


async def test_recovery_serializes_double_clicks(controller):
    started=asyncio.Event();release=asyncio.Event()
    async def close():
        started.set();await release.wait();controller.browser.alive=False
    controller.browser.close=close
    first=asyncio.create_task(controller.command('recover'))
    await started.wait()
    with pytest.raises(Conflict):await controller.command('recover')
    with pytest.raises(Conflict):await controller.command('check')
    release.set();await first
    assert controller.state==State.IDLE


async def test_hung_real_chromium_close_is_bounded(browser,monkeypatch):
    gate=asyncio.Event()
    async def hung():await gate.wait()
    monkeypatch.setattr(browser.browser,'close',hung)
    browser.SHUTDOWN_TIMEOUT=.05
    assert browser.processes
    await asyncio.wait_for(browser.close(),2)
    assert not browser.alive and not browser.processes


async def test_stale_process_identity_cannot_kill_another_process():
    import sys
    from src.browser import Browser
    child=await asyncio.create_subprocess_exec(sys.executable,'-c','import time; time.sleep(60)')
    try:
        browser=Browser()
        browser.processes={child.pid:'0'}
        await browser.close()
        assert child.returncode is None
    finally:
        child.kill();await child.wait()
