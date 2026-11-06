import asyncio
import os
import smtplib
import ssl
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import aiohttp

from .models import ATTENTION, State, utcnow

MESSAGES = {
    State.POSSIBLE_SLOTS: "Возможна свободная запись. Требуется ручное подтверждение на портале. Браузер сохранён. Место не зарезервировано.",
    State.AWAITING_USER: "Требуется участие человека. Откройте текущую сессию.",
    State.BOOKING_IN_PROGRESS: "Управление текущей сессией передано пользователю.",
    State.SESSION_EXPIRED: "Сессия истекла либо её доступность больше нельзя считать актуальной.",
    State.BLOCKED: "Портал ограничил доступ. Мониторинг остановлен.",
    State.BOOKED: "Подтверждение записи обнаружено на странице портала.",
    State.ERROR: "Техническая ошибка. Проверьте интерфейс add-on.",
}


class Notifications:
    def __init__(self, settings, storage):
        self.settings = settings
        self.storage = storage
        self.queue = asyncio.Queue(maxsize=30)
        self.worker = None

    def start(self):
        self.worker = asyncio.create_task(self._worker())

    def emit(self, profile, state, *, code=None):
        text = (f"{profile.label if profile else 'Система'} · {state.value}\n"
                f"{utcnow().astimezone(ZoneInfo('Europe/Madrid')).strftime('%d.%m.%Y %H:%M:%S %Z')}\n"
                f"{MESSAGES.get(state, 'Проверьте состояние системы.')}"
                + ("\nВремя ожидания истекает; доступность места не гарантируется." if code == "SESSION_WARNING" else ""))
        try:
            self.queue.put_nowait((profile.id if profile else None, text))
        except asyncio.QueueFull:
            self.storage.event(None, "NOTIFICATION_QUEUE_FULL")

    async def _worker(self):
        while True:
            profile, text = await self.queue.get()
            try:
                await self.send(profile, text)
            except Exception:
                self.storage.event(profile, "NOTIFICATION_WORKER_FAILED")
            finally:
                self.queue.task_done()

    async def send(self, profile, text):
        s = self.settings()
        tasks = []
        # Separate recipient requests, always from the configured allowlist.
        if s.telegram_enabled:
            for chat in s.telegram_chat_ids:
                if s.ha_telegram_action:
                    domain, action = s.ha_telegram_action.split(".")
                    payload = {"message": text, "target": [chat], "data": {
                        "inline_keyboard": [[f"Открыть текущую сессию:{s.ingress_url}"]]}}
                    tasks.append(("TELEGRAM", self.ha(f"services/{domain}/{action}", payload)))
                else:
                    payload = {"chat_id": chat, "text": text, "protect_content": True,
                               "reply_markup": {"inline_keyboard": [[{
                                   "text": "Открыть текущую сессию", "url": s.ingress_url}]]}}
                    tasks.append(("TELEGRAM", self.post(
                        f"https://api.telegram.org/bot{s.telegram_token}/sendMessage", payload)))
        if s.ha_notifications:
            message = text + (f"\n[Открыть текущую сессию]({s.ingress_url})" if s.ingress_url else "")
            tasks.append(("HA", self.ha("services/persistent_notification/create", {
                "title": "Barcelona TIE Cita", "message": message,
                "notification_id": f"barcelona_cita_{profile or 'system'}"})))
        if s.smtp_enabled:
            tasks.append(("SMTP", asyncio.to_thread(self.smtp, s, text)))
        results = await asyncio.gather(*(c for _, c in tasks), return_exceptions=True)
        for (channel, _), result in zip(tasks, results):
            self.storage.event(profile, f"NOTIFY_{channel}_{'FAILED' if isinstance(result, BaseException) else 'SENT'}")

    @staticmethod
    async def post(url, payload, *, headers=None, internal=False):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=not internal) as session:
            async with session.post(url, json=payload, headers=headers, allow_redirects=False) as response:
                if response.status >= 300:
                    # Never include URLs, tokens, bodies or server exceptions in diagnostics.
                    raise RuntimeError("NOTIFICATION_DELIVERY_FAILED")
                if not internal:
                    data = await response.json()
                    if not data.get("ok"):
                        raise RuntimeError("TELEGRAM_REJECTED")

    async def ha(self, path, payload):
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise RuntimeError("HA_API_UNAVAILABLE")
        await self.post(f"http://supervisor/core/api/{path}", payload,
                        headers={"Authorization": f"Bearer {token}"}, internal=True)

    @staticmethod
    def smtp(s, text):
        message = EmailMessage()
        message["Subject"] = "Barcelona TIE Cita — уведомление"
        message["From"] = s.smtp_sender
        message["To"] = "undisclosed-recipients:;"
        message.set_content(text + (f"\nОткрыть текущую сессию: {s.ingress_url}" if s.ingress_url else ""))
        context = ssl.create_default_context()
        if s.smtp_starttls:
            connection = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=12)
        else:
            connection = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=12, context=context)
        with connection as smtp:
            if s.smtp_starttls:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(message, to_addrs=s.smtp_recipients)

    async def sensors(self, status):
        if not self.settings().ha_sensors or not os.environ.get("SUPERVISOR_TOKEN"):
            return
        values = {
            "sensor.barcelona_cita_status": (status["state"], {}),
            "sensor.barcelona_cita_last_check": (status["last_check"] or "unknown", {"device_class": "timestamp"}),
            "sensor.barcelona_cita_next_check": (status["next_check"] or "unknown", {"device_class": "timestamp"}),
            "sensor.barcelona_cita_checks_today": (str(status["checks_today"]), {}),
            "binary_sensor.barcelona_cita_attention_required": (
                "on" if State(status["state"]) in ATTENTION else "off", {"device_class": "problem"}),
        }
        results = await asyncio.gather(*(self.ha(f"states/{entity}", {"state": value, "attributes": attrs})
                                         for entity, (value, attrs) in values.items()), return_exceptions=True)
        if any(isinstance(r, BaseException) for r in results):
            self.storage.event(None, "HA_SENSORS_FAILED")

    async def close(self):
        if self.worker:
            try:
                await asyncio.wait_for(self.queue.join(), 15)
            except TimeoutError:
                pass
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
