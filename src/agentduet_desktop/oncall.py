"""The calls on right now — what the hub needs to mark a person "On a call" (2026-10-03).

Split out of `live.py` for the RECORDER edition, which has no live captions (they are speech
recognition, so AI — see `edition.py`) but still shows who is on the line. `live` adds captions
to these same records in the full edition; here they are only ever an empty list, which the
pages already render as a call with nothing said yet.
"""
from __future__ import annotations

import time

#: The calls on right now: call_id -> {"who", "started", "captions": [...]}. What a page that opens
#: mid-call needs in order to catch up.
_calls: dict[str, dict] = {}


def active() -> bool:
    return bool(_calls)


def snapshot() -> dict:
    """Every call in progress, with its captions so far — sent to a page when it connects."""
    return {"type": "live_calls",
            "calls": [{"call": cid, "who": c["who"], "started": c["started"],
                       "captions": c["captions"]} for cid, c in _calls.items()]}


async def push(obj: dict) -> None:
    from . import phone
    await phone._broadcast(obj)


async def start(call_id: str, who: str) -> None:
    """A call is on. Pages mark the person as live."""
    _calls[call_id] = {"who": who, "started": time.time(), "captions": []}
    await push({"type": "live_start", "call": call_id, "who": who,
                "started": _calls[call_id]["started"]})


async def end(call_id: str) -> bool:
    """The call is over. Pages drop the live mark. True when it was on."""
    if _calls.pop(call_id, None) is None:
        return False
    await push({"type": "live_end", "call": call_id})
    return True
