import asyncio
from datetime import timedelta

import pytest

from src.controller import Conflict, Controller
from src.models import State
from src.portal import Result
from src.state_machine import validate_transition


def scenario(controller, result, wait=None):
    class FakePortal:
        def __init__(self, browser):
            self.browser = browser

        async def run(self, profile, checking):
            if wait:
                await wait.wait()
            checking()
            return result

        async def inspect(self, **kwargs):
            return result

        async def verify_confirmation(self, reference, appointment):
            return result.state == State.BOOKED
    controller.portal_factory = FakePortal


async def checked(controller):
    await controller.request_check()
    await controller.check_task


async def test_hold_same_objects_stop_scheduler_and_other_applicant(controller):
    scenario(controller, Result(State.POSSIBLE_SLOTS, "POSSIBLE_SLOTS_UNRESERVED"))
    page = controller.browser.page
    await checked(controller)
    assert controller.paused and controller.browser.alive and controller.browser.closes == 0
    assert controller.notifications.messages[-1] == (1, State.POSSIBLE_SLOTS)
    with pytest.raises(Conflict):
        await controller.request_check(2)
    await controller.takeover()
    assert controller.browser.page is page and controller.browser.starts == 1
    assert controller.human and controller.state == State.BOOKING_IN_PROGRESS
    with pytest.raises(Conflict):
        await controller.command("resume")


async def test_no_parallel_and_cancel_during_navigation(controller):
    scenario(controller, Result(State.NO_SLOTS, "NO_SLOTS"), asyncio.Event())
    await controller.request_check()
    with pytest.raises(Conflict):
        await controller.request_check()
    await asyncio.sleep(0)
    await controller.takeover()
    assert controller.browser.alive and controller.browser.closes == 0
    assert controller.state == State.AWAITING_USER


async def test_global_rate_and_round_robin(controller, clock):
    scenario(controller, Result(State.NO_SLOTS, "NO_SLOTS"))
    await checked(controller)
    assert controller.active_id == 1 and not controller.browser.alive
    with pytest.raises(Conflict):
        await controller.request_check(2)
    clock.now += timedelta(minutes=15)
    await checked(controller)
    assert controller.active_id == 2


async def test_booked_profile_cannot_be_reenabled_for_checks(controller, clock):
    controller.storage.book(1, "2026-11-20", "MOCK", "MOCK-ABC123")
    restarted = Controller(controller.storage, controller.browser, controller.notifications, clock=clock, memory=lambda: 0)
    assert restarted.profiles[0].booked
    raw = [p.model_dump(mode="json") for p in restarted.profiles]
    raw[0]["booked"] = False
    restarted.save_profiles(raw)
    assert restarted.profiles[0].booked and restarted.select().id == 2


async def test_network_error_and_backoff(controller, clock):
    class Broken:
        def __init__(self, browser):
            pass

        async def run(self, profile, checking):
            raise OSError("SENSITIVE_NETWORK_TEXT")
    controller.portal_factory = Broken
    await checked(controller)
    assert controller.state == State.ERROR and controller.paused
    await controller.command("discard")
    await controller.command("resume")
    clock.now += timedelta(minutes=15)
    with pytest.raises(Conflict):
        await controller.request_check()
    assert "SENSITIVE_NETWORK_TEXT" not in str(controller.storage.events())


async def test_restart_does_not_restore_availability(controller, clock):
    scenario(controller, Result(State.POSSIBLE_SLOTS, "POSSIBLE_SLOTS_UNRESERVED"))
    await checked(controller)
    await controller.close()
    restarted = Controller(controller.storage, controller.browser, controller.notifications, clock=clock, memory=lambda: 0)
    assert restarted.state == State.SESSION_EXPIRED
    assert restarted.paused and not restarted.possible_available and not restarted.browser.alive


async def test_expiry_keeps_browser_but_marks_stale(controller, clock):
    scenario(controller, Result(State.POSSIBLE_SLOTS, "POSSIBLE_SLOTS_UNRESERVED"))
    await checked(controller)
    clock.now += timedelta(minutes=31)
    await controller.tick()
    assert controller.state == State.SESSION_EXPIRED
    assert not controller.possible_available and controller.browser.alive


async def test_block_is_persistent(controller, clock):
    scenario(controller, Result(State.BLOCKED, "PORTAL_BLOCKED"))
    await checked(controller)
    await controller.close()
    restarted = Controller(controller.storage, controller.browser, controller.notifications, clock=clock, memory=lambda: 0)
    assert restarted.state == State.BLOCKED and restarted.paused
    with pytest.raises(Conflict):
        await restarted.command("resume")


def test_invalid_transition():
    with pytest.raises(ValueError):
        validate_transition(State.NAVIGATING, State.BOOKED)
    with pytest.raises(ValueError):
        validate_transition(State.POSSIBLE_SLOTS, State.NAVIGATING)


async def test_rate_limit_survives_restart(controller, clock):
    scenario(controller, Result(State.NO_SLOTS, "NO_SLOTS"))
    await checked(controller)
    await controller.close()
    restarted = Controller(controller.storage, controller.browser, controller.notifications, clock=clock, memory=lambda: 0)
    with pytest.raises(Conflict):
        await restarted.request_check(2)


async def test_lost_availability_is_indicated_without_reload_or_close(controller, clock):
    scenario(controller, Result(State.POSSIBLE_SLOTS, "POSSIBLE_SLOTS_UNRESERVED"))
    await checked(controller)
    page = controller.browser.page
    scenario(controller, Result(State.NO_SLOTS, "NO_SLOTS"))
    clock.now += timedelta(seconds=16)
    await controller.tick()
    assert not controller.possible_available and controller.paused
    assert controller.state == State.AWAITING_USER
    assert controller.browser.page is page and controller.browser.closes == 0
