"""Asker-facing canvas — a clickable surface beside the chat, not instead of it.

WHY IT EXISTS

Chat is the wrong shape for a closed set of choices. Picking one of five pizzas and one of
twenty delivery slots is mechanical: typing it out is slower than clicking, and it invites the
misunderstandings ("large? which size is that?") the agent then has to resolve. The chat stays
open the whole time — this is an alternative input, not a replacement.

The same asymmetry decides when to render a canvas at all: **closed, mechanical choices get a
canvas; anything needing discussion stays in chat.**

WHAT IT IS NOT

- It is NOT new authority. Submitting runs the same path an order typed in chat runs —
  `capabilities.check_bounds` then `schedule.book` — so the bounds the owner declared still
  decide, in code. A form cannot book what a sentence could not.
- It does NOT take payment. That would be a genuinely new authority and needs its own bound.

ISOLATION

This module is external-facing, so it must never import `tools` (the owner registry that can
grant folders and send as the owner). It reads only `capabilities`, `schedule` and the public
knowledge file, and its one write goes through the capability path.

Honest limit: that is MODULE-level isolation, not process-level — the canvas is served by the
same daemon as the owner site, as the simulator already is. What protects the owner surface is
its separate token, not a separate process.

ONE SOURCE OF TRUTH FOR PRICES

The menu is parsed from the same document the chat answers from, located by NAME from the
capability it serves (`pizza_delivery` -> `pizza-delivery.md`). A second structured copy would
have been easier and would have drifted the first time a price changed — the agent quoting $24
while the form charges $26 is exactly the class of bug this project keeps finding.
"""
from __future__ import annotations

import json
import pathlib
import re
import secrets
from datetime import datetime, timedelta

from . import capabilities
from . import paths
from . import schedule

TOKENS = paths.RUN / "canvas.json"


def doc_for(capability: str) -> pathlib.Path:
    """`pizza_delivery` -> `knowledge/public/pizza-delivery.md`.

    Duplicated from tools.doc_for rather than imported: this module must not import the owner
    surface, and one naming rule is not worth breaking that isolation for.
    """
    return paths.KNOWLEDGE / (capability.strip().lower().replace("_", "-") + ".md")


# ---- who is this canvas for -------------------------------------------------

def _tokens() -> dict:
    try:
        return json.loads(TOKENS.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def link_for(asker: str, verified: bool, capability: str = "pizza_delivery") -> str:
    """Issue (or reuse) a canvas token for one identity, and return its path.

    Scoped per identity so a link cannot be handed around to act as someone else, and
    unguessable so possession of the URL is the only access — the asker has no owner token
    and must never need one.
    """
    data = _tokens()
    for tok, rec in data.items():
        if rec.get("asker") == asker and rec.get("capability") == capability:
            return f"/c/{tok}"
    tok = secrets.token_urlsafe(12)
    data[tok] = {"asker": asker, "verified": bool(verified), "capability": capability,
                 "issued": datetime.now().isoformat(timespec="seconds")}
    TOKENS.parent.mkdir(parents=True, exist_ok=True)
    TOKENS.write_text(json.dumps(data, indent=2))
    return f"/c/{tok}"


def holder(token: str) -> dict | None:
    return _tokens().get(token or "")


def describe(token: str) -> dict | None:
    """What to call this canvas, for a client that shows several of them at once.

    The label comes from the capability's own `canvas_label`, not from this module. A second
    canvas use-case must be nameable in config — hardcoding "Order" here would make the tab
    strip pizza-specific, which is the exact coupling this file exists to avoid.
    """
    rec = holder(token)
    if not rec:
        return None
    name = rec.get("capability", "")
    cap = capabilities.get(name) or {}
    label = cap.get("canvas_label") or name.replace("_", " ").capitalize() or "Panel"
    return {"capability": name, "label": label}


# ---- the menu, from the document the chat also answers from -----------------

_ROW = re.compile(r"^\|(?P<name>[^|]+)\|(?P<med>[^|]*)\|(?P<lge>[^|]*)\|\s*$")
_PRICE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")


def advertised(asker: str, verified: bool) -> list[dict]:
    """Capabilities to offer up front, as [{capability, label, url}].

    Discovery: an external party does not know what the owner does, and until now the ordering
    page was only mentioned if the agent happened to ask for a missing detail — so finding it was
    accidental. A standing entry answers "what can I do here?" without a round trip.

    Opt-in via `canvas_label`. NOT every declared capability should be advertised to everyone —
    "approve a refund" is a capability too — and a label is the owner saying "this has a surface
    people are meant to use". Absent it, the capability still works, it just is not offered.

    Eligibility is deliberately NOT filtered on `verified_only`: browsing is disclosure, acting
    is action. An unverified visitor sees the page and meets the requirement at the point of
    commitment, which is the funnel working rather than a wasted click.
    """
    out = []
    for name, cap in (capabilities.all_capabilities() or {}).items():
        label = (cap or {}).get("canvas_label")
        if not label:
            continue
        out.append({"capability": name, "label": label,
                    "url": link_for(asker, verified, name)})
    return out


def page_for(capability: str) -> pathlib.Path:
    """The HTML surface for a capability: its own if it has one, else the generic fallback.

    The shape of the page follows the capability — a menu with sizes and prices looks nothing
    like a callback request — so a custom page is instance data, named after the capability like
    its document. The framework's fallback names no domain at all; it previously shipped the
    pizzeria's page as the only canvas, which would have handed every new owner a menu.
    """
    own = paths.CANVAS / (capability.strip().lower().replace("_", "-") + ".html")
    return own if own.is_file() else paths.INSTALL / "canvas-default.html"


def menu(capability: str = "pizza_delivery") -> dict:
    """{"items": [{name, note, sizes:{label: price}}], "extras": [...], "hours": str}.

    Parses the pipe table under "## Pizzas". Deliberately tolerant: a row it cannot read is
    skipped rather than raising, because a malformed menu should degrade to fewer options, not
    take the ordering page down.
    """
    items, extras = [], []
    # The bounds come from the capability, the items from its document. A capability with no
    # document still has limits, and the generic page shows them — so bail out of the PARSING,
    # not out of the function, or a document-less capability renders as "no limits declared".
    try:
        lines = doc_for(capability).read_text().splitlines()
    except OSError:
        lines = []

    section = ""
    for line in lines:
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        m = _ROW.match(line)
        if not m or section != "pizzas":
            continue
        name = m.group("name").strip()
        if not name or name.lower().startswith("pizza") or set(name) <= set("-: "):
            continue                                   # header or separator row
        med, lge = _PRICE.search(m.group("med")), _PRICE.search(m.group("lge"))
        if not (med or lge):
            continue
        label, _, note = name.partition("—")
        sizes = {}
        if med:
            sizes["Medium"] = float(med.group(1))
        if lge:
            sizes["Large"] = float(lge.group(1))
        items.append({"name": label.strip(), "note": note.strip(), "sizes": sizes})

    # Extras are prose, not a table — surfaced as text so the page can show them without
    # pretending they are selectable.
    for line in lines:
        if "+$" in line and ("gluten" in line.lower() or "vegan" in line.lower()):
            extras.append(re.sub(r"\*+", "", line).strip("- ").strip())

    cap = capabilities.get(capability) or {}
    return {"items": items, "extras": extras,
            "hours": str((cap.get("bounds") or {}).get("hours", "")),
            "max_quantity": (cap.get("bounds") or {}).get("max_quantity")}


# ---- slots, from the same bounds and the same schedule ---------------------

def slots(capability: str = "pizza_delivery", days: int = 2) -> list[dict]:
    """Bookable start times, with the taken ones marked rather than hidden.

    Showing a slot as taken is better than omitting it: "why can't I have 7pm" is answered on
    the page instead of becoming a chat message. Availability comes from `schedule`, so what
    the canvas offers and what the agent books can never disagree.
    """
    cap = capabilities.get(capability) or {}
    bounds = cap.get("bounds") or {}
    block = int(bounds.get("block_minutes") or 30)
    hours = str(bounds.get("hours") or "")
    out = []
    now = datetime.now()
    for day in range(days):
        base = (now + timedelta(days=day)).replace(second=0, microsecond=0)
        for step in range(0, 24 * 60, block):
            when = base.replace(hour=0, minute=0) + timedelta(minutes=step)
            if when < now:
                continue                                # no booking the past
            if hours and not schedule.within_hours(when, block, hours):
                continue
            out.append({"at": when.isoformat(timespec="minutes"),
                        "day": when.strftime("%a %d %b"),
                        "time": when.strftime("%H:%M"),
                        "free": not schedule.conflicts(when, block)})
    return out


# ---- the one write ---------------------------------------------------------

def submit(token: str, lines: list[dict], at: str) -> dict:
    """Place an order. Returns {ok, message, booking?}.

    Runs exactly the checks a typed order runs. If the canvas could bypass `check_bounds`, the
    bounds would be decoration — so this function deliberately has no way to book without them.
    """
    rec = holder(token)
    if not rec:
        return {"ok": False, "message": "This ordering link is not valid."}
    cap_name = rec.get("capability") or "pizza_delivery"
    quantity = sum(int(l.get("qty") or 0) for l in lines)
    if quantity < 1:
        return {"ok": False, "message": "Nothing selected yet."}
    if not at:
        return {"ok": False, "message": "Pick a delivery time."}

    minutes = capabilities.block_minutes(cap_name)
    ok, why = capabilities.check_bounds(cap_name, rec.get("verified", False),
                                        quantity=quantity, at=at, minutes=minutes)
    if not ok:
        return {"ok": False, "message": why}

    what = ", ".join(f"{int(l['qty'])}x {l.get('size','')} {l.get('name','')}".strip()
                     for l in lines if int(l.get("qty") or 0) > 0)
    try:
        row = schedule.book(at, minutes, what, rec["asker"])
    except schedule.Conflict:
        nxt = schedule.next_free(at, minutes,
                                 str((capabilities.get(cap_name) or {})
                                     .get("bounds", {}).get("hours", "")))
        return {"ok": False, "message": "That slot was just taken."
                + (f" The next free one is {nxt.replace('T', ' ')}." if nxt else "")}
    return {"ok": True, "booking": row,
            "message": f"Confirmed — {what} at {row['at'][11:16]} "
                       f"on {row['at'][:10]}."}
