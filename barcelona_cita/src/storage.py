import json
import os
import sqlite3
from pathlib import Path

from .models import Applicant, Settings, utcnow


class Storage:
    """Local private store. Events accept only internal codes, never HTML/exceptions."""

    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / "cita.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, profile INTEGER, code TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY, profile INTEGER NOT NULL UNIQUE, at TEXT NOT NULL,
                appointment TEXT NOT NULL, office TEXT NOT NULL, reference TEXT NOT NULL);
        """)
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def profiles(self):
        raw = self.get("profiles")
        return [Applicant.model_validate(p) for p in raw] if raw else [
            Applicant(id=1, label="Антон"), Applicant(id=2, label="Наталья")]

    def save_profiles(self, profiles):
        self.put("profiles", [p.model_dump(mode="json") for p in profiles])

    def settings(self):
        return Settings.model_validate(self.get("settings", {}))

    def event(self, profile, code):
        import re
        if not re.fullmatch(r"[A-Z_]{2,70}", code):
            raise ValueError("Only internal event codes may be stored")
        self.db.execute("INSERT INTO events(at,profile,code) VALUES (?,?,?)", (utcnow().isoformat(), profile, code))
        self.db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)")
        self.db.commit()

    def events(self):
        return [dict(zip(("at", "profile", "code"), r)) for r in self.db.execute(
            "SELECT at,profile,code FROM events ORDER BY id DESC LIMIT 100")]

    def book(self, profile, appointment, office, reference):
        # Ledger is authoritative even if a power failure precedes save_profiles().
        self.db.execute("INSERT INTO bookings(profile,at,appointment,office,reference) VALUES (?,?,?,?,?)",
                        (profile, utcnow().isoformat(), appointment, office, reference))
        self.db.commit()

    def bookings(self):
        return [dict(zip(("profile", "at", "appointment", "office"), r)) for r in self.db.execute(
            "SELECT profile,at,appointment,office FROM bookings ORDER BY id DESC")]

    def close(self):
        self.db.close()
