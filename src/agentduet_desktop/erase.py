"""Deleting one call, everywhere this app keeps it (2026-10-07).

WHY. Deleting a recording in Finder left a full copy of both sides in
`run/legs`, and nothing could remove a call from the record at all. Since then the legs go once
the merge holds them (`merge.discard_legs`), and this is the owner's "Delete Call", which takes
the rest:

  - the recording and its `.txt`, in whichever folder it was merged into, and any legs left;
  - the call's line in `calls.jsonl`, and the index rebuilt without it;
  - in an edition with AI: the call's pieces in the search index, its calendar suggestion, and
    the brief of the person it was with, which is rebuilt from the calls that remain.

NOT the log. `daemon.log` names the number and the time of every call, and that stays until
the log is cleared.

A CORE MODULE: the recorder records calls, so it must be able to delete them. The AI stores are
reached only behind `edition.ai()`.
"""
from __future__ import annotations

import logging
import pathlib
import re

from . import calls, carry, edition, merge, paths

logger = logging.getLogger("secretary")

#: What a call id looks like. Anything else is refused before it reaches a file name.
CALL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _stem(name: str) -> str:
    """`<stamp>-<id>-caller.wav.part` -> `<stamp>-<id>`."""
    base = name.split(".", 1)[0]
    return carry.stem_of(base + ".wav")


def _folders() -> list[pathlib.Path]:
    out = []
    for folder in (carry.recordings(), paths.RUN / "recordings"):
        out += [folder, folder / carry.ANSWERED]
    return [f for f in dict.fromkeys(out) if f.is_dir()]


def _stems(call_id: str) -> set[str]:
    """Every stem this call was written under: from its row, and from what is on disk."""
    row = calls.get(call_id) or {}
    stems = {carry.stem_of(n) for n in row.get("recordings") or []}
    for folder in [carry.legs()] + _folders():
        if folder.is_dir():
            stems |= {_stem(f.name) for f in folder.iterdir()
                      if not f.name.startswith(".") and _stem(f.name).endswith("-" + call_id)}
    return {s for s in stems if s.endswith("-" + call_id)}


def _forget_ai(call_id: str, row: dict) -> None:
    """The call's traces in the AI stores. Each one on its own: one failing stops none."""
    from . import brief, search, suggest
    try:
        folder, names = carry.call_audio(row.get("recordings", []), call_id)
        suggest.forget(carry.transcript_of(names, folder))
    except Exception as exc:
        logger.warning("delete %s: the suggestion was not removed (%s)", call_id, exc)
    try:
        search.forget_call(call_id)
    except Exception as exc:
        logger.warning("delete %s: the search index was not updated (%s)", call_id, exc)
    try:
        brief.forget(calls.person_of(row))
    except Exception as exc:
        logger.warning("delete %s: the brief was not rebuilt (%s)", call_id, exc)


def delete_call(call_id: str) -> dict:
    """Delete one call. {"ok": bool, "message": str, "files": n}."""
    if not CALL_ID.match(call_id or ""):
        return {"ok": False, "message": "No such call.", "files": 0}
    row = calls.get(call_id)
    stems = _stems(call_id)
    if row is None and not stems:
        return {"ok": False, "message": "No such call.", "files": 0}
    # A CALL STILL BEING RECORDED is not deleted: its legs are open, and would come back.
    if any(f.name.endswith(".part") for s in stems for f in merge.leg_files(s)):
        return {"ok": False, "message": "The call is still on.", "files": 0}
    # THE AI STORES FIRST, while the transcript is still there to say what to remove.
    if row is not None and edition.ai():
        _forget_ai(call_id, row)
    n = 0
    for s in stems:
        n += merge.discard_legs(s, force=True)
        for folder in _folders():
            for f in folder.iterdir():
                if f.is_file() and _stem(f.name) == s:
                    try:
                        f.unlink()
                        n += 1
                    except OSError as exc:
                        logger.warning("delete %s: could not delete %s (%s)", call_id, f.name, exc)
    calls.forget(call_id)
    logger.info("call %s deleted (%d file(s))", call_id, n)
    return {"ok": True, "message": "Call deleted.", "files": n}
