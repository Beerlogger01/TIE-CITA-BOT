from datetime import date

import pytest

from src.models import State
from src.portal import Halt, Portal, classify


@pytest.mark.parametrize("outcome,expected", [
    ("no_slots", State.NO_SLOTS), ("possible", State.POSSIBLE_SLOTS),
    ("unknown", State.AWAITING_USER), ("expired", State.SESSION_EXPIRED),
    ("blocked", State.BLOCKED), ("captcha", State.AWAITING_USER), ("error", State.ERROR),
])
async def test_known_sequence_real_browser(mock_portal, browser, applicant, outcome, expected):
    mock_portal["outcome"] = outcome
    marker = []
    result = await Portal(browser, url=mock_portal["url"]).run(applicant, lambda: marker.append(True))
    assert result.state == expected
    assert mock_portal["visits"] == ["/", "/procedure", "/entry", "/applicant", "/summary", "/result"]
    assert mock_portal["forms"] == 1
    assert browser.alive
    if outcome not in {"blocked", "captcha", "error", "expired"}:
        assert marker


def test_warning_not_no_slots_until_request():
    text = "En este momento no hay citas disponibles"
    assert classify(text).state == State.AWAITING_USER
    assert classify(text, after_request=True).state == State.NO_SLOTS
    assert classify(text, after_request=True, http_status=500).state == State.ERROR
    assert classify(text, after_request=True, http_status=429).state == State.BLOCKED
    assert classify("Seleccione una fecha", after_request=True).state == State.AWAITING_USER
    assert classify("Calendario", after_request=True, calendar=True).state != State.BOOKED


async def test_confirmation_needs_server_evidence(mock_portal, browser, applicant):
    mock_portal["outcome"] = "possible"
    portal = Portal(browser, url=mock_portal["url"])
    await portal.run(applicant, lambda: None)
    assert not await portal.verify_confirmation("MOCK-ABC123", date(2026, 11, 20))
    await browser.page.goto(mock_portal["url"] + "confirmed")
    assert await portal.verify_confirmation("MOCK-ABC123", date(2026, 11, 20))
    assert not await portal.verify_confirmation("ABC123", date(2026, 11, 20))
    assert not await portal.verify_confirmation("MOCK-ABC123", date(2026, 11, 21))


async def test_https_downgrade_stops_before_any_form_access():
    from types import SimpleNamespace
    fake = SimpleNamespace(page=SimpleNamespace(url="http://icp.administracionelectronica.gob.es/icpplus/"),
                           extra_tab_blocked=False)
    with pytest.raises(Halt) as error:
        await Portal(fake).guard()
    assert error.value.result.code == "UNEXPECTED_NAVIGATION"
