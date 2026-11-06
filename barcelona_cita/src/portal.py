"""Only visible portal UI. Subsequent booking screens intentionally require a human."""
import asyncio
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeout

from .models import State

PORTAL_URL = "https://icp.administracionelectronica.gob.es/icpplus/index.html"
PROCEDURE = "POLICÍA – TOMA DE HUELLAS (EXPEDICIÓN DE TARJETA)"


def normalized(text):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                             if not unicodedata.combining(c)).split())


@dataclass(frozen=True)
class Result:
    state: State
    code: str


class Halt(Exception):
    def __init__(self, result):
        self.result = result


def classify(text, *, after_request=False, http_status=None, calendar=False, captcha=False):
    t = normalized(text)
    if http_status in (403, 429) or any(x in t for x in (
        "too many requests", "access denied", "acceso bloqueado", "acceso denegado",
        "actividad inusual", "limite de peticiones", "request rejected")):
        return Result(State.BLOCKED, "PORTAL_BLOCKED")
    if http_status and http_status >= 500:
        return Result(State.ERROR, "PORTAL_UNAVAILABLE")
    if any(x in t for x in ("sesion ha caducado", "sesion caducada", "sesion ha expirado",
                            "session expired", "sesion no es valida", "tiempo de sesion")):
        return Result(State.SESSION_EXPIRED, "PORTAL_SESSION_EXPIRED")
    if captcha or any(x in t for x in ("captcha", "verifique que es humano", "codigo enviado por sms",
                                       "introduzca el codigo de verificacion")):
        return Result(State.AWAITING_USER, "HUMAN_VERIFICATION_REQUIRED")
    if any(x in t for x in ("error interno", "error del servidor", "servicio no disponible",
                            "error inesperado", "se ha producido un error")):
        return Result(State.ERROR, "PORTAL_ERROR")
    if after_request and "en este momento no hay citas disponibles" in t:
        return Result(State.NO_SLOTS, "NO_SLOTS")
    if after_request and calendar:
        return Result(State.POSSIBLE_SLOTS, "POSSIBLE_SLOTS_UNRESERVED")
    return Result(State.AWAITING_USER, "UNKNOWN_PAGE")


class Portal:
    def __init__(self, browser, *, url=PORTAL_URL):
        self.browser = browser
        self.url = url

    @property
    def page(self):
        return self.browser.page

    async def text(self):
        return await self.page.locator("body").inner_text(timeout=12000)

    async def guard(self):
        current, expected = urlsplit(self.page.url), urlsplit(self.url)
        if ((current.scheme, current.hostname, current.port) != (expected.scheme, expected.hostname, expected.port)
                or self.browser.extra_tab_blocked):
            raise Halt(Result(State.AWAITING_USER, "UNEXPECTED_NAVIGATION"))
        captcha = await self.page.locator(
            'iframe[title*="CAPTCHA" i], iframe[src*="recaptcha"], input[name*="captcha" i]'
        ).count() > 0
        result = classify(await self.text(), http_status=self.browser.http_status, captcha=captcha)
        if result.code != "UNKNOWN_PAGE":
            raise Halt(result)

    async def visible(self, locator):
        found = []
        for item in await locator.all():
            if await item.is_visible():
                found.append(item)
        if len(found) != 1:
            raise Halt(Result(State.AWAITING_USER, "AMBIGUOUS_OR_MISSING_CONTROL"))
        return found[0]

    async def option_select(self, wanted, *, optional=False):
        """Match displayed option text, not unstable portal numeric identifiers."""
        matches = []
        for select in await self.page.locator("select").all():
            if not await select.is_visible():
                continue
            for option in await select.locator("option").all():
                text = normalized(await option.inner_text())
                if wanted(text):
                    matches.append((select, await option.get_attribute("value")))
        if not matches and optional:
            return False
        if len(matches) != 1 or matches[0][1] is None:
            raise Halt(Result(State.AWAITING_USER, "AMBIGUOUS_OR_MISSING_SELECTION"))
        await self.guard()
        await matches[0][0].select_option(value=matches[0][1])
        return True

    async def click(self, name, fallback=None):
        await self.guard()
        loc = self.page.get_by_role("button", name=name, exact=True).or_(
            self.page.get_by_role("link", name=name, exact=True))
        if fallback:
            loc = loc.or_(self.page.locator(fallback))
        item = await self.visible(loc)
        displayed = await item.get_attribute("value") if await item.evaluate("e => e.tagName === 'INPUT'") else await item.inner_text()
        if normalized(displayed or "") != normalized(name):
            raise Halt(Result(State.AWAITING_USER, "UNEXPECTED_CONTROL_LABEL"))
        before = await self.text()
        await item.click()
        for _ in range(60):
            try:
                if await self.text() != before:
                    break
            except PlaywrightTimeout:
                pass
            await asyncio.sleep(.2)
        else:
            await self.guard()
            raise Halt(Result(State.AWAITING_USER, "PAGE_DID_NOT_ADVANCE"))
        await self.guard()

    async def fill(self, label, fallback, value):
        await self.guard()
        text_controls = self.page.locator("input:not([type='radio']):not([type='checkbox']), textarea")
        loc = self.page.get_by_label(label).and_(text_controls).or_(self.page.locator(fallback))
        await (await self.visible(loc)).fill(value)

    async def run(self, applicant, checking):
        report = getattr(self, "progress", lambda code: None)
        try:
            report("PORTAL_OPENING")
            await self.page.goto(self.url, wait_until="domcontentloaded")
            await self.guard()
            report("PROVINCE_SELECTION")
            await self.option_select(lambda t: t == "barcelona")
            # Some versions display the procedure on the province screen, others after Aceptar.
            has_procedure = await self.option_select(lambda t: "toma de huellas" in t and
                                                      "expedicion de tarjeta" in t, optional=True)
            if not has_procedure:
                await self.click("Aceptar", "#btnAceptar")
            await self.option_select(lambda t: "cualquier oficina" in t, optional=True)
            report("PROCEDURE_SELECTION")
            if not has_procedure:
                await self.option_select(lambda t: "toma de huellas" in t and "expedicion de tarjeta" in t)
            await self.click("Aceptar", "#btnAceptar")
            await self.click("Presentación sin Cl@ve", "#btnEntrar")
            report("APPLICANT_FORM")
            # If the portal asks explicitly for document type, choose NIE, never impersonate Cl@ve.
            radio = self.page.get_by_role("radio", name=re.compile(r"^NIE$", re.I))
            if await radio.count() == 1 and await radio.is_visible():
                await radio.check()
            await self.fill(re.compile(r"^NIE|n[uú]mero de (?:documento|identidad)", re.I),
                            "#txtIdCitado, input[name='txtIdCitado']", applicant.nie)
            await self.fill(re.compile(r"nombre y apellidos", re.I),
                            "#txtDesCitado, input[name='txtDesCitado']", applicant.full_name)
            await self.option_select(lambda t: t == normalized(applicant.nationality))
            await self.click("Aceptar", "#btnEnviar")
            report("REQUESTING_APPOINTMENT")
            # Intermediate warnings (including no-slots text) are not availability results.
            await self.click("Solicitar Cita", "#btnEnviar")
            checking()
            return await self.inspect(after_request=True)
        except Halt as exc:
            return exc.result

    async def inspect(self, *, after_request=False):
        try:
            await self.guard()
        except Halt as exc:
            return exc.result
        # Only recognise a next-step control after Solicitar Cita, never just a word in a warning.
        calendars = self.page.locator(
            'input[type="date"], [role="grid"][aria-label*="fecha" i], '
            '#calendario, #idFecha, select[name="idSede"], select[name="sede"]')
        calendar = any([await x.is_visible() for x in await calendars.all()])
        return classify(await self.text(), after_request=after_request,
                        http_status=self.browser.http_status, calendar=calendar)

    async def verify_confirmation(self, reference, appointment):
        """Read-only evidence check after the human completes the portal's final action.

        No confirmation is inferred from a calendar. Unknown production layouts fail closed.
        The user transcribes the portal's reference/date, both must appear in its current DOM.
        """
        try:
            await self.guard()
        except Halt:
            return False
        if not re.fullmatch(r"[A-Za-z0-9/-]{6,80}", reference):
            return False
        text = normalized(await self.text())
        headings = [normalized(await h.inner_text()) for h in await self.page.locator("h1,h2,[role='heading']").all()
                    if await h.is_visible()]
        phrases = ("cita confirmada", "confirmacion de la cita", "justificante de cita", "justificante de cita previa")
        dates = (appointment.isoformat(), appointment.strftime("%d/%m/%Y"), appointment.strftime("%d-%m-%Y"))
        return (any(p in headings for p in phrases)
                and re.search(r"(?<![a-z0-9/-])" + re.escape(reference.casefold()) + r"(?![a-z0-9/-])", text) is not None
                and any(d in text for d in dates))
