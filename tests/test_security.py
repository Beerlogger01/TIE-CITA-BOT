import json
import stat
from pathlib import Path

import pytest
import yaml
import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from src.models import Settings
from src.models import State
from src.notifications import Notifications
from src.web import create_app


async def test_ingress_peer_not_spoofable(controller):
    async with TestClient(TestServer(create_app(controller))) as client:
        r = await client.get("/api/status", headers={"X-Ingress-Path": "/fake", "X-Forwarded-For": "172.30.32.2"})
        assert r.status == 403


async def test_csrf_redaction_and_ui(controller):
    async with TestClient(TestServer(create_app(controller, trusted_peer="127.0.0.1"))) as client:
        r = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/test"})
        assert r.status == 200 and 'src="app.js"' in await r.text()
        assert r.headers["Cache-Control"] == "no-store"
        assert (await client.post("/api/command", json={"action": "check"})).status == 403
        csrf = (await (await client.get("/api/status")).json())["csrf"]
        headers = {"X-CSRF-Token": csrf}
        assert (await client.post("/api/command", json={"action": "pause"}, headers=headers)).status == 200
        controller.settings.smtp_password = "PRIVATE_TEST_SECRET"
        assert "PRIVATE_TEST_SECRET" not in await (await client.get("/api/settings")).text()
        raw = await (await client.get("/api/profiles")).json()
        raw[0]["nie"] = "PRIVATE_INVALID_DOCUMENT"
        r = await client.post("/api/profiles", json=raw, headers=headers)
        assert r.status == 409 and "PRIVATE_INVALID_DOCUMENT" not in await r.text()


def test_private_storage_and_no_pii_in_events(controller, caplog):
    assert stat.S_IMODE(controller.storage.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(controller.storage.path.parent.stat().st_mode) == 0o700
    with pytest.raises(ValueError):
        controller.storage.event(1, controller.profiles[0].nie)
    controller.storage.event(1, "NO_SLOTS")
    assert controller.profiles[0].nie not in json.dumps(controller.storage.events())
    assert controller.profiles[0].full_name not in caplog.text


@pytest.mark.parametrize("url", ["http://ha/hassio/ingress/local_cita", "https://ha/hassio/ingress/local_cita?token=secret",
                                "https://user:pass@ha/hassio/ingress/local_cita", "https://ha/api/hassio_ingress/session"])
def test_no_credentials_in_links(url):
    with pytest.raises(ValidationError):
        Settings(ingress_url=url)


async def test_telegram_payload_allowlist_and_email_fallback(controller, monkeypatch):
    s = Settings(ingress_url="https://ha.example.invalid/hassio/ingress/local_barcelona_cita",
                 telegram_enabled=True, telegram_token="TEST_TOKEN", telegram_chat_ids=["123", "456"],
                 smtp_enabled=True, smtp_host="smtp.example.invalid", smtp_sender="sender@example.invalid",
                 smtp_recipients=["receiver@example.invalid"], ha_notifications=False)
    n = Notifications(lambda: s, controller.storage)
    sent, emails = [], []

    async def post(url, payload, **kwargs):
        sent.append(payload)
        raise OSError("PRIVATE_TEST_SECRET")

    monkeypatch.setattr(n, "post", post)
    monkeypatch.setattr(n, "smtp", lambda settings, text: emails.append(text))
    n.emit(controller.profiles[0], controller.state)
    profile, text = n.queue.get_nowait()
    await n.send(profile, text)
    assert {p["chat_id"] for p in sent} == {"123", "456"}
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["url"] == s.ingress_url
    assert controller.profiles[0].nie not in text and controller.profiles[0].full_name not in text
    assert emails and "PRIVATE_TEST_SECRET" not in str(controller.storage.events())
    assert any(e["code"] == "NOTIFY_SMTP_SENT" for e in controller.storage.events())


def test_addon_no_exposed_ports_or_privileges():
    config = yaml.safe_load(Path("barcelona_cita/config.yaml").read_text())
    assert config["ingress"] and config["homeassistant_api"] and "aarch64" in config["arch"]
    assert not any(config.get(key) for key in ("ports", "host_network", "privileged", "full_access", "docker_api", "hassio_api"))


async def test_websocket_requires_csrf_before_takeover(controller):
    controller.browser.alive = True
    controller.state = State.POSSIBLE_SLOTS
    async with TestClient(TestServer(create_app(controller, trusted_peer="127.0.0.1"))) as client:
        with pytest.raises(aiohttp.WSServerHandshakeError) as error:
            await client.ws_connect("/ws", protocols=["binary"])
        assert error.value.status == 403
    assert not controller.human
