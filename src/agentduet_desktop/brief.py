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

PROMPT = """Today is {today}.

You keep a short brief about one person {owner} talks to. {owner}'s assistant reads
it before answering questions about this person.

THIS BRIEF IS ABOUT: {person}. Never call them by any other name, and never write about
anyone else as if they were this person.

Update the brief with the new information below.
- Where the new information and the brief disagree, the NEWER one wins.
- Where {owner} and the other person disagree about the same thing, {owner}'s word wins.
- Keep the date beside anything that has one: appointments, promises, deadlines. Work out
  "tomorrow" or "Friday" from the date of the call it was said in, and write the date. Never
  add a weekday or a date that was not said or cannot be worked out that way.
- KEEP every open item already in the brief unless the new information changes, cancels or
  completes it. A call about one thing says nothing about the others.
- Drop an item only when the new information says it is finished or no longer true.
- Use only the brief and the new information. Do not guess.
- A CORRECTION from {owner} outranks everything: remove or change what it says is wrong,
  even in the current brief, and keep the rest.
- Never record what the assistant did or will do (running a tool, drafting) as an open item.
- At most {words} words, in three short parts:
  Who: who they are and how they relate to {owner}.
  Open: EVERY appointment, meeting, promise or follow-up either side mentioned that has not
        happened yet as of today, each with its date and time. One per line. A request to
        call back, or a promise to call, is a follow-up.
  Last contact: the date of the NEWEST call or message below, and what was said in it.

Reply with the brief only. Never repeat these rules.

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
    # NO AUDIO AT ALL is nothing said, not "still coming". The index is written when a call ends,
    # with its legs already on disk, so a row with no files will never have a transcript — and
    # waiting a day on one blocked every later call of that person behind it. (Found on a call
    # that rang out before calls were labelled "missed".)
    if not names:
        return ""
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


def _about_them(q: str, who: str) -> bool:
    """Whether a question asked on `who`'s page is ABOUT them (2026-09-30).

    Asked on Cen's page, "email Kok Choong" is about Kok Choong — and filed by page it made Cen's
    brief say "Who: Kok Choong". A question that names this person counts; one that names
    somebody else does not; one that names nobody is about the page it was asked on.
    """
    from . import calls, names
    if names.mentions(q, who):
        return True
    return not any(names.mentions(q, other) for other in calls.by_person() if other != who)


def _chat_after(who: str, after: str) -> list[tuple[str, str]]:
    """The owner's words to the assistant about `who`, newer than `after`, oldest first.

    THE OWNER'S WORDS ONLY — not the assistant's reply, whose "I need to run a tool" came back
    as an open item in the brief.
    """
    from .assistant import OwnerChat
    try:
        shown = json.loads(OwnerChat.STORE.read_text())
    except (OSError, ValueError):
        return []
    return [(t.get("at", ""), t.get("q", "")) for t in shown
            if t.get("about") == who and "q" in t and not t.get("pending")
            and (t.get("at") or "") > after and _about_them(t.get("q", ""), who)]


def _person(who: str) -> str:
    """How the prompt names them: the name the hub shows, or plainly that there is none."""
    from . import names
    name = names.name_for(who)
    return f"{name} ({who})" if name and name != who else \
        f"a person known only by their number, {who} — do not give them a name"


#: A CORRECTION IS NOT A CONTACT (2026-10-01). Given as the newest dated item, the owner's
#: correction "name" came back as "Last contact: Thursday 01 October, CORRECTION from Stanley:
#: name" on both briefs it fixed. So it goes in its own section, after the information, with
#: its own rule.
CORRECTION = """
CORRECTION FROM {owner} — this is not a contact or a conversation. Apply it to the brief:
change or remove whatever it says is wrong, keep everything else, and leave "Last contact" as it
was unless the correction is about it. Never mention the correction itself.
{correction}
"""


def _prompt(who: str, rec: dict, items: list[str], correction: str = "") -> str:
    from . import budget, owner
    name = owner.name() or "the owner"
    out = PROMPT.format(today=datetime.now().strftime("%A %d %B %Y"),
                        owner=name, person=_person(who),
                        words=budget.split()["person_words"],
                        asof=rec.get("updated", "never"),
                        current=rec.get("summary") or "(none yet)",
                        new="\n\n".join(items) if items else "(nothing new)")
    return out + (CORRECTION.format(owner=name, correction=correction) if correction else "")


def _day(at: str, plus: int = 0) -> str:
    """"Friday 25 September 2026" for an ISO timestamp, `plus` days on."""
    from datetime import timedelta
    try:
        return (datetime.fromisoformat(at) + timedelta(days=plus)).strftime("%A %d %B %Y")
    except ValueError:
        return at


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
    # RELATIVE DATES ARE WORKED OUT HERE, NOT BY THE MODEL. Told only the call's timestamp, Gemma
    # put "lunch tomorrow" said on the 25th on the 25th in some runs and the 26th in others, at
    # temperature 0. So each call says what "today" and "tomorrow" meant in it.
    items = [(at, f"{_day(at)} — a call with them. In this call \"today\" means {_day(at)} and "
                  f"\"tomorrow\" means {_day(at, 1)}.\n" + tools.untrusted(text))
             for at, text in calls if text]
    items += [(at, f"{_day(at)} — {owner.name() or 'the owner'} said to the assistant (\"tomorrow\" "
                   f"means {_day(at, 1)}): {q}") for at, q in chat]
    items.sort()
    summary = llm.client().complete(_prompt(who, rec, [x for _, x in items])).strip()
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


def correct(who: str, correction: str) -> str:
    """Fold the owner's correction into `who`'s brief NOW, as the newest and strongest fact.

    Through the model, never a text edit: the owner says what is wrong in their own words and
    the brief is rewritten around it, with the rest kept (Stanley, 2026-09-30).
    """
    from . import llm, owner
    rec = load(who)
    if not rec.get("summary"):
        return "There is no brief about them yet."
    if not llm.configured():
        return "No model is set up to rewrite the brief."
    said = " ".join((correction or "").split())[:600]
    if not said:
        return "Nothing to correct."
    # AS THE BRIEF'S OWN JOB, not as the question it was asked in: `gate` saves and restores the
    # assistant's cached conversation around a PERSON job, and a rewrite at QUESTION priority
    # would overwrite it mid-turn.
    with gate.priority(gate.PERSON):
        summary = llm.client().complete(_prompt(who, rec, [], correction=said)).strip()
    if not summary:
        return "The model returned nothing, so the brief is unchanged."
    rec.update(summary=summary, updated=datetime.now().isoformat(timespec="seconds"))
    _save(who, rec)
    logger.info("brief for %s corrected by the owner", who)
    return "Corrected. The brief now reads:\n" + summary


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
