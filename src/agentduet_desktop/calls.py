"""What happened on a call, as the recorder's own record.

WHY NOT `brain.record`. That is a QUERY log — asker, question, outcome, reason, answer — built
for an agent that was asked something and decided what to say. A carried call has no question and
no answer: two people talked and we kept the audio. Writing it there would mean inventing a
question to satisfy a schema, and then every reader of that log has to know which rows are real
queries. The recorder gets its own noun instead.

WHY A FILE AND NOT A DATABASE. The same reason the transcription queue is the filesystem: one
append per call, restart-safe, nothing to corrupt, and readable with `cat` when someone is
trying to work out what happened on a call at 3am.

The caller is the point. Recording filenames carry a call id, so without this there is no way
back from a `.wav` to a person — which is exactly what a per-person view needs.
"""

import json
import logging
from datetime import datetime

from . import paths

logger = logging.getLogger("dduet.calls")

#: One JSON object per line, appended. Never rewritten.
LOG = paths.RUN / "calls.jsonl"


def record(call_id: str, caller: str, mode: str, *, recordings: list[str] | None = None,
           note: str = "", outgoing: bool = False) -> None:
    """Append one call. Never raises: losing the audio matters, losing the index does not."""
    try:
        paths.RUN.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps({
                "at": datetime.now().isoformat(timespec="seconds"),
                "call_id": call_id,
                # E.164 where the platform gives it. "?" when it does not — better an honest
                # unknown than a row silently attributed to the wrong person.
                "caller": caller or "?",
                "mode": mode,                      # "carried" | "answered"
                # WHICH WAY THE CALL WENT, as a field. It briefly lived inside `caller` as a
                # "to "/"from " prefix, which split one person into several — see carry.handle.
                "outgoing": outgoing,
                "recordings": recordings or [],
                "note": note,
            }) + "\n")
    except OSError as exc:
        logger.warning("could not record call %s: %s", call_id, exc)


def recent(limit: int = 200) -> list[dict]:
    """Newest first. Bounded, because a machine that has carried calls for a year has thousands."""
    if not LOG.is_file():
        return []
    out = []
    try:
        for line in LOG.read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue                      # one bad line must not lose the rest
    except OSError:
        return []
    return out[::-1][:limit]


#: Identities written before the direction moved to its own field. Stripped on read so an
#: existing log merges instead of showing one person two or three times.
_LEGACY_PREFIXES = ("to ", "from ")


def person_of(row: dict) -> str:
    """The person a call belongs to: the number alone, whichever way the call went."""
    who = (row.get("caller") or "?").strip()
    for p in _LEGACY_PREFIXES:
        if who.startswith(p):
            who = who[len(p):].strip()
            break
    return who or "?"


def by_person(limit: int = 200) -> dict[str, list[dict]]:
    """Calls grouped by who was on them, newest first within each.

    GROUPED BY NUMBER, NOT BY THE STRING IN THE ROW. A direction word briefly lived inside
    `caller`, so the same number appeared as "+65…", "to +65…" and "from +65…" — three people
    with three histories, one of whom had rung the other two. `person_of` strips that, which
    also merges any rows already written that way rather than leaving them stranded.
    """
    grouped: dict[str, list[dict]] = {}
    for row in recent(limit):
        grouped.setdefault(person_of(row), []).append(row)
    return grouped
