"""The name a person is shown by — one answer for the hub and the assistant (2026-09-29).

WHICH NAME WINS, in order:

1. **What the owner typed** (`names.json`). Their choice, so nothing overrides it.
2. **The owner's Contacts** on a Mac, as the Swift shell reports it (`run/contacts.json`). That
   is every account macOS syncs — iCloud, a Google account added under Internet Accounts,
   Exchange — so Google contacts arrive here without a Google token.
3. **What arrived with a message** (`run/sessions.json`, `display`) — a WhatsApp profile name.
4. Nothing: the caller shows by their number.

A NAME IS A LABEL, NEVER A KEY. Files, permissions and `people/` all stay keyed on the number
or account uid; a name decides only what is displayed and what a search matches.

THE ADDRESS BOOK DOES NOT LAND ON DISK. The daemon writes the numbers it wants names for
(`run/contacts-wanted.json`) and the shell writes back names for THOSE numbers only. So what is
stored is who has already called or written — which this app holds anyway — and not a copy of
the owner's contacts.
"""
from __future__ import annotations

import json
import re

from . import paths

#: What the owner typed, keyed by identity. Owner data, beside settings.md — not run state.
TYPED = paths.HOME / "names.json"
#: Written by the shell: {"access": ..., "at": ..., "names": {identity: name}}.
CONTACTS = paths.RUN / "contacts.json"
#: Written here: the identities the shell should look up.
WANTED = paths.RUN / "contacts-wanted.json"

#: A phone number as a caller or a WhatsApp sender arrives: "+6596918851" or "6596918851".
#: Account uids (DDUET) never match, so they are never sent to be looked up.
_NUMBER = re.compile(r"^\+?\d{6,15}$")

#: A name longer than this is not a name, and would break the list's layout.
MAX_NAME = 60


def _load(path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def typed() -> dict:
    return {k: v for k, v in _load(TYPED).items() if isinstance(v, str) and v.strip()}


def set_typed(who: str, name: str) -> str:
    """Save the owner's name for `who`; empty removes it. Returns the name kept."""
    name = " ".join((name or "").split())[:MAX_NAME]
    d = typed()
    if name:
        d[who] = name
    else:
        d.pop(who, None)
    TYPED.parent.mkdir(parents=True, exist_ok=True)
    TYPED.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    return name


def contacts_state() -> dict:
    """What the shell last reported. Empty when there is no shell (a browser, Linux, Windows)."""
    return _load(CONTACTS)


def from_contacts() -> dict:
    names = contacts_state().get("names")
    return names if isinstance(names, dict) else {}


def _sessions() -> dict:
    return _load(paths.RUN / "sessions.json")


def name_for(who: str, seen: dict | None = None) -> str:
    """The best name for `who`, or "" when there is none."""
    if not who:
        return ""
    for source in (typed(), from_contacts()):
        n = source.get(who)
        if isinstance(n, str) and n.strip():
            return n.strip()
    seen = _sessions() if seen is None else seen
    return ((seen.get(who) or {}).get("display") or "").strip()


def source_of(who: str) -> str:
    """"typed", "contacts", "message" or "" — which of the four the name came from."""
    if who in typed():
        return "typed"
    if (from_contacts().get(who) or "").strip():
        return "contacts"
    if ((_sessions().get(who) or {}).get("display") or "").strip():
        return "message"
    return ""


def display(who: str, seen: dict | None = None) -> str:
    """The name, or the identity itself when there is none."""
    return name_for(who, seen) or who


def is_number(who: str) -> bool:
    return bool(_NUMBER.match(who or ""))


def want(identities) -> None:
    """Ask the shell for names for these numbers. Written only when the set changes, because
    the shell looks up again whenever the file does."""
    numbers = sorted({w for w in identities if is_number(w)})
    if _load_list(WANTED) == numbers:
        return
    WANTED.parent.mkdir(parents=True, exist_ok=True)
    WANTED.write_text(json.dumps(numbers), encoding="utf-8")


def _load_list(path) -> list:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return d if isinstance(d, list) else []
