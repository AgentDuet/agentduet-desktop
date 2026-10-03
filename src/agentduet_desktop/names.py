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
the owner's contacts. With each name come that card's email addresses, for `draft_email`,
and its identifier, for the hub's "Open in Contacts".
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


def prune_typed() -> None:
    """Drop a typed name Contacts now agrees with (2026-09-30).

    A rename written through to Contacts is typed here too, so the hub shows it at once. Once the
    shell reports the same name from Contacts, the typed copy has done its job — and kept, it would
    hide the NEXT edit the owner makes in Contacts itself, since a typed name wins.
    """
    d, book = typed(), from_contacts()
    same = [w for w, n in d.items() if (book.get(w) or "").strip() == n]
    if same:
        for w in same:
            d.pop(w, None)
        TYPED.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")


def contacts_state() -> dict:
    """What the shell last reported. Empty when there is no shell (a browser, Linux, Windows)."""
    return _load(CONTACTS)


def from_contacts() -> dict:
    names = contacts_state().get("names")
    return names if isinstance(names, dict) else {}


def contact(who: str) -> dict:
    """The Contacts card the shell matched to `who`: {name, emails, id}, or {} for none."""
    people = contacts_state().get("people")
    row = people.get(who) if isinstance(people, dict) else None
    return row if isinstance(row, dict) else {}


def resolve(who: str) -> str:
    """The identity `who` means — a number, or a name the hub shows — or "" when it is not ONE
    person. A number is itself. A name must fit exactly one person who has called or written,
    by the whole name or by all of its words ("Kok Choong" for "Ong Kok Choong").
    """
    who = (who or "").strip()
    if not who or is_number(who):
        return who
    from . import calls
    identities = set(calls.people()) | set(typed())
    people = contacts_state().get("people")
    if isinstance(people, dict):
        identities |= set(people)
    want = " ".join(who.split()).lower()
    shown = {i: name_for(i).lower() for i in identities}
    exact = [i for i, n in shown.items() if n and n == want]
    if len(exact) == 1:
        return exact[0]
    words = set(want.split())
    loose = [i for i, n in shown.items() if n and words <= set(n.split())]
    return loose[0] if len(exact) == 0 and len(loose) == 1 else ""


def mentions(text: str, who: str) -> bool:
    """Whether `text` names this person — by the name the hub shows, or by their number."""
    text = (text or "").lower()
    digits = re.sub(r"\D", "", who)
    if len(digits) >= 7 and digits[-8:] in re.sub(r"\D", "", text):
        return True
    name = name_for(who).lower()
    return bool(name) and any(w in text.split() for w in name.split() if len(w) >= 3)


def emails_for(who: str) -> list[str]:
    """Email addresses Contacts holds for `who` — a number, or a name the hub shows.

    ONLY PEOPLE WHO HAVE CALLED OR WRITTEN, because only they are in `contacts.json` (see the
    module docstring): a name that matches nobody here answers [], not a search of the address
    book. A name that matches more than one person also answers [] — a draft to the wrong one of
    two people called Lee is worse than a draft with the address left for the owner to fill in.
    """
    who = (who or "").strip()
    if not who:
        return []
    if is_number(who):
        found = [who]
    else:
        people = contacts_state().get("people")
        identities = set(people) if isinstance(people, dict) else set()
        identities |= set(typed())
        want_name = " ".join(who.split()).lower()
        shown = {i: {name_for(i).lower(), (contact(i).get("name") or "").lower()} - {""}
                 for i in identities}
        found = [i for i, ns in shown.items() if want_name in ns]
        # "Kok Choong" for "Ong Kok Choong": every word given is a word of the name. Still one
        # person or none, below.
        if not found:
            words = set(want_name.split())
            found = [i for i, ns in shown.items() if any(words <= set(n.split()) for n in ns)]
    # One PERSON, counted by card: two numbers on the same card are still one person.
    if len({contact(i).get("id") or i for i in found}) != 1:
        return []
    return [e for e in contact(found[0]).get("emails") or [] if isinstance(e, str) and e.strip()]


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
