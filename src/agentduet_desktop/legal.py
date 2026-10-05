"""The Terms of Use and the Privacy Policy, and the owner's agreement to them (2026-10-05).

The texts are `legal/terms.md` and `legal/privacy.md`, shipped in the package so every edition
shows the same words offline. Setup shows them first, before sign-in, because sign-in is
already a use of the service. Nothing goes further until the owner agrees.

THE AGREEMENT NAMES A VERSION, NOT A FILE HASH. `VERSION` changes when the terms change in a way
the owner must see again; a typo fixed does not ask everyone to agree again. Bump it in the same
commit as the change that needs it. An agreement to an older version counts as none, so the
next launch shows the new text.

A CORE MODULE, in every edition: the recorder records calls, which is exactly what the terms are
about. Whether a partner's own terms replace these in a branded build is open (CLAUDE.md).

THE HTML WIZARD DOES NOT ASK (it is frozen, Mac first), which is why this is NOT part of
`needs_setup`: a page with no terms step would be stuck on setup for ever. The native shell asks
`agreed()` itself.
"""
from __future__ import annotations

import json
import pathlib
import time

from . import edition, paths

#: The version of the terms an agreement is to. Bump it when the owner must read them again.
VERSION = "2026-10-05"

HERE = pathlib.Path(__file__).resolve().parent / "legal"
DOCUMENTS = {"terms": "terms.md", "privacy": "privacy.md"}


def _record() -> pathlib.Path:
    return paths.RUN / "terms.json"


def text(name: str) -> str:
    """One document, as the owner reads it."""
    return (HERE / DOCUMENTS[name]).read_text(encoding="utf-8")


def agreement() -> dict:
    """What the owner agreed to, or {} if nothing readable is on file."""
    try:
        got = json.loads(_record().read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def agreed() -> bool:
    """Whether the owner has agreed to THESE terms."""
    return agreement().get("version") == VERSION


def agree(version: str) -> bool:
    """Record the owner's agreement. Refused unless `version` is the one being shown now, so an
    agreement can never be to words the owner was not shown."""
    if version != VERSION:
        return False
    paths.RUN.mkdir(parents=True, exist_ok=True)
    record = {"version": VERSION, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "edition": edition.name()}
    tmp = _record().with_suffix(".part")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_record())
    return True


def state() -> dict:
    """What a UI needs to show the terms and ask for agreement."""
    got = agreement()
    return {"version": VERSION, "agreed": got.get("version") == VERSION,
            "agreed_at": got.get("at", "") if got.get("version") == VERSION else "",
            "terms": text("terms"), "privacy": text("privacy")}
