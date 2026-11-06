from datetime import timedelta

import pytest

from src.controller import Conflict
from src.models import State
from src.portal import Portal


async def test_verified_confirmation_stops_first_profile_and_continues_second(controller, browser, mock_portal, clock):
    await browser.close()
    controller.browser = browser
    controller.portal_factory = lambda b: Portal(b, url=mock_portal["url"])
    mock_portal["outcome"] = "possible"
    await controller.request_check(1)
    await controller.check_task
    assert controller.state == State.POSSIBLE_SLOTS
    await controller.takeover()
    evidence = {"appointment": "2026-11-20", "reference": "MOCK-ABC123", "office": "MOCK"}
    with pytest.raises(Conflict):
        await controller.command("verify", evidence)
    assert not controller.profiles[0].booked
    # Simulate the human completing the local mock portal. No production booking action exists.
    await browser.page.goto(mock_portal["url"] + "confirmed")
    await controller.command("verify", evidence)
    assert controller.state == State.BOOKED and controller.profiles[0].booked
    assert browser.alive and controller.paused
    assert controller.storage.bookings()[0]["profile"] == 1
    await controller.command("discard")
    await controller.command("resume")
    clock.now += timedelta(minutes=15)
    mock_portal["outcome"] = "no_slots"
    await controller.request_check()
    await controller.check_task
    assert controller.active_id == 2 and controller.state == State.NO_SLOTS
    assert controller.profiles[0].booked and mock_portal["forms"] == 2
