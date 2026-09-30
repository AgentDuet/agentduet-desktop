"""One zip a tester can send when something goes wrong — Help › Export Logs… (2026-09-30).

WHY. A remote tester's report ("I clicked Allow and nothing happened") could not be answered
without a terminal, and asking a tester to run one is how a round trip becomes a day. The logs
already say most of what is needed; what was missing was a way to hand them over.

WHAT GOES IN, and the line it draws. The daemon's own logs, which build this is, and the
permission record — what a diagnosis needs. NEVER the credentials (`.env`, the sign-in store),
the recordings, the transcripts, the assistant's chat, or the Contacts file. The logs do carry
phone numbers and caller names, which the app says before saving; that is the one thing a
tester has to decide about.

THE SITE TOKEN IS SCRUBBED. Every request line in the log carries it (`?t=…`), and with it
anyone on the machine could drive the owner's site. Anything else shaped like a secret is
scrubbed too, on the same principle: a log is not the place a credential should travel.
"""
from __future__ import annotations

import io
import json
import platform
import re
import zipfile
from datetime import datetime

from . import paths

#: The newest part of the log only: enough for the last few days, small enough to email.
TAIL_BYTES = 5 * 1024 * 1024

#: What a secret looks like in a log line. The site token first — it is on every request.
_SECRETS = (
    (re.compile(r"([?&]t=)[A-Za-z0-9_\-]+"), r"\1REDACTED"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I), r"\1REDACTED"),
    (re.compile(r"((?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)"
                r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", re.I), r"\1REDACTED"),
)

#: The files that go in, relative to `run/`. Anything not named here stays out.
LOGS = ("daemon.log", "daemon-start.log")
STATE = ("permissions.json", "mic-state.json", "update.json")


def scrub(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def _tail(path, limit: int = TAIL_BYTES) -> str:
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - limit))
        data = f.read()
    text = data.decode("utf-8", "replace")
    # Cut at a line: a half line at the top reads as corruption.
    return text.split("\n", 1)[1] if size > limit and "\n" in text else text


def _about() -> dict:
    from . import build_id, owner, version_string
    out = {"version": version_string(), "build": build_id(), "exported": datetime.now().isoformat(),
           "system": platform.platform(), "machine": platform.machine(),
           "calls": owner.calls(), "answer_here_quarantined": owner.ANSWER_HERE_QUARANTINED}
    try:
        from . import connector
        out["backend"] = connector.environment()
    except Exception as exc:                      # a diagnostic must not fail to diagnose
        out["backend"] = f"unknown ({type(exc).__name__})"
    return out


def bundle() -> bytes:
    """The zip, in memory. Never raises for a missing file: a missing log is itself a finding."""
    buf = io.BytesIO()
    missing = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in LOGS:
            path = paths.RUN / name
            if path.is_file():
                z.writestr(name, scrub(_tail(path)))
            else:
                missing.append(name)
        for name in STATE:
            path = paths.RUN / name
            if path.is_file():
                z.writestr(name, scrub(path.read_text(encoding="utf-8", errors="replace")))
        # CONTACTS: whether access was given and how much was read — never the names.
        try:
            book = json.loads((paths.RUN / "contacts.json").read_text(encoding="utf-8"))
            z.writestr("contacts-access.json", json.dumps(
                {k: book.get(k) for k in ("access", "read", "at")}, indent=1))
        except (OSError, ValueError):
            pass
        z.writestr("about.json", json.dumps(_about(), indent=1))
        z.writestr("README.txt",
                   "AgentDuet Desktop logs, exported from the app.\n\n"
                   "Included: the app's logs (they contain phone numbers and caller names), "
                   "which build this is, and the permission record.\n"
                   "Not included: credentials, recordings, transcripts, the assistant chat, "
                   "or your contacts. The site token is removed from the logs.\n"
                   + (f"\nNot found on this machine: {', '.join(missing)}\n" if missing else ""))
    return buf.getvalue()
