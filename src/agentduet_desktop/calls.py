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
           note: str = "") -> None:
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


def by_person(limit: int = 200) -> dict[str, list[dict]]:
    """Calls grouped by who was on them, newest first within each."""
    grouped: dict[str, list[dict]] = {}
    for row in recent(limit):
        grouped.setdefault(row.get("caller") or "?", []).append(row)
    return grouped
