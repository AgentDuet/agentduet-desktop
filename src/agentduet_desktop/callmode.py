"""Which handler owns the connector's one inbound-call slot.

ONE CONNECTOR HAS ONE `on_incoming_call`. Carrying a call and answering it are therefore
mutually exclusive per install — a MODE, not a preference, and the one place where the two
products in this binary genuinely collide (see `docs/design.md`, "Two products, one daemon").

`secretary_agent` picks the mode from `## Calls` and that remains the only place the choice is
made. This module exists for the case that choice does not cover: some OTHER path registering a
second handler. The SDK will accept it, both will attach, and the two race for the same call —
which presents as a call that answers intermittently, or one that connects and then drops,
neither of which points at its cause.

So the second registration RAISES instead. A daemon that refuses to start is a bug found in
seconds; two handlers on one connector is a bug found by a customer on a phone call.

Deliberately module state rather than a parameter threaded through: the thing being protected is
a process-wide resource — the single callback slot on the session manager — so the guard belongs
where anything that could take it can see it.
"""
from __future__ import annotations


#: Who holds the slot: "carry", "answer", or "" when nothing has claimed it yet.
_holder = ""


class CallHandlerConflict(RuntimeError):
    """A second handler tried to take the connector's one inbound-call slot."""


def claim(mode: str) -> None:
    """Take the call slot for `mode`, or raise if someone else already has it.

    Re-claiming by the SAME mode is allowed and does nothing. That is not laxity: the channel
    loop can reconnect and re-register after a drop, and turning a reconnect into a crash would
    trade a rare silent bug for a common loud one.
    """
    global _holder
    if _holder and _holder != mode:
        raise CallHandlerConflict(
            f"{_holder!r} already handles incoming calls; {mode!r} cannot also register. "
            "One connector has one handler — carrying and answering are exclusive per install. "
            "Set `## Calls` in settings.md to choose."
        )
    _holder = mode


def holder() -> str:
    """Which mode holds the slot, or "" if none does."""
    return _holder


def release() -> None:
    """Give the slot up. For tests, and for a daemon that tears a channel down to rebuild it."""
    global _holder
    _holder = ""
