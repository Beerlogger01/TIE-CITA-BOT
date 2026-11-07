from datetime import date, datetime, timezone
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class State(StrEnum):
    IDLE = "IDLE"
    NAVIGATING = "NAVIGATING"
    CHECKING = "CHECKING"
    NO_SLOTS = "NO_SLOTS"
    POSSIBLE_SLOTS = "POSSIBLE_SLOTS"
    AWAITING_USER = "AWAITING_USER"
    BOOKING_IN_PROGRESS = "BOOKING_IN_PROGRESS"
    BOOKED = "BOOKED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


HELD = {State.POSSIBLE_SLOTS, State.AWAITING_USER, State.BOOKING_IN_PROGRESS,
        State.BLOCKED, State.SESSION_EXPIRED}
ATTENTION = HELD | {State.ERROR}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Applicant(Model):
    id: int = Field(ge=1, le=2)
    label: str = Field(max_length=30)
    enabled: bool = False
    full_name: str = Field(default="", max_length=120, repr=False)
    nie: str = Field(default="", max_length=12, repr=False)
    nationality: str = Field(default="RUSIA", min_length=2, max_length=60)
    email: str = Field(default="", max_length=180, repr=False)
    phone: str = Field(default="", max_length=25, repr=False)
    booked: bool = False
    status: State = State.IDLE
    last_check: datetime | None = None

    @model_validator(mode="after")
    def ready(self):
        import re
        if self.enabled and (not self.full_name or not re.fullmatch(r"[XYZxyz]\d{7}[A-Za-z]", self.nie)):
            raise ValueError("Для включённого профиля необходимы ФИО и NIE")
        # Labels are aliases, never free text from a sensitive form.
        self.label = {1: "Антон", 2: "Наталья"}[self.id]
        return self


class Settings(Model):
    automatic: bool = False
    interval_minutes: int = Field(default=15, ge=15, le=1440)
    thursday_enabled: bool = True
    priority_minutes: int = Field(default=15, ge=15, le=1440)
    morning_enabled: bool = False
    morning_minutes: int = Field(default=30, ge=15, le=1440)
    session_warning_minutes: int = Field(default=10, ge=2, le=120)
    session_expiry_minutes: int = Field(default=30, ge=5, le=240)
    memory_limit_mb: int = Field(default=1200, ge=500, le=2000)
    date_from: date | None = None
    date_to: date | None = None
    offices: list[str] = Field(default_factory=list, max_length=30)
    ingress_url: str = Field(default="", max_length=400)
    ha_notifications: bool = True
    ha_sensors: bool = True
    telegram_enabled: bool = False
    telegram_polling_enabled: bool = False
    telegram_token: str = Field(default="", max_length=200, repr=False)
    telegram_chat_ids: list[str] = Field(default_factory=list, max_length=10)
    ha_telegram_action: str = Field(default="", max_length=100)
    smtp_enabled: bool = False
    smtp_host: str = Field(default="", max_length=200)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_starttls: bool = False
    smtp_username: str = Field(default="", max_length=200, repr=False)
    smtp_password: str = Field(default="", max_length=200, repr=False)
    smtp_sender: str = Field(default="", max_length=200, repr=False)
    smtp_recipients: list[str] = Field(default_factory=list, max_length=10, repr=False)
    debug: bool = False
    manual_confirmation: bool = True

    @field_validator("ingress_url")
    @classmethod
    def safe_link(cls, v):
        import re
        if not v:
            return v
        u = urlsplit(v)
        if (u.scheme != "https" or not u.hostname or u.username or u.password
                or u.query or u.fragment or not re.fullmatch(r"/hassio/ingress/[a-zA-Z0-9_-]+/?", u.path)):
            raise ValueError("Нужен HTTPS URL Home Assistant /hassio/ingress/<slug>, без токенов")
        return v.rstrip("/")

    @field_validator("telegram_chat_ids")
    @classmethod
    def chat_ids(cls, v):
        import re
        if any(not re.fullmatch(r"-?\d{1,20}", x) for x in v):
            raise ValueError("Укажите числовые ID разрешённых Telegram чатов")
        return list(dict.fromkeys(v))

    @field_validator("ha_telegram_action")
    @classmethod
    def action(cls, v):
        import re
        if v and not re.fullmatch(r"notify\.[a-z0-9_]+", v):
            raise ValueError("Допустимо только notify.<имя>")
        return v

    @model_validator(mode="after")
    def consistent(self):
        if not self.manual_confirmation:
            raise ValueError("Автоматическое окончательное подтверждение в v0.2 отключено")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("Неверный диапазон дат")
        if self.session_warning_minutes >= self.session_expiry_minutes:
            raise ValueError("Предупреждение должно предшествовать истечению")
        if self.telegram_enabled and (not self.ingress_url or not (self.telegram_token or self.ha_telegram_action)):
            raise ValueError("Для Telegram нужны HTTPS ссылка и token либо HA notify")
        if self.telegram_polling_enabled and (not self.telegram_enabled or not self.telegram_token or self.ha_telegram_action):
            raise ValueError("Приём команд требует включённого прямого Telegram token, без HA notify")
        if self.smtp_enabled and (not self.smtp_host or not self.smtp_sender or not self.smtp_recipients):
            raise ValueError("Для SMTP нужны сервер, отправитель и получатели")
        if any("\n" in x or "\r" in x for x in [self.smtp_sender, *self.smtp_recipients]):
            raise ValueError("Неверный адрес email")
        return self
