"""Bookable time — the framework primitive any commitment capability needs.

Generic on purpose. Pizza delivery is what we happen to test with, but "hold an N-minute
block and refuse an overlap" is the same shape as a viewing, a callback, a service visit
or a fitting. Nothing here knows what is being booked; the caller passes a label.

The three-layer split this sits in (see README):

  base code  this file — the conflict rule
  config     the block length and opening hours, per capability
  state      the bookings themselves, below

Deliberately not a calendar. No recurrence, no timezones, no invitees: a POC needs to
answer one question — is this slot free — and to answer it the same way twice.
"""

import json
import pathlib
import uuid
from datetime import datetime, time, timedelta

from . import paths

STORE = paths.RUN / "schedule.json"


class Conflict(Exception):
    """Raised instead of double-booking. Carries what it clashed with so the caller can
    say *why* rather than just refusing."""

    def __init__(self, clashes: list[dict]):
        self.clashes = clashes
        super().__init__("; ".join(f"{c['at']} {c['what']}" for c in clashes))


def _load() -> list[dict]:
    if not STORE.exists():
        return []
    try:
        return json.loads(STORE.read_text())
    except json.JSONDecodeError:
        return []


def _save(rows: list[dict]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(rows, indent=2))


def _dt(at: str | datetime) -> datetime:
    """Parse the ISO form the model produces. Minute precision is all we keep — seconds
    in a delivery slot are noise, and dropping them makes equality behave."""
    d = at if isinstance(at, datetime) else datetime.fromisoformat(str(at))
    return d.replace(second=0, microsecond=0)


def overlaps(a_start: datetime, a_min: int, b_start: datetime, b_min: int) -> bool:
    """Half-open intervals: a slot ending at 19:00 does NOT clash with one starting then.
    Closed intervals would refuse back-to-back deliveries, which is the normal case."""
    return a_start < b_start + timedelta(minutes=b_min) and \
           b_start < a_start + timedelta(minutes=a_min)


def conflicts(at: str | datetime, minutes: int) -> list[dict]:
    """Existing bookings this slot would collide with. Empty means free."""
    start = _dt(at)
    return [r for r in _load()
            if overlaps(start, minutes, _dt(r["at"]), int(r["minutes"]))]


def within_hours(at: str | datetime, minutes: int, hours: str) -> bool:
    """Is the whole slot inside an "HH:MM-HH:MM" window?

    The END is checked too, not just the start: a 30-minute delivery accepted at 20:50
    against a 21:00 close would run past closing. Malformed windows return True rather
    than blocking — a typo'd bound should not silently refuse every order, and the owner
    sees the bound listed as-written.
    """
    try:
        lo_s, hi_s = hours.split("-")
        lo, hi = time.fromisoformat(lo_s.strip()), time.fromisoformat(hi_s.strip())
    except (ValueError, AttributeError):
        return True
    start = _dt(at)
    end = start + timedelta(minutes=minutes)
    return lo <= start.time() and end.time() <= hi and start.date() == end.date()


def book(at: str | datetime, minutes: int, what: str, who: str) -> dict:
    """Take the slot, or raise Conflict. The only writer.

    Re-checks conflicts immediately before writing rather than trusting an earlier
    `conflicts()` call: between the model deciding and this running, another booking may
    have landed. Cheap here, and it keeps the invariant in one place.
    """
    start = _dt(at)
    clash = conflicts(start, minutes)
    if clash:
        raise Conflict(clash)
    row = {"id": uuid.uuid4().hex[:8], "at": start.isoformat(timespec="minutes"),
           "minutes": int(minutes), "what": what, "who": who,
           "booked_at": datetime.now().isoformat(timespec="seconds")}
    _save(_load() + [row])
    return row


def next_free(at: str | datetime, minutes: int, hours: str = "", tries: int = 8) -> str:
    """The nearest later slot that fits, or "" if none within `tries` steps.

    Refusing without an alternative wastes a round trip — the person has to guess again.
    Steps by the block length, so suggestions land on the same grid as the bookings.
    """
    start = _dt(at)
    for i in range(1, tries + 1):
        cand = start + timedelta(minutes=minutes * i)
        if hours and not within_hours(cand, minutes, hours):
            continue
        if not conflicts(cand, minutes):
            return cand.isoformat(timespec="minutes")
    return ""


def bookings(day: str = "") -> list[dict]:
    """Everything booked, or just one YYYY-MM-DD. Oldest first, for the owner's view."""
    rows = sorted(_load(), key=lambda r: r["at"])
    return [r for r in rows if r["at"].startswith(day)] if day else rows


def cancel(booking_id: str) -> bool:
    rows = _load()
    keep = [r for r in rows if r["id"] != booking_id]
    if len(keep) == len(rows):
        return False
    _save(keep)
    return True
