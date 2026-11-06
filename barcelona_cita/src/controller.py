import asyncio
from datetime import datetime, timedelta

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .browser import memory_mb
from .models import ATTENTION, HELD, Applicant, Settings, State, utcnow
from .portal import Portal
from .scheduler import MADRID, next_check
from .state_machine import validate_transition


class Conflict(Exception):
    pass


class Controller:
    def __init__(self, storage, browser, notifications, *, portal_factory=Portal, clock=utcnow, memory=memory_mb):
        self.storage, self.browser, self.notifications = storage, browser, notifications
        self.portal_factory, self.clock, self.memory = portal_factory, clock, memory
        self.settings = storage.settings()
        self.profiles = storage.profiles()
        for booking in storage.bookings():
            p = self.profiles[booking["profile"] - 1]
            p.booked = True
            p.status = State.BOOKED
        runtime = storage.get("runtime", {})
        self.monitoring = runtime.get("monitoring", False)
        self.paused = runtime.get("paused", False)
        self.active_id = runtime.get("active_id")
        self.last_id = runtime.get("last_id", 0)
        self.last_check = datetime.fromisoformat(runtime["last_check"]) if runtime.get("last_check") else None
        self.count_day = runtime.get("count_day", "")
        self.checks_today = runtime.get("checks_today", 0)
        self.failures = runtime.get("failures", 0)
        previous = State(runtime.get("state", "IDLE"))
        self.state = State.IDLE
        self.last_response = "READY"
        if previous in ATTENTION | {State.NAVIGATING, State.CHECKING}:
            self.state = State.BLOCKED if previous == State.BLOCKED else State.SESSION_EXPIRED
            self.paused = True
            self.last_response = "RESTART_SESSION_LOST"
        self.check_task = self.loop_task = None
        self.lock = asyncio.Lock()
        self.human = False
        self.viewers = 0
        self.hold_since = None
        self.warned = False
        self.possible_available = False
        self.stage = "READY"
        self.last_notification_test = None
        self._last_inspect = self._last_sensors = self.clock() - timedelta(minutes=1)
        self.persist()

    @property
    def active(self):
        return next((p for p in self.profiles if p.id == self.active_id), None)

    @property
    def busy(self):
        return self.check_task is not None and not self.check_task.done()

    def persist(self):
        self.storage.save_profiles(self.profiles)
        self.storage.put("runtime", {"state": self.state.value, "monitoring": self.monitoring,
            "paused": self.paused, "active_id": self.active_id, "last_id": self.last_id,
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "count_day": self.count_day, "checks_today": self.checks_today, "failures": self.failures})

    def transition(self, state, code, *, notice=True):
        validate_transition(self.state, state)
        self.state, self.last_response = state, code
        if self.active:
            self.active.status = state
        self.storage.event(self.active_id, code)
        self.persist()
        if notice and (state in ATTENTION or state == State.BOOKED):
            self.notifications.emit(self.active, state)

    def status(self):
        now = self.clock()
        if self.count_day != now.astimezone(MADRID).date().isoformat():
            self.checks_today = 0
        eligible = any(p.enabled and not p.booked for p in self.profiles)
        scheduled = (self.monitoring and self.settings.automatic and not self.paused and
                     not self.busy and self.state not in HELD and not self.browser.alive and eligible)
        allowed = next_check(self.settings, self.last_check, now, self.failures)
        return {"state": self.state.value, "monitoring": self.monitoring, "paused": self.paused,
            "busy": self.busy, "stage": self.stage, "automatic": self.settings.automatic,
            "next_allowed_check": allowed.isoformat(),
            "active_profile": self.active.label if self.active else None, "active_id": self.active_id,
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "next_check": next_check(self.settings, self.last_check, now, self.failures).isoformat() if scheduled else None,
            "checks_today": self.checks_today, "last_response": self.last_response,
            "browser_alive": self.browser.alive, "human_control": self.human, "viewers": self.viewers,
            "possible_slots": self.possible_available, "session_held_since": self.hold_since.isoformat() if self.hold_since else None,
            "memory_mb": self.memory(), "profiles": [{"id": p.id, "label": p.label, "enabled": p.enabled,
                "booked": p.booked, "status": p.status.value,
                "last_check": p.last_check.isoformat() if p.last_check else None} for p in self.profiles],
            "bookings": self.storage.bookings(), "events": self.storage.events()}

    def select(self, profile_id=None):
        eligible = [p for p in self.profiles if p.enabled and not p.booked]
        if profile_id is not None:
            eligible = [p for p in eligible if p.id == profile_id]
        if not eligible:
            raise Conflict("Нет включённых незаписанных заявителей")
        return next((p for p in eligible if p.id > self.last_id), eligible[0])

    async def request_check(self, profile_id=None):
        # No await between guard and task reservation: one event loop, no overlapping checks.
        if self.busy or self.lock.locked() or self.browser.alive or self.viewers:
            raise Conflict("Проверка или браузерная сессия уже активна")
        if self.paused or self.state in HELD or self.human:
            raise Conflict("Сначала явно завершите ручную сессию или снимите паузу")
        now = self.clock()
        if self.last_check and now < next_check(self.settings, self.last_check, now, self.failures):
            raise Conflict("Минимальный интервал или пауза после ошибки ещё не истекли")
        if self.memory() >= self.settings.memory_limit_mb:
            raise Conflict("Недостаточно свободного бюджета памяти контейнера")
        profile = self.select(profile_id)
        self.check_task = asyncio.create_task(self._check(profile))

    def progress(self, code):
        self.stage = code
        self.storage.event(self.active_id, code)

    async def _check(self, profile):
        async with self.lock:
            self.active_id = self.last_id = profile.id
            now = self.clock()
            today = now.astimezone(MADRID).date().isoformat()
            if self.count_day != today:
                self.checks_today = 0
                self.count_day = today
            self.checks_today += 1
            self.last_check = profile.last_check = now
            self.transition(State.NAVIGATING, "CHECK_STARTED", notice=False)
            try:
                async with asyncio.timeout(150):
                    self.progress("BROWSER_STARTING")
                    await self.browser.start()
                    portal = self.portal_factory(self.browser)
                    portal.progress = self.progress
                    result = await portal.run(profile, lambda: self.transition(State.CHECKING, "REQUEST_RESULT", notice=False))
                self.stage = "CHECK_FINISHED"
                if result.state == State.NO_SLOTS:
                    self.failures = 0
                    self.transition(result.state, result.code, notice=False)
                    await self.browser.close()
                else:
                    # Stop all scheduling BEFORE queuing the first notification.
                    self.paused = True
                    self.possible_available = result.state == State.POSSIBLE_SLOTS
                    self.hold_since = self.clock()
                    self.failures += result.state == State.ERROR
                    self.transition(result.state, result.code)
            except asyncio.CancelledError:
                # Handoff cancels automation, never browser/context/page.
                raise
            except (TimeoutError, PlaywrightError, OSError) as exc:
                self.failures += 1
                self.paused = self.browser.alive
                if self.browser.alive:
                    self.hold_since = self.clock()
                code = "BROWSER_START_FAILED" if self.stage == "BROWSER_STARTING" else "NETWORK_OR_BROWSER_ERROR"
                if self.stage == "PORTAL_OPENING":
                    if isinstance(exc, (TimeoutError, PlaywrightTimeout)):
                        code = "PORTAL_LOAD_TIMEOUT"
                    elif "ERR_NAME_NOT_RESOLVED" in str(exc):
                        code = "PORTAL_DNS_ERROR"
                    elif "ERR_CERT_" in str(exc):
                        code = "PORTAL_TLS_ERROR"
                    elif "ERR_CONNECTION_" in str(exc):
                        code = "PORTAL_CONNECTION_ERROR"
                self.transition(State.ERROR, code)
            except Exception:
                self.failures += 1
                self.paused = True
                self.transition(State.ERROR, "INTERNAL_ERROR")
            finally:
                self.persist()

    async def takeover(self, *, interrupt=True):
        if self.busy and not interrupt:
            raise Conflict("Проверка ещё выполняется. Дождитесь результата; открытие страницы просмотра не останавливает её.")
        self.paused = True
        self.human = True
        # Also handles a check task reserved but not started yet.
        if self.busy:
            self.check_task.cancel()
            await asyncio.gather(self.check_task, return_exceptions=True)
        if not self.browser.alive:
            self.human = False
            self.persist()
            raise Conflict("Активной браузерной сессии нет")
        if self.state == State.BLOCKED:
            self.persist()
            raise Conflict("Портал заблокировал доступ; автоматическое продолжение запрещено")
        if self.state != State.BOOKED:
            target = State.AWAITING_USER if self.state in {State.NAVIGATING, State.CHECKING} else State.BOOKING_IN_PROGRESS
            self.transition(target, "USER_TAKEOVER", notice=False)
        if self.hold_since is None:
            self.hold_since = self.clock()
        self.persist()

    async def command(self, action, data=None):
        data = data or {}
        if action == "test_notifications":
            if not (self.settings.telegram_enabled or self.settings.smtp_enabled or self.settings.ha_notifications):
                raise Conflict("Сначала включите и сохраните хотя бы один канал уведомлений")
            if self.last_notification_test and self.clock() - self.last_notification_test < timedelta(seconds=60):
                raise Conflict("Повторная проверка уведомлений доступна через минуту")
            self.last_notification_test = self.clock()
            self.notifications.emit(None, State.IDLE, code="NOTIFICATION_TEST")
            self.storage.event(None, "NOTIFICATION_TEST_QUEUED")
            return
        if action == "check":
            await self.request_check(data.get("profile_id"))
            return
        if action in ("stop", "pause"):
            self.paused = True
            if action == "stop":
                self.monitoring = False
            if self.busy:
                self.check_task.cancel()
                await asyncio.gather(self.check_task, return_exceptions=True)
                if self.browser.alive:
                    self.hold_since = self.clock()
                    self.transition(State.AWAITING_USER, "USER_PAUSED_CHECK", notice=False)
        elif action in ("start", "resume"):
            if self.browser.alive or self.state in HELD or self.viewers:
                raise Conflict("Сначала завершите текущую сессию; она не будет перезагружена")
            self.monitoring, self.paused, self.human = True, False, False
        elif action == "discard":
            if self.busy or self.viewers:
                raise Conflict("Остановите проверку и отключите noVNC перед закрытием сессии")
            await self.browser.close()
            self.human = self.possible_available = False
            self.hold_since = None
            self.warned = False
            self.paused = True
            if self.state != State.IDLE:
                # Explicitly abandoning a page is the only reset of a held session.
                self.state = State.IDLE
                self.last_response = "SESSION_DISCARDED_BY_USER"
                self.storage.event(self.active_id, self.last_response)
        elif action == "reset":
            if self.browser.alive or self.busy or self.viewers:
                raise Conflict("Сначала явно завершите текущую сессию")
            self.state = State.IDLE
            self.paused = True
            self.human = False
            self.last_response = "ERROR_RESET_BY_USER"
            self.storage.event(self.active_id, self.last_response)
        elif action == "select":
            if self.busy or self.browser.alive:
                raise Conflict("Нельзя переключить заявителя во время процедуры")
            self.active_id = self.select(data.get("profile_id")).id
            # Honour explicit selection on the next automatic check.
            self.last_id = self.active_id - 1
        elif action == "verify":
            await self.confirm(data)
        else:
            raise Conflict("Неизвестная команда")
        self.persist()

    async def confirm(self, data):
        from datetime import date
        if self.busy or not self.browser.alive or not self.active or self.active.booked or not self.human:
            raise Conflict("Подтверждение проверяется только после ручного оформления в текущей сессии")
        try:
            appointment = date.fromisoformat(data["appointment"])
            reference = str(data["reference"])
            office = str(data.get("office", ""))[:180]
        except (ValueError, KeyError):
            raise Conflict("Укажите дату и номер подтверждения портала") from None
        async with self.lock:
            verified = await self.portal_factory(self.browser).verify_confirmation(reference, appointment)
            if not verified:
                raise Conflict("В текущей странице нет проверяемого подтверждения с этим номером и датой")
            self.storage.book(self.active_id, appointment.isoformat(), office, reference)
            self.active.booked = True
            self.possible_available = False
            self.transition(State.BOOKED, "PORTAL_CONFIRMATION_VERIFIED")
        # Keep the confirmation page open until the user explicitly closes it.

    def save_settings(self, raw):
        if self.busy:
            raise Conflict("Настройки нельзя менять во время проверки")
        self.settings = Settings.model_validate(raw)
        self.storage.put("settings", self.settings.model_dump(mode="json"))

    def save_profiles(self, raw):
        if self.busy or self.browser.alive:
            raise Conflict("Профили нельзя менять во время браузерной сессии")
        profiles = [Applicant.model_validate(p) for p in raw]
        if len(profiles) != 2 or [p.id for p in profiles] != [1, 2]:
            raise Conflict("Требуются ровно два профиля с id 1 и 2")
        for old, new in zip(self.profiles, profiles):
            # Neither booking status nor last-check timestamps can be supplied through the form.
            new.booked, new.status, new.last_check = old.booked, old.status, old.last_check
        self.profiles = profiles
        self.persist()

    async def tick(self):
        now = self.clock()
        if self.browser.alive and not self.busy and self.state in HELD | {State.ERROR}:
            if self.hold_since:
                elapsed = now - self.hold_since
                if elapsed >= timedelta(minutes=self.settings.session_expiry_minutes) and self.state != State.SESSION_EXPIRED:
                    if self.state != State.BLOCKED:
                        self.possible_available = False
                        self.transition(State.SESSION_EXPIRED, "LOCAL_SESSION_FRESHNESS_EXPIRED")
                elif elapsed >= timedelta(minutes=self.settings.session_warning_minutes) and not self.warned:
                    self.warned = True
                    self.notifications.emit(self.active, self.state, code="SESSION_WARNING")
            if now - self._last_inspect >= timedelta(seconds=15) and self.state not in {State.BLOCKED, State.SESSION_EXPIRED}:
                self._last_inspect = now
                # DOM reads only: no reload, heartbeat, re-login or portal keepalive requests.
                result = await self.portal_factory(self.browser).inspect(after_request=self.possible_available)
                if result.state in {State.BLOCKED, State.SESSION_EXPIRED}:
                    self.possible_available = False
                    self.transition(result.state, result.code)
                elif self.possible_available and result.state in {State.NO_SLOTS, State.ERROR}:
                    self.possible_available = False
                    self.transition(State.AWAITING_USER, "AVAILABILITY_NO_LONGER_VISIBLE")
        elif not self.browser.alive and self.state in HELD | {State.ERROR} and self.state not in {State.BLOCKED, State.SESSION_EXPIRED} and self.hold_since:
            self.possible_available = False
            self.paused = True
            self.transition(State.SESSION_EXPIRED, "BROWSER_SESSION_LOST")
        if (self.monitoring and self.settings.automatic and not self.paused and not self.busy
                and not self.browser.alive and self.state not in HELD):
            if now >= next_check(self.settings, self.last_check, now, self.failures):
                try:
                    await self.request_check()
                except Conflict:
                    pass
        if now - self._last_sensors >= timedelta(seconds=60):
            self._last_sensors = now
            await self.notifications.sensors(self.status())

    async def run(self):
        if self.last_response == "RESTART_SESSION_LOST":
            self.notifications.emit(self.active, self.state)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.paused = True
                self.last_response = "SCHEDULER_PAUSED_ERROR"
                self.storage.event(None, self.last_response)
                self.persist()
                self.notifications.emit(self.active, State.ERROR)
            await asyncio.sleep(2)

    async def close(self):
        for task in (self.loop_task, self.check_task):
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.persist()  # Persist interrupted state, never cookies or a fictitious live session.
        await self.browser.close()
