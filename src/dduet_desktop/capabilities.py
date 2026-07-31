"""Bounded authority to ACT — declared by the owner, never inferred.

The disclosure/action seam says: folders can be granted, commitment cannot. That is right
as a default and wrong as an absolute — a real assistant may take a delivery order and may
not sign a contract. This module is the narrow exception: the owner names an action the
agent may take, and the limits it may take it within. Anything outside those limits
escalates exactly as before.

Four mechanisms, all generic — nothing here knows about pizza:

  declare   the owner states a capability; it lands in config (`add`)
  bound     every capability carries explicit limits; unbounded is not expressible
  evaluate  `candidates()` + `check_bounds()` decide whether an ask is inside them
  refine    the owner adjusts a limit by talking (`set_bound`, `remove`)

Config, not state: this file is per-owner and hand/MCP-editable, which is why it sits
beside owner.md rather than under .run/. The BOOKINGS a capability creates are state.
"""

import json
import pathlib
from datetime import datetime

from . import paths

STORE = paths.CAPABILITIES

#: Action primitives a capability may use. The vocabulary is deliberately tiny — each entry
#: is a thing the framework knows how to actually DO. Adding a capability that books time
#: needs no code; adding a capability that, say, issues a refund would need a new primitive
#: here, and that is the honest boundary of "config, not code".
ACTIONS = {
    "book_slot": "hold a block of time (uses schedule.py) and refuse overlaps",
}

#: Bound types the framework CHECKS mechanically. Anything else the owner writes is kept
#: and passed to the model as an instruction, but not enforced in code — see check_bounds.
#: Keeping this split explicit matters: an advisory bound looks identical to a checked one
#: in the owner's listing, and pretending "radius_km: 5" is enforced would be a lie.
CHECKED = {
    "hours": "HH:MM-HH:MM — the whole slot must fit inside this window",
    "block_minutes": "how long one booking occupies",
    "max_quantity": "numeric ceiling on how much may be ordered at once",
    "verified_only": "true = only identities the channel has verified",
}


def _load() -> dict:
    if not STORE.exists():
        return {}
    try:
        return json.loads(STORE.read_text())
    except json.JSONDecodeError:
        return {}


def _save(data: dict) -> None:
    STORE.write_text(json.dumps(data, indent=2))


def all_capabilities() -> dict:
    return _load()


def get(name: str) -> dict | None:
    return _load().get(name.strip().lower().replace(" ", "_"))


def add(name: str, what: str, action: str = "book_slot", bounds: dict | None = None) -> str:
    """DECLARE. Returns a human-readable confirmation, or an error string.

    Refuses an unknown action rather than accepting a capability the framework cannot
    perform: a declaration that silently never fires is worse than a rejection, because
    the owner believes the agent can do something it cannot.
    """
    key = name.strip().lower().replace(" ", "_")
    if not key or not what.strip():
        return "Give the capability a name and say what it covers."
    if action not in ACTIONS:
        return (f"Unknown action {action!r}. The framework can currently do: "
                + ", ".join(f"{k} ({v})" for k, v in ACTIONS.items()))
    data = _load()
    existing = data.get(key, {})
    data[key] = {
        "what": what.strip(),
        "action": action,
        "bounds": {**existing.get("bounds", {}), **(bounds or {})},
        "declared_at": existing.get("declared_at",
                                   datetime.now().isoformat(timespec="seconds")),
    }
    _save(data)
    verb = "Updated" if existing else "Declared"
    return f"{verb} capability {key!r}: {what.strip()}\n" + describe(key)


def set_bound(name: str, key: str, value) -> str:
    """REFINE. "make it 45 minutes", "stop taking orders after 8pm"."""
    data = _load()
    cap = data.get(name.strip().lower().replace(" ", "_"))
    if not cap:
        return f"No capability named {name!r}. Have: {', '.join(data) or 'none'}"
    k = key.strip()
    if value in (None, "", "none"):
        cap["bounds"].pop(k, None)
        _save(data)
        return f"Removed bound {k!r} from {name!r}.\n" + describe(name)
    # Numbers arrive as strings over JSON/MCP; keep them numeric so comparisons work
    # rather than failing silently on "4" > 4.
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    elif isinstance(value, str) and value.strip().lower() in ("true", "false"):
        value = value.strip().lower() == "true"
    cap["bounds"][k] = value
    _save(data)
    advisory = "" if k in CHECKED else "  (advisory — passed to the model, not enforced in code)"
    return f"{name}: {k} = {value}{advisory}\n" + describe(name)


def remove(name: str) -> str:
    data = _load()
    key = name.strip().lower().replace(" ", "_")
    if key not in data:
        return f"No capability named {name!r}."
    data.pop(key)
    _save(data)
    return f"Removed capability {key!r}. Asks it covered will escalate again."


def describe(name: str) -> str:
    cap = get(name)
    if not cap:
        return f"No capability named {name!r}."
    lines = [f"  {name}: {cap['what']}  [{cap['action']}]"]
    for k, v in sorted(cap.get("bounds", {}).items()):
        mark = "" if k in CHECKED else "  (advisory)"
        lines.append(f"    {k} = {v}{mark}")
    if not cap.get("bounds"):
        lines.append("    NO BOUNDS SET — nothing will be accepted until you set some")
    return "\n".join(lines)


def listing() -> str:
    data = _load()
    if not data:
        return "No capabilities declared. The agent can answer, but commits to nothing."
    return "\n".join(describe(k) for k in data)


def candidates() -> list[dict]:
    """EVALUATE, part 1: capabilities worth asking the model about, as prompt-ready dicts.

    No keyword matching here. With a handful of capabilities it is cheaper and far more
    robust to let the one model call that already has to extract the order details decide
    which capability it falls under — regex topic-matching is what made the action gate
    phrasing-dependent in the first place.
    """
    return [{"name": k, "what": v["what"], "action": v["action"],
             "bounds": v.get("bounds", {})}
            for k, v in _load().items()]


def disclosable() -> str:
    """Declared capabilities as facts the agent may STATE, not just act on.

    Without this the two sides disagreed out loud: asked "does the owner sell pizza?" the agent
    escalated for want of a document, then took a pizza order in the next message. You cannot
    hide a capability you exercise in front of the asker.

    The limits are included because refusals already say them ("up to 6 pizzas", "11:00-21:00"),
    so they are public in practice — and stating them here stops the agent quoting hours that
    contradict the ones it enforces.
    """
    out = []
    for name, cap in (_load() or {}).items():
        what = str((cap or {}).get("what") or "").strip()
        if not what:
            continue
        bounds = (cap or {}).get("bounds") or {}
        limits = []
        if bounds.get("hours"):
            limits.append(f"only between {bounds['hours']}")
        if bounds.get("max_quantity"):
            limits.append(f"up to {bounds['max_quantity']} at a time")
        if bounds.get("radius_km"):
            limits.append(f"within {bounds['radius_km']} km")
        if bounds.get("verified_only"):
            limits.append("only for verified identities")
        # Phrased as a FACT ABOUT THE OWNER first. An earlier version led with the agent's
        # authority ("this agent may arrange it: taking pizza orders") and the model drew no
        # conclusion about the owner from it — answering "No, Stanley does not sell pizza.
        # However, I can arrange a pizza delivery order for you" in a single breath.
        out.append(f"- The owner's business includes {what}. If asked whether the owner does "
                   f"this, the answer is YES. This agent may arrange it."
                   + (f" Limits: {', '.join(limits)}." if limits else ""))
    return "\n".join(out)


def check_bounds(name: str, verified: bool, quantity=None,
                 at: str = "", minutes: int | None = None) -> tuple[bool, str]:
    """EVALUATE, part 2: is this specific ask inside the declared limits?

    Returns (ok, why_not). Only the CHECKED bounds are enforced here; advisory ones went
    into the prompt. A capability with no bounds at all fails closed — declaring one
    should not hand over unlimited authority by omission.
    """
    cap = get(name)
    if not cap:
        return False, f"no capability named {name!r}"
    b = cap.get("bounds", {})
    if not b:
        return False, f"capability {name!r} has no bounds set, so nothing is authorised yet"

    if b.get("verified_only") and not verified:
        return False, "this capability is limited to verified identities"

    if "max_quantity" in b and quantity is not None:
        try:
            if int(quantity) > int(b["max_quantity"]):
                return False, (f"{quantity} is over the limit of {b['max_quantity']} "
                               f"per order")
        except (TypeError, ValueError):
            pass

    if at and "hours" in b:
        from . import schedule
        span = int(minutes or b.get("block_minutes") or 30)
        if not schedule.within_hours(at, span, str(b["hours"])):
            return False, f"{at} is outside the allowed hours ({b['hours']})"

    return True, ""


def block_minutes(name: str, default: int = 30) -> int:
    cap = get(name) or {}
    try:
        return int(cap.get("bounds", {}).get("block_minutes", default))
    except (TypeError, ValueError):
        return default
