"""A short running summary of each person the owner talks to, kept up to date in the background.

WHY. Asked about someone, the assistant was handed up to five of their full transcripts on every
question — about 5,000 tokens, re-read from scratch each time the owner opened a different
person. A brief of ~150 words carries what matters (who they are, what is open, the last
contact), and only calls it has not absorbed yet still go in whole.

SOURCES: that person's calls, and the owner's chat with the assistant ABOUT them (a turn asked
while their page was open). Shaped only by the prompt below — nobody edits a brief by hand.

LATEST WINS. Each update gives the model the current brief plus the new, DATED information, and
tells it the newer fact replaces the older where they disagree, and that the owner's word
outranks the caller's about the same thing. The dates it keeps are what let the next update
tell which fact is newer.

INCREMENTAL, NEVER REGENERATED. An update folds only what is new into the current brief. A
summary rewritten from everything each time wears facts away, and a small model compounds its
own mistakes.

WATERMARKS. A brief records the newest call and the newest chat turn it absorbed. An update
with nothing newer ends before touching the model — which is what makes `sweep()` after every
merge cheap, and makes a restart harmless.

A brief is DERIVED FROM WHAT A CALLER SAID, so where it enters a prompt it is marked with
`tools.untrusted`, the same as the transcripts it came from.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from . import gate, jobs, paths

logger = logging.getLogger("secretary.brief")

#: Calls folded in one update. More wait for the next one, which is requested straight away.
CALLS_PER_UPDATE = 3
#: Characters of one transcript given to an update.
CALL_CHARS = 4000

PROMPT = """You keep a short brief about one person {owner} talks to. {owner}'s assistant reads
it before answering questions about this person.

Update the brief with the new information below.
- Where the new information and the brief disagree, the NEWER one wins.
- Where {owner} and the other person disagree about the same thing, {owner}'s word wins.
- Keep the date beside anything that has one: appointments, promises, deadlines.
- Drop what is finished or no longer true.
- Use only the brief and the new information. Do not guess.
- At most {words} words, in three short parts:
  Who: who they are and how they relate to {owner}.
  Open: what is still open, with dates.
  Last contact: the date and what it was about.

Reply with the brief only.

CURRENT BRIEF (as of {asof}):
{current}

NEW INFORMATION, oldest first:
{new}
"""


def _dir():
    return paths.RUN / "briefs"


def _file(who: str):
    return _dir() / (re.sub(r"[^A-Za-z0-9+@._-]+", "_", who.strip()) + ".json")


def load(who: str) -> dict:
    try:
        return json.loads(_file(who).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(who: str, rec: dict) -> None:
    f = _file(who)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


def _transcript(row: dict) -> str | None:
    """The call's transcript text, "" for a call with nothing to say, None while still coming."""
    from . import carry
    if row.get("note") in ("missed", "missed in the app"):
        return ""
    folder, names = carry.call_audio(row.get("recordings", []), row.get("call_id", ""))
    for n in names:
        t = (folder / n).with_suffix(".txt")
        if t.is_file():
            try:
                return t.read_text(encoding="utf-8")[:CALL_CHARS]
            except OSError:
                return ""
    # NOT YET TRANSCRIBED — or never will be. A row a day old with no transcript is not coming.
    try:
        age = (datetime.now() - datetime.fromisoformat(row.get("at", ""))).total_seconds()
    except ValueError:
        return ""
    return None if age < 86400 else ""


def _calls_after(who: str, after: str) -> tuple[list[tuple[str, str]], bool]:
    """(calls newer than `after`, oldest first, with their text), and whether more remain.

    Stops at the first call still being transcribed, so the watermark never jumps past one.
    """
    from . import calls as _calls
    rows = [r for r in _calls.recent() if (r.get("caller") or "") == who
            and (r.get("at") or "") > after]
    rows.sort(key=lambda r: r.get("at", ""))
    out = []
    for r in rows:
        text = _transcript(r)
        if text is None:
            break
        out.append((r.get("at", ""), text))
        if len(out) >= CALLS_PER_UPDATE:
            return out, len(rows) > len(out)
    return out, False


def _chat_after(who: str, after: str) -> list[tuple[str, str, str]]:
    """The owner's turns with the assistant about `who`, newer than `after`, oldest first."""
    from .assistant import OwnerChat
    try:
        shown = json.loads(OwnerChat.STORE.read_text())
    except (OSError, ValueError):
        return []
    return [(t.get("at", ""), t.get("q", ""), t.get("a", "")) for t in shown
            if t.get("about") == who and "q" in t and not t.get("pending")
            and (t.get("at") or "") > after]


def update(who: str) -> bool:
    """Fold what is new about `who` into their brief. True when the model was asked."""
    from . import llm, owner, tools
    rec = load(who)
    calls, more = _calls_after(who, rec.get("through_call", ""))
    chat = _chat_after(who, rec.get("through_chat", ""))
    if not calls and not chat:
        return False                                  # the watermark: nothing new
    if not chat and not any(text for _, text in calls):
        # ONLY CALLS WITH NOTHING SAID (missed, or no audio): nothing to fold, so move the
        # watermark past them without asking the model.
        rec["through_call"] = calls[-1][0]
        _save(who, rec)
        if more:
            request(who)
        return False
    if not llm.configured():
        return False
    items = [(at, f"{at} — a call with them:\n" + (tools.untrusted(text) if text
                                                   else "(nothing was said)"))
             for at, text in calls]
    items += [(at, f"{at} — {owner.name() or 'the owner'} asked the assistant: {q}\n"
                   f"The assistant answered: {a}") for at, q, a in chat]
    items.sort()
    from . import budget
    prompt = PROMPT.format(owner=owner.name() or "the owner", words=budget.split()["person_words"],
                           asof=rec.get("updated", "never"),
                           current=rec.get("summary") or "(none yet)",
                           new="\n\n".join(x for _, x in items))
    summary = llm.client().complete(prompt).strip()
    if not summary:
        return True
    rec.update(summary=summary, updated=datetime.now().isoformat(timespec="seconds"))
    if calls:
        rec["through_call"] = calls[-1][0]
    if chat:
        rec["through_chat"] = chat[-1][0]
    _save(who, rec)
    logger.info("brief for %s updated from %d call(s) and %d turn(s)", who, len(calls), len(chat))
    if more:
        request(who)
    return True


def request(who: str) -> None:
    if who:
        jobs.request("person:" + who, gate.PERSON, lambda: update(who))


def sweep() -> int:
    """Request an update for everyone with a call newer than their brief. Cheap when idle."""
    from . import calls as _calls
    newest: dict[str, str] = {}
    for r in _calls.recent():
        who, at = r.get("caller") or "", r.get("at") or ""
        if who and at > newest.get(who, ""):
            newest[who] = at
    n = 0
    for who, at in newest.items():
        if at > load(who).get("through_call", ""):
            request(who)
            n += 1
    return n


def for_prompt(who: str) -> str:
    """What the assistant is handed about `who`: the brief, and calls it has not absorbed yet."""
    from . import tools
    rec = load(who)
    parts = []
    if rec.get("summary"):
        parts.append(f"WHAT YOU KNOW ABOUT THEM (a running brief, as of {rec.get('updated', '')}):\n"
                     + tools.untrusted(rec["summary"]))
    calls, _ = _calls_after(who, rec.get("through_call", ""))
    for at, text in calls[-2:]:
        if text:
            parts.append(f"--- {at} with {who} (newer than the brief) ---\n" + tools.untrusted(text))
    return "\n\n".join(parts)
